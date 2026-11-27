from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inkpress.ai.client import AIClient
from inkpress.ai.editorial import (
    CuratedDigestResult,
    curate_illustrated_digest,
    enrich_articles_with_ai,
    extract_candidates_from_articles,
    extract_tugua_topics,
    is_duplicate_topic,
)
from inkpress.config import get_settings
from inkpress.db.base import get_session_factory
from inkpress.db.models import (
    Article,
    ArticleDelivery,
    Book,
    Delivery,
    Edition,
    EditionSource,
    Source,
    utcnow,
)
from inkpress.delivery.smtp import parse_recipients, send_ebook_via_smtp
from inkpress.ingestion.service import sync_source
from inkpress.logging import get_logger
from inkpress.publishing.book_converter import (
    build_companion_kindle_mobi,
    convert_book_to_kindle_epub,
)
from inkpress.publishing.epub_builder import EpubChapterInput, build_kindle_edition_epubs
from inkpress.settings_store import get_ai_config, get_network_config, get_smtp_config
from inkpress.worker.broker import enqueue_job, publish_event

logger = get_logger(__name__)

AI_EDITORIAL_TIMEOUT_SECONDS = 240.0
EDITION_LOCK_TTL_SECONDS = 900
EDITION_LOCK_RENEW_SECONDS = 60.0

_ROUNDUP_TITLE_RE = re.compile(
    r"(集锦|早报|晚报|派评|盘后|复盘|速递|汇总|周报|日报|读报|要闻回顾|大事件)"
)

_LOCAL_EDITION_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = (
    WeakKeyDictionary()
)

_UNLOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_RENEW_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], tonumber(ARGV[2]))
else
    return 0
end
"""


def _get_local_edition_lock(edition_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_loop = _LOCAL_EDITION_LOCKS.get(loop)
    if per_loop is None:
        per_loop = {}
        _LOCAL_EDITION_LOCKS[loop] = per_loop
    lock = per_loop.get(edition_id)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[edition_id] = lock
    return lock


@asynccontextmanager
async def _edition_mutex(edition_id: int, *, wait_seconds: float = 0.0) -> AsyncIterator[bool]:
    """Acquire per-edition mutex across both in-process coroutines and Redis workers."""
    local_lock = _get_local_edition_lock(edition_id)
    if local_lock.locked() and wait_seconds <= 0:
        yield False
        return

    try:
        if wait_seconds > 0:
            await asyncio.wait_for(local_lock.acquire(), timeout=wait_seconds)
        else:
            await asyncio.wait_for(local_lock.acquire(), timeout=0.05)
    except TimeoutError:
        yield False
        return

    lock_key = f"inkpress:lock:edition:{edition_id}"
    token = uuid.uuid4().hex
    redis_client: aioredis.Redis | None = None
    redis_acquired = False
    renew_task: asyncio.Task[None] | None = None

    try:
        try:
            redis_client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
            deadline = time.monotonic() + max(0.0, wait_seconds)
            while True:
                ok = await redis_client.set(
                    lock_key, token, nx=True, ex=EDITION_LOCK_TTL_SECONDS
                )
                if ok:
                    redis_acquired = True
                    break
                if time.monotonic() >= deadline:
                    yield False
                    return
                await asyncio.sleep(1.0)
        except Exception as exc:
            # Gracefully degrade to local lock when Redis is offline (e.g. SQLite unit tests)
            logger.debug("edition_redis_lock_unavailable", edition_id=edition_id, err=str(exc))
            if redis_client is not None:
                with contextlib.suppress(Exception):
                    await redis_client.aclose()
                redis_client = None

        if redis_acquired and redis_client is not None:
            rc = redis_client

            async def _renew_loop() -> None:
                while True:
                    await asyncio.sleep(EDITION_LOCK_RENEW_SECONDS)
                    with contextlib.suppress(Exception):
                        await rc.eval(_RENEW_LOCK_LUA, 1, lock_key, token, EDITION_LOCK_TTL_SECONDS)  # type: ignore[misc]

            renew_task = asyncio.create_task(_renew_loop())

        yield True
    finally:
        if renew_task is not None:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renew_task
        if redis_acquired and redis_client is not None:
            with contextlib.suppress(Exception):
                await redis_client.eval(_UNLOCK_LUA, 1, lock_key, token)  # type: ignore[misc]
        if redis_client is not None:
            with contextlib.suppress(Exception):
                await redis_client.aclose()
        local_lock.release()


async def _make_ai_client(ai_cfg: dict[str, Any], outbound_proxy: str) -> AIClient | None:
    if not ai_cfg.get("enabled") or not ai_cfg.get("api_key"):
        return None
    return AIClient(
        base_url=str(ai_cfg["base_url"]),
        api_key=str(ai_cfg["api_key"]),
        model=str(ai_cfg["model"]),
        outbound_proxy=outbound_proxy,
        use_proxy=bool(ai_cfg.get("use_proxy", False)),
        max_concurrency=int(ai_cfg.get("max_concurrency", 3)),
        timeout_seconds=max(int(ai_cfg.get("timeout_seconds", 120)), 120),
    )


async def _record_article_deliveries(
    session: AsyncSession,
    *,
    edition_id: int,
    delivery_id: int,
    article_ids: list[int],
) -> None:
    """Upsert delivered articles in the dedup ledger, refreshing `delivered_at` on re-delivery."""
    unique_ids = list(dict.fromkeys(article_ids))
    if not unique_ids:
        return
    now = utcnow()
    rows = [
        {
            "edition_id": edition_id,
            "article_id": aid,
            "delivery_id": delivery_id,
            "delivered_at": now,
        }
        for aid in unique_ids
    ]
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        stmt = (
            pg_insert(ArticleDelivery)
            .values(rows)
            .on_conflict_do_update(
                constraint="uq_article_delivery_edition_article",
                set_={"delivery_id": delivery_id, "delivered_at": now},
            )
        )
        await session.execute(stmt)
        return
    if dialect == "sqlite":
        stmt_sq = (
            sqlite_insert(ArticleDelivery)
            .values(rows)
            .on_conflict_do_update(
                index_elements=["edition_id", "article_id"],
                set_={"delivery_id": delivery_id, "delivered_at": now},
            )
        )
        await session.execute(stmt_sq)
        return

    existing_rows = {
        row.article_id: row
        for row in (
            await session.execute(
                select(ArticleDelivery).where(
                    ArticleDelivery.edition_id == edition_id,
                    ArticleDelivery.article_id.in_(unique_ids),
                )
            )
        )
        .scalars()
        .all()
    }
    for aid in unique_ids:
        existing_row = existing_rows.get(aid)
        if existing_row is not None:
            existing_row.delivery_id = delivery_id
            existing_row.delivered_at = now
        else:
            session.add(
                ArticleDelivery(
                    edition_id=edition_id,
                    article_id=aid,
                    delivery_id=delivery_id,
                    delivered_at=now,
                )
            )


async def run_sync_source(source_id: int) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return {"error": "source_not_found", "source_id": source_id}
        source_name = source.name
        await publish_event("source.sync.started", {"source_id": source.id, "name": source_name})
        try:
            stats = await sync_source(session, source)
            await publish_event(
                "source.sync.completed",
                {"source_id": source.id, "name": source_name, **stats},
            )
            return {"source_id": source.id, **stats}
        except Exception as exc:
            await session.rollback()
            await publish_event(
                "source.sync.failed",
                {"source_id": source_id, "name": source_name, "error": str(exc)},
            )
            raise


def _is_digest_bundle_article(art: Article) -> bool:
    meta = art.extra_meta if isinstance(art.extra_meta, dict) else {}
    if isinstance(meta.get("digest_items"), list) or meta.get("newsnow_id"):
        return True
    return '<div class="digest-item"' in (art.clean_html or "")


def _is_anchor_tugua_article(art: Article, role: str = "") -> bool:
    if role == "anchor":
        return True
    return "喷嚏图卦" in (art.title or "") or "喷嚏图卦" in (art.source_name or "")


def _is_roundup_article(art: Article, role: str = "") -> bool:
    if role == "deep":
        return False
    return bool(_ROUNDUP_TITLE_RE.search(art.title or ""))


def _extract_delta_digest_article(art: Article, edition_id: int) -> Article | None:
    """When a daily `newsnow_digest` row was already sent earlier today, extract newly-emerged items."""
    meta = art.extra_meta if isinstance(art.extra_meta, dict) else {}
    structured = meta.get("digest_items")
    if not isinstance(structured, list) or not structured:
        return None
    sigs_map = meta.get("delivered_sigs_by_edition")
    if not isinstance(sigs_map, dict):
        return None
    prev_state = sigs_map.get(str(edition_id))
    if not isinstance(prev_state, dict):
        return None
    cur_sig = str(meta.get("items_sig") or "")
    if cur_sig and prev_state.get("items_sig") == cur_sig:
        return None

    sent_urls = {str(u) for u in (prev_state.get("urls") or []) if u}
    sent_titles = {str(t) for t in (prev_state.get("titles") or []) if t}
    delta_items = [
        it
        for it in structured
        if isinstance(it, dict)
        and str(it.get("title") or "")
        and str(it.get("url") or "") not in sent_urls
        and str(it.get("title") or "") not in sent_titles
    ]
    if len(delta_items) < 2:
        return None

    clone_meta = dict(meta)
    clone_meta["digest_items"] = delta_items
    return Article(
        id=art.id,
        source_id=art.source_id,
        canonical_url=art.canonical_url,
        content_hash=art.content_hash,
        title=art.title,
        translated_title=art.translated_title,
        author=art.author,
        source_name=art.source_name,
        category=art.category,
        language=art.language,
        published_at=art.published_at,
        fetched_at=art.fetched_at,
        lead_image_url=art.lead_image_url,
        clean_html=art.clean_html,
        plain_text=art.plain_text,
        ai_summary=art.ai_summary,
        bilingual_html=art.bilingual_html,
        word_count=art.word_count,
        reading_time_min=art.reading_time_min,
        extra_meta=clone_meta,
    )


async def _record_digest_delivery_snapshots(
    session: AsyncSession,
    *,
    edition_id: int,
    article_ids: list[int],
) -> None:
    """Persist delivered `items_sig` & URLs/titles on `newsnow_digest` rows for same-day delta runs."""
    if not article_ids:
        return
    rows = (
        (await session.execute(select(Article).where(Article.id.in_(article_ids))))
        .scalars()
        .all()
    )
    ed_key = str(edition_id)
    for db_art in rows:
        meta = dict(db_art.extra_meta) if isinstance(db_art.extra_meta, dict) else {}
        structured = meta.get("digest_items")
        if not isinstance(structured, list) or not structured:
            continue
        sigs_map = dict(meta.get("delivered_sigs_by_edition") or {})
        prev_raw = sigs_map.get(ed_key)
        prev: dict[str, Any] = prev_raw if isinstance(prev_raw, dict) else {}
        merged_urls = list(
            dict.fromkeys(
                [*(prev.get("urls") or []), *[str(it.get("url") or "") for it in structured if isinstance(it, dict) and it.get("url")]]
            )
        )
        merged_titles = list(
            dict.fromkeys(
                [*(prev.get("titles") or []), *[str(it.get("title") or "") for it in structured if isinstance(it, dict) and it.get("title")]]
            )
        )
        sigs_map[ed_key] = {
            "items_sig": str(meta.get("items_sig") or ""),
            "urls": merged_urls[:120],
            "titles": merged_titles[:120],
        }
        meta["delivered_sigs_by_edition"] = sigs_map
        db_art.extra_meta = meta


async def _run_build_and_deliver_edition_unlocked(
    edition_id: int,
    *,
    trigger: str = "manual",
    send_email: bool = True,
    ignore_dedup: bool = False,
    delivery_id: int | None = None,
) -> dict[str, Any]:
    """Inner edition pipeline called under `_edition_mutex`."""
    settings = get_settings()
    settings.ensure_storage_dirs()
    factory = get_session_factory()
    started = time.monotonic()
    stage_timings: dict[str, int] = {}

    # Phase 1: Load edition/settings snapshot & create/claim running delivery record
    async with factory() as session:
        edition = (
            await session.execute(
                select(Edition)
                .options(selectinload(Edition.source_links).selectinload(EditionSource.source))
                .where(Edition.id == edition_id)
            )
        ).scalar_one_or_none()
        if edition is None:
            return {"error": "edition_not_found", "edition_id": edition_id}

        smtp_cfg = await get_smtp_config(session)
        ai_cfg = await get_ai_config(session)
        net_cfg = await get_network_config(session)

        raw_targets = edition.target_emails.strip() or str(
            smtp_cfg.get("default_kindle_email") or ""
        )
        recipients = parse_recipients(raw_targets)

        ed_name = edition.name
        ed_subtitle = edition.subtitle
        ed_cover_style = edition.cover_style
        ed_timezone = edition.timezone
        ed_lookback_hours = edition.lookback_hours
        ed_dedup_window_days = edition.dedup_window_days
        ed_max_articles = edition.max_articles
        ed_max_mb_per_volume = edition.max_mb_per_volume
        ed_grayscale_images = edition.grayscale_images
        ed_include_qr_links = edition.include_qr_links
        ed_strip_links = bool(edition.strip_links)
        ed_ai_briefing_enabled = edition.ai_briefing_enabled
        next_issue = (edition.issue_counter or 0) + 1

        active_source_specs: list[tuple[int, int, str, dict[str, Any]]] = []
        for link in edition.source_links:
            if link.source is not None and link.source.enabled:
                per_limit = link.max_items_override or link.source.max_items or 12
                active_source_specs.append(
                    (
                        link.source.id,
                        per_limit,
                        link.source.fetch_strategy,
                        dict(link.source.ai_options or {}),
                    )
                )

        target_email_str = ", ".join(recipients) if send_email else ""
        delivery: Delivery | None = None
        if delivery_id is not None:
            delivery = await session.get(Delivery, delivery_id)
        if delivery is not None:
            delivery.edition_id = edition.id
            delivery.title = ed_name
            delivery.trigger = trigger
            delivery.target_email = target_email_str
            delivery.status = "running"
            delivery.error_message = None
            delivery.finished_at = None
        else:
            delivery = Delivery(
                edition_id=edition.id,
                title=ed_name,
                trigger=trigger,
                target_email=target_email_str,
                status="running",
            )
            session.add(delivery)
        await session.commit()
        delivery_id = delivery.id

    await publish_event(
        "delivery.stage",
        {
            "delivery_id": delivery_id,
            "edition_id": edition_id,
            "title": ed_name,
            "stage": "syncing",
        },
    )

    try:
        # Phase 2: Sync bound enabled sources concurrently (Semaphore(4), isolated Session per source)
        t0 = time.monotonic()
        sync_sem = asyncio.Semaphore(4)

        async def _sync_one_source(src_id: int) -> None:
            async with sync_sem, factory() as sync_session:
                try:
                    src = await sync_session.get(Source, src_id)
                    if src is not None and src.enabled:
                        await sync_source(sync_session, src)
                except Exception as exc:
                    await sync_session.rollback()
                    logger.warning(
                        "edition_source_sync_error",
                        edition_id=edition_id,
                        source_id=src_id,
                        err=str(exc),
                    )

        await asyncio.gather(*[_sync_one_source(sid) for sid, _, _, _ in active_source_specs])
        stage_timings["sync_ms"] = int((time.monotonic() - t0) * 1000)

        # Phase 3: Select fresh, undelivered articles within lookback window (with deep-read reservation)
        selected_articles: list[Article] = []
        summarize_ids: set[int] = set()
        bilingual_ids: set[int] = set()
        role_by_art_id: dict[int, str] = {}
        seen_ids: set[int] = set()
        raw_selection_cap = (
            max(ed_max_articles * 3, 64) if ed_ai_briefing_enabled else ed_max_articles
        )

        async with factory() as sel_session:
            cutoff_pub = utcnow() - timedelta(hours=max(1, ed_lookback_hours))
            cutoff_dedup = utcnow() - timedelta(days=max(1, ed_dedup_window_days))

            already_sent_ids = (
                set()
                if ignore_dedup
                else set(
                    (
                        await sel_session.execute(
                            select(ArticleDelivery.article_id).where(
                                ArticleDelivery.edition_id == edition_id,
                                ArticleDelivery.delivered_at >= cutoff_dedup,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            )

            for src_id, per_limit, fetch_strategy, ai_opts in active_source_specs:
                rows = (
                    (
                        await sel_session.execute(
                            select(Article)
                            .where(
                                Article.source_id == src_id,
                                Article.fetched_at >= cutoff_pub,
                            )
                            .order_by(desc(Article.published_at), desc(Article.id))
                            .limit(max(per_limit * 3, 24))
                        )
                    )
                    .scalars()
                    .all()
                )
                src_role = str(ai_opts.get("editorial_role") or "").strip().lower()
                eligible_for_src: list[Article] = []
                for art in rows:
                    if art.id in seen_ids:
                        continue
                    if art.id in already_sent_ids:
                        if not ignore_dedup and _is_digest_bundle_article(art):
                            delta_art = _extract_delta_digest_article(art, edition_id)
                            if delta_art is not None:
                                eligible_for_src.append(delta_art)
                        continue
                    eligible_for_src.append(art)

                source_picked = eligible_for_src[:per_limit]
                # Guarantee at least 1 genuine >=1350-word non-roundup deep article if available deeper in feed
                if (
                    ed_ai_briefing_enabled
                    and fetch_strategy != "newsnow_digest"
                    and src_role not in ("anchor", "digest")
                    and per_limit >= 2
                    and len(source_picked) >= per_limit
                    and not any(
                        a.word_count >= 1350 and not _is_roundup_article(a, src_role)
                        for a in source_picked
                    )
                ):
                    for extra_cand in eligible_for_src[per_limit:]:
                        if extra_cand.word_count >= 1350 and not _is_roundup_article(
                            extra_cand, src_role
                        ):
                            source_picked[-1] = extra_cand
                            break

                for art in source_picked:
                    selected_articles.append(art)
                    seen_ids.add(art.id)
                    if src_role:
                        role_by_art_id[art.id] = src_role
                    if ai_opts.get("summarize"):
                        summarize_ids.add(art.id)
                    if ai_opts.get("bilingual"):
                        bilingual_ids.add(art.id)
                    if len(selected_articles) >= raw_selection_cap:
                        break
                if len(selected_articles) >= raw_selection_cap:
                    break

            if not selected_articles:
                deliv = await sel_session.get(Delivery, delivery_id)
                ed_row = await sel_session.get(Edition, edition_id)
                if deliv is not None:
                    deliv.status = "skipped"
                    deliv.error_message = "本期时间窗口内暂无未推送的新文章"
                    deliv.duration_ms = int((time.monotonic() - started) * 1000)
                    deliv.stage_timings = stage_timings
                    deliv.finished_at = utcnow()
                if send_email and ed_row is not None:
                    ed_row.last_run_at = utcnow()
                await sel_session.commit()
                await publish_event(
                    "delivery.completed",
                    {"delivery_id": delivery_id, "status": "skipped", "title": ed_name},
                )
                return {"delivery_id": delivery_id, "status": "skipped", "article_count": 0}

        # Partition into 3-tier layout when AI editorial curation is enabled:
        #   1) Anchor full-issue chapters (e.g. 喷嚏图卦)
        #   2) AI Curated Illustrated Digest (merges hotlists, flashes, short news & roundups + deduplicates)
        #   3) Selected non-duplicate deep long-form reads (e.g. 少数派 / 天下杂志 / 联合早报社论)
        anchor_articles: list[Article] = []
        digest_pool_articles: list[Article] = []
        deep_selected: list[Article] = []
        excluded_topics_for_digest: list[str] = []

        if ed_ai_briefing_enabled:
            deep_candidates: list[Article] = []
            for art in selected_articles:
                role = role_by_art_id.get(art.id, "")
                if _is_anchor_tugua_article(art, role):
                    anchor_articles.append(art)
                    excluded_topics_for_digest.extend(
                        extract_tugua_topics(art.plain_text, art.title)
                    )
                elif (
                    _is_digest_bundle_article(art)
                    or role == "digest"
                    or _is_roundup_article(art, role)
                    or (role != "deep" and art.word_count < 1350)
                ):
                    digest_pool_articles.append(art)
                else:
                    deep_candidates.append(art)

            # Round-robin across sources so a single source cannot monopolize all deep-read slots
            buckets_by_src: dict[int | None, list[Article]] = {}
            for cand_art in deep_candidates:
                buckets_by_src.setdefault(cand_art.source_id, []).append(cand_art)
            interleaved_deep: list[Article] = []
            max_bucket_len = max((len(b) for b in buckets_by_src.values()), default=0)
            for depth_idx in range(max_bucket_len):
                for bucket in buckets_by_src.values():
                    if depth_idx < len(bucket):
                        interleaved_deep.append(bucket[depth_idx])

            max_deep = min(5, max(2, ed_max_articles // 5))
            accepted_deep_titles: list[str] = list(excluded_topics_for_digest)
            for cand_art in interleaved_deep:
                if len(deep_selected) < max_deep and not is_duplicate_topic(
                    cand_art.title, accepted_deep_titles, threshold=0.36
                ):
                    deep_selected.append(cand_art)
                    accepted_deep_titles.append(cand_art.title)
                    summarize_ids.add(cand_art.id)
                else:
                    digest_pool_articles.append(cand_art)
            excluded_topics_for_digest = accepted_deep_titles
        else:
            deep_selected = list(selected_articles[:ed_max_articles])

        # Phase 4: Optional AI enrichment & illustrated digest curation IN PARALLEL (NO DB tx held open!)
        t1 = time.monotonic()
        curated_digest: CuratedDigestResult | None = None
        ai_client = await _make_ai_client(ai_cfg, net_cfg["outbound_proxy"])
        try:
            if ai_client is not None:
                await publish_event(
                    "delivery.stage",
                    {"delivery_id": delivery_id, "edition_id": edition_id, "stage": "ai_editorial"},
                )

                async def _enrich_branch() -> None:
                    standalone_arts = anchor_articles + deep_selected
                    if standalone_arts:
                        await enrich_articles_with_ai(
                            ai_client,
                            standalone_arts,
                            summarize_ids=summarize_ids,
                            bilingual_ids=bilingual_ids,
                        )

                async def _curate_branch() -> CuratedDigestResult | None:
                    if ed_ai_briefing_enabled and digest_pool_articles:
                        cands = extract_candidates_from_articles(digest_pool_articles)
                        return await curate_illustrated_digest(
                            ai_client,
                            cands,
                            excluded_topics=excluded_topics_for_digest,
                            max_items=20,
                        )
                    return None

                async def _run_ai_stage() -> CuratedDigestResult | None:
                    _enr, dig = await asyncio.gather(
                        _enrich_branch(),
                        _curate_branch(),
                        return_exceptions=True,
                    )
                    if isinstance(_enr, BaseException):
                        logger.warning("ai_enrich_branch_degraded", err=str(_enr))
                    if isinstance(dig, BaseException):
                        logger.warning("ai_curate_branch_degraded", err=str(dig))
                        return None
                    return dig

                try:
                    curated_digest = await asyncio.wait_for(
                        _run_ai_stage(), timeout=AI_EDITORIAL_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    logger.warning(
                        "ai_editorial_timeout",
                        edition_id=edition_id,
                        timeout_seconds=AI_EDITORIAL_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logger.warning("ai_editorial_degraded", edition_id=edition_id, err=str(exc))

                # Persist any AI fields generated back to DB in a short transaction
                async with factory() as ai_save_session:
                    for art in anchor_articles + deep_selected:
                        db_art = await ai_save_session.get(Article, art.id)
                        if db_art is not None:
                            if art.ai_summary and not db_art.ai_summary:
                                db_art.ai_summary = art.ai_summary
                            if art.translated_title and not db_art.translated_title:
                                db_art.translated_title = art.translated_title
                            if art.bilingual_html and not db_art.bilingual_html:
                                db_art.bilingual_html = art.bilingual_html
                    await ai_save_session.commit()

            # Deterministic deduplicated digest fallback when AI is disabled or timed out
            if ed_ai_briefing_enabled and digest_pool_articles and curated_digest is None:
                cands = extract_candidates_from_articles(digest_pool_articles)
                curated_digest = await curate_illustrated_digest(
                    None,
                    cands,
                    excluded_topics=excluded_topics_for_digest,
                    max_items=15,
                )
        finally:
            if ai_client is not None:
                await ai_client.close()
        stage_timings["ai_ms"] = int((time.monotonic() - t1) * 1000)

        # Phase 5: Build strict Kindle EPUB3 volume(s) (NO DB transaction held open!)
        await publish_event(
            "delivery.stage",
            {"delivery_id": delivery_id, "edition_id": edition_id, "stage": "building_epub"},
        )
        t2 = time.monotonic()
        try:
            tz = ZoneInfo(ed_timezone)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        now_local = datetime.now(tz)

        def _to_chapter_input(art: Article) -> EpubChapterInput:
            return EpubChapterInput(
                title=art.translated_title or art.title,
                source_name=art.source_name,
                category=art.category,
                author=art.author,
                published_at=art.published_at.astimezone(tz)
                if art.published_at.tzinfo is not None
                else art.published_at.replace(tzinfo=tz),
                canonical_url=art.canonical_url,
                body_html=art.clean_html,
                ai_summary=art.ai_summary,
                bilingual_html=art.bilingual_html,
                language=art.language,
                reading_time_min=art.reading_time_min,
            )

        chapters_input: list[EpubChapterInput] = []
        if ed_ai_briefing_enabled:
            for art in anchor_articles:
                chapters_input.append(_to_chapter_input(art))
            if curated_digest is not None and curated_digest.item_count > 0 and curated_digest.body_html:
                chapters_input.append(
                    EpubChapterInput(
                        title=f"【今日全网图文辑要】{now_local.strftime('%m-%d')} 精选",
                        source_name="AI 跨源精编",
                        category="图卦辑要",
                        author=f"收录 {curated_digest.item_count} 条去重精编",
                        published_at=now_local,
                        canonical_url=f"https://inkpress.local/curated/{now_local.strftime('%Y-%m-%d')}",
                        body_html=curated_digest.body_html,
                        language="zh-CN",
                        image_referers=curated_digest.image_referers,
                    )
                )
            for art in deep_selected:
                chapters_input.append(_to_chapter_input(art))
        else:
            chapters_input = [_to_chapter_input(art) for art in deep_selected]

        if not chapters_input:
            chapters_input = [_to_chapter_input(art) for art in selected_articles[:ed_max_articles]]

        volumes = await build_kindle_edition_epubs(
            output_dir=settings.library_data_dir / "editions",
            edition_title=ed_name,
            subtitle=ed_subtitle,
            issue_number=next_issue,
            issue_date=now_local,
            chapters_input=chapters_input,
            briefing_html="",
            cover_style=ed_cover_style,
            max_mb_per_volume=ed_max_mb_per_volume,
            grayscale_images=ed_grayscale_images,
            include_qr_links=ed_include_qr_links,
            strip_links=ed_strip_links,
            outbound_proxy=net_cfg["outbound_proxy"],
        )
        mobi_by_epub: dict[Path, Path | None] = {}
        for vol in volumes:
            mobi_by_epub[vol.path] = await build_companion_kindle_mobi(vol.path)
        stage_timings["epub_ms"] = int((time.monotonic() - t2) * 1000)

        # Phase 6: Archive generated books & attach delivery.book_id + full retry metadata BEFORE SMTP
        saved_book_ids: list[int] = []
        selected_article_ids = [art.id for art in selected_articles]
        total_size_bytes = sum(v.size_bytes for v in volumes)
        primary_title = volumes[0].title

        async with factory() as archive_session:
            saved_books: list[Book] = []
            for vol in volumes:
                cover_file = settings.library_data_dir / "covers" / f"{vol.path.stem}.jpg"
                cover_file.write_bytes(vol.cover_bytes)
                vol_mobi = mobi_by_epub.get(vol.path)
                vol_meta: dict[str, Any] = {
                    "issue_number": next_issue,
                    "volume_index": vol.volume_index,
                    "total_volumes": vol.total_volumes,
                    "article_count": vol.article_count,
                    "is_preview": not send_email,
                }
                if vol_mobi is not None:
                    vol_meta["kindle_mobi_path"] = str(vol_mobi)
                book = Book(
                    kind="edition_issue",
                    edition_id=edition_id,
                    title=vol.title,
                    author=ed_name,
                    description=f"第 {next_issue} 期 · 收录 {vol.article_count} 篇文章",
                    language="zh-CN",
                    original_format="epub",
                    original_path=str(vol.path),
                    kindle_epub_path=str(vol.path),
                    cover_path=str(cover_file),
                    file_size_bytes=vol.size_bytes,
                    conversion_status="ready",
                    metadata_json=vol_meta,
                )
                archive_session.add(book)
                saved_books.append(book)
            await archive_session.flush()
            saved_book_ids = [b.id for b in saved_books]
            if saved_books:
                primary_meta = dict(saved_books[0].metadata_json or {})
                primary_meta["volume_book_ids"] = saved_book_ids
                primary_meta["article_ids"] = selected_article_ids
                primary_meta["sent_book_ids"] = []
                saved_books[0].metadata_json = primary_meta

            deliv = await archive_session.get(Delivery, delivery_id)
            if deliv is not None:
                deliv.book_id = saved_book_ids[0] if saved_book_ids else None
                deliv.title = primary_title
                deliv.article_count = len(chapters_input)
                deliv.volume_count = len(volumes)
                deliv.file_size_bytes = total_size_bytes
                deliv.stage_timings = dict(stage_timings)
                if send_email:
                    deliv.status = "sending_smtp"
            await archive_session.commit()

        # Phase 7: Deliver via SMTP if enabled & configured (NO DB transaction held open!)
        t3 = time.monotonic()
        sent_book_ids: list[int] = []
        if send_email:
            if not recipients:
                raise ValueError("未配置 Kindle 收件邮箱，请在工作室设置或刊物中填写")
            await publish_event(
                "delivery.stage",
                {"delivery_id": delivery_id, "edition_id": edition_id, "stage": "sending_smtp"},
            )
            try:
                for bid, vol in zip(saved_book_ids, volumes, strict=False):
                    await send_ebook_via_smtp(
                        smtp_config=smtp_cfg,
                        recipients=recipients,
                        subject=f"InkPress · {vol.title}",
                        body_text=f"Delivered by InkPress ({vol.article_count} articles).",
                        attachments=[vol.path],
                    )
                    sent_book_ids.append(bid)
            except Exception:
                if sent_book_ids and saved_book_ids:
                    async with factory() as partial_session:
                        pb = await partial_session.get(Book, saved_book_ids[0])
                        if pb is not None:
                            pmeta = dict(pb.metadata_json or {})
                            pmeta["sent_book_ids"] = list(sent_book_ids)
                            pb.metadata_json = pmeta
                            await partial_session.commit()
                raise
        stage_timings["smtp_ms"] = int((time.monotonic() - t3) * 1000)

        # Phase 8: Finalize DB state (preview `send_email=False` -> status="generated", no counter/ledger pollution!)
        final_status = "sent" if send_email else "generated"
        async with factory() as final_session:
            now_done = utcnow()
            if send_email:
                for bid in saved_book_ids:
                    b_row = await final_session.get(Book, bid)
                    if b_row is not None:
                        b_row.pushed_count += 1
                        b_row.last_pushed_at = now_done
                        if bid == saved_book_ids[0]:
                            bmeta = dict(b_row.metadata_json or {})
                            bmeta["sent_book_ids"] = list(saved_book_ids)
                            b_row.metadata_json = bmeta
                await _record_article_deliveries(
                    final_session,
                    edition_id=edition_id,
                    delivery_id=delivery_id,
                    article_ids=selected_article_ids,
                )
                await _record_digest_delivery_snapshots(
                    final_session,
                    edition_id=edition_id,
                    article_ids=selected_article_ids,
                )
                ed_row = await final_session.get(Edition, edition_id)
                if ed_row is not None:
                    ed_row.issue_counter = next_issue
                    ed_row.last_run_at = now_done

            deliv = await final_session.get(Delivery, delivery_id)
            if deliv is not None:
                deliv.status = final_status
                deliv.duration_ms = int((time.monotonic() - started) * 1000)
                deliv.stage_timings = stage_timings
                deliv.finished_at = now_done
            await final_session.commit()

        await publish_event(
            "delivery.completed",
            {
                "delivery_id": delivery_id,
                "edition_id": edition_id,
                "status": final_status,
                "title": primary_title,
                "article_count": len(chapters_input),
                "size_bytes": total_size_bytes,
            },
        )
        return {
            "delivery_id": delivery_id,
            "status": final_status,
            "title": primary_title,
            "article_count": len(chapters_input),
            "volume_count": len(volumes),
            "size_bytes": total_size_bytes,
            "book_id": saved_book_ids[0] if saved_book_ids else None,
        }
    except Exception as exc:
        async with factory() as err_session:
            try:
                deliv = await err_session.get(Delivery, delivery_id)
                if deliv is not None:
                    deliv.status = "failed"
                    deliv.error_message = str(exc)[:2000]
                    deliv.duration_ms = int((time.monotonic() - started) * 1000)
                    deliv.stage_timings = stage_timings
                    deliv.finished_at = utcnow()
                    await err_session.commit()
            except Exception:
                await err_session.rollback()
        await publish_event(
            "delivery.failed",
            {
                "delivery_id": delivery_id,
                "edition_id": edition_id,
                "title": ed_name,
                "error": str(exc),
            },
        )
        logger.exception("edition_delivery_failed", edition_id=edition_id, delivery_id=delivery_id)
        raise


async def run_build_and_deliver_edition(
    edition_id: int,
    *,
    trigger: str = "manual",
    send_email: bool = True,
    ignore_dedup: bool = False,
    delivery_id: int | None = None,
) -> dict[str, Any]:
    """End-to-end edition pipeline guarded by per-edition mutex."""
    wait_sec = 120.0 if trigger == "cron" else 0.0
    async with _edition_mutex(edition_id, wait_seconds=wait_sec) as acquired:
        if not acquired:
            logger.warning(
                "edition_delivery_skipped_locked",
                edition_id=edition_id,
                trigger=trigger,
            )
            if delivery_id is not None:
                factory = get_session_factory()
                async with factory() as session:
                    deliv = await session.get(Delivery, delivery_id)
                    if deliv is not None and deliv.status == "pending":
                        deliv.status = "skipped"
                        deliv.error_message = "该刊物正在执行中，已跳过重复触发"
                        deliv.finished_at = utcnow()
                        await session.commit()
            return {
                "status": "skipped",
                "reason": "edition_locked",
                "edition_id": edition_id,
                "delivery_id": delivery_id,
            }
        return await _run_build_and_deliver_edition_unlocked(
            edition_id,
            trigger=trigger,
            send_email=send_email,
            ignore_dedup=ignore_dedup,
            delivery_id=delivery_id,
        )


async def run_convert_book(book_id: int) -> dict[str, Any]:
    settings = get_settings()
    settings.ensure_storage_dirs()
    factory = get_session_factory()

    async with factory() as session:
        book = await session.get(Book, book_id)
        if book is None:
            return {"error": "book_not_found", "book_id": book_id}
        book.conversion_status = "converting"
        book.conversion_error = None
        orig_path = Path(book.original_path)
        meta = dict(book.metadata_json or {})
        has_custom_title = bool(meta.get("has_custom_title"))
        title_override = book.title if has_custom_title else ""
        author_override = book.author
        book_title_for_event = book.title
        await session.commit()

    await publish_event("book.convert.started", {"book_id": book_id, "title": book_title_for_event})
    try:
        res = await convert_book_to_kindle_epub(
            orig_path,
            output_dir=settings.library_data_dir / "epubs",
            cover_dir=settings.library_data_dir / "covers",
            title_override=title_override,
            author_override=author_override,
        )
        mobi_out = await build_companion_kindle_mobi(res.kindle_epub_path)
        async with factory() as save_session:
            book = await save_session.get(Book, book_id)
            if book is None:
                return {"error": "book_not_found", "book_id": book_id}
            book.title = res.title[:500]
            book.author = res.author[:250]
            book.language = res.language[:16]
            book.kindle_epub_path = str(res.kindle_epub_path)
            book.cover_path = str(res.cover_path) if res.cover_path else None
            book.file_size_bytes = res.file_size_bytes
            book.conversion_status = "ready"
            updated_meta = dict(book.metadata_json or {})
            updated_meta.pop("chapters_cache", None)
            updated_meta.pop("chapters_cache_sig", None)
            updated_meta["chapter_count"] = res.chapter_count
            updated_meta["conversion_notes"] = res.notes
            if mobi_out is not None:
                updated_meta["kindle_mobi_path"] = str(mobi_out)
            else:
                updated_meta.pop("kindle_mobi_path", None)
            book.metadata_json = updated_meta
            final_title = book.title
            final_size = book.file_size_bytes
            await save_session.commit()

        await publish_event(
            "book.convert.completed",
            {"book_id": book_id, "title": final_title, "size_bytes": final_size},
        )
        return {"book_id": book_id, "status": "ready", "notes": res.notes}
    except Exception as exc:
        async with factory() as err_session:
            try:
                book = await err_session.get(Book, book_id)
                if book is not None:
                    book.conversion_status = "failed"
                    book.conversion_error = str(exc)[:1500]
                    await err_session.commit()
            except Exception:
                await err_session.rollback()
        await publish_event(
            "book.convert.failed",
            {"book_id": book_id, "title": book_title_for_event, "error": str(exc)},
        )
        raise


async def run_deliver_book(
    book_id: int,
    *,
    target_email: str = "",
    trigger: str = "manual",
) -> dict[str, Any]:
    factory = get_session_factory()
    started = time.monotonic()

    async with factory() as session:
        book = await session.get(Book, book_id)
        if book is None:
            return {"error": "book_not_found", "book_id": book_id}
        if book.conversion_status != "ready" or not book.kindle_epub_path:
            raise ValueError("电子书尚未完成格式转换，无法推送")

        smtp_cfg = await get_smtp_config(session)
        raw_targets = target_email.strip() or str(smtp_cfg.get("default_kindle_email") or "")
        recipients = parse_recipients(raw_targets)
        if not recipients:
            raise ValueError("未配置 Kindle 收件邮箱")

        file_path = Path(book.kindle_epub_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Kindle file missing: {file_path}")

        book_title = book.title
        delivery = Delivery(
            book_id=book.id,
            title=book_title,
            trigger=trigger,
            target_email=", ".join(recipients),
            status="sending_smtp",
            article_count=int((book.metadata_json or {}).get("chapter_count", 1)),
            volume_count=1,
            file_size_bytes=file_path.stat().st_size,
        )
        session.add(delivery)
        await session.commit()
        delivery_id = delivery.id

    try:
        await send_ebook_via_smtp(
            smtp_config=smtp_cfg,
            recipients=recipients,
            subject=f"InkPress · {book_title}",
            body_text=f"Book delivered from InkPress Library: {book_title}",
            attachments=[file_path],
        )
        async with factory() as done_session:
            book_row = await done_session.get(Book, book_id)
            if book_row is not None:
                book_row.pushed_count = (book_row.pushed_count or 0) + 1
                book_row.last_pushed_at = utcnow()
                if trigger == "scheduled":
                    book_row.scheduled_push_at = None
            deliv_row = await done_session.get(Delivery, delivery_id)
            if deliv_row is not None:
                deliv_row.status = "sent"
                deliv_row.duration_ms = int((time.monotonic() - started) * 1000)
                deliv_row.finished_at = utcnow()
            await done_session.commit()

        await publish_event(
            "delivery.completed",
            {
                "delivery_id": delivery_id,
                "book_id": book_id,
                "status": "sent",
                "title": book_title,
            },
        )
        return {"delivery_id": delivery_id, "book_id": book_id, "status": "sent"}
    except Exception as exc:
        async with factory() as err_session:
            try:
                deliv_row = await err_session.get(Delivery, delivery_id)
                if deliv_row is not None:
                    deliv_row.status = "failed"
                    deliv_row.error_message = str(exc)[:2000]
                    deliv_row.duration_ms = int((time.monotonic() - started) * 1000)
                    deliv_row.finished_at = utcnow()
                    await err_session.commit()
            except Exception:
                await err_session.rollback()
        await publish_event(
            "delivery.failed",
            {
                "delivery_id": delivery_id,
                "book_id": book_id,
                "title": book_title,
                "error": str(exc),
            },
        )
        raise


async def run_retry_delivery(
    delivery_id: int,
    *,
    trigger: str = "retry",
) -> dict[str, Any]:
    """Retry a failed delivery in-place, preserving multi-volume attachments and closing the dedup ledger."""
    factory = get_session_factory()
    started = time.monotonic()

    async with factory() as session:
        deliv = await session.get(Delivery, delivery_id)
        if deliv is None:
            return {"error": "delivery_not_found", "delivery_id": delivery_id}

        # Failed before Phase 6 built any EPUB -> re-run full pipeline reusing the same Delivery row
        if deliv.book_id is None:
            if deliv.edition_id is not None:
                ed_id = deliv.edition_id
                return await run_build_and_deliver_edition(
                    ed_id,
                    trigger=trigger,
                    send_email=True,
                    delivery_id=deliv.id,
                )
            raise ValueError("该投递记录无关联书籍或刊物，无法重试")

        primary_book = await session.get(Book, deliv.book_id)
        if primary_book is None:
            raise FileNotFoundError(f"关联书籍记录不存在 (book_id={deliv.book_id})")

        smtp_cfg = await get_smtp_config(session)
        ed_targets = ""
        if deliv.edition_id is not None:
            ed_obj = await session.get(Edition, deliv.edition_id)
            if ed_obj is not None:
                ed_targets = ed_obj.target_emails.strip()

        raw_targets = (
            deliv.target_email.strip()
            or ed_targets
            or str(smtp_cfg.get("default_kindle_email") or "")
        )
        recipients = parse_recipients(raw_targets)
        if not recipients:
            raise ValueError("未配置 Kindle 收件邮箱")

        meta = dict(primary_book.metadata_json or {})
        raw_vol_ids = meta.get("volume_book_ids")
        volume_book_ids = (
            [int(x) for x in raw_vol_ids if str(x).isdigit() or isinstance(x, int)]
            if isinstance(raw_vol_ids, list) and raw_vol_ids
            else [primary_book.id]
        )
        sent_bids = {
            int(x)
            for x in (meta.get("sent_book_ids") or [])
            if str(x).isdigit() or isinstance(x, int)
        }
        article_ids = [
            int(x)
            for x in (meta.get("article_ids") or [])
            if str(x).isdigit() or isinstance(x, int)
        ]
        issue_number = int(meta.get("issue_number") or 0)
        extra_vol_paths = [
            Path(str(p)) for p in (meta.get("volume_paths") or []) if str(p).strip()
        ]

        send_plan: list[tuple[int | None, str, Path]] = []
        if len(volume_book_ids) > 1 or not extra_vol_paths:
            for bid in volume_book_ids:
                b_obj = primary_book if bid == primary_book.id else await session.get(Book, bid)
                if b_obj is None or not b_obj.kindle_epub_path:
                    raise FileNotFoundError(f"分卷图书记录缺失 (book_id={bid})")
                p = Path(b_obj.kindle_epub_path)
                if not p.exists():
                    raise FileNotFoundError(f"EPUB 文件不存在: {p}")
                if bid not in sent_bids:
                    send_plan.append((bid, b_obj.title, p))
        else:
            for v_idx, p in enumerate(extra_vol_paths, start=1):
                if not p.exists():
                    raise FileNotFoundError(f"EPUB 分卷文件不存在: {p}")
                v_label = (
                    f"{primary_book.title} ({v_idx}/{len(extra_vol_paths)})"
                    if len(extra_vol_paths) > 1
                    else primary_book.title
                )
                send_plan.append((primary_book.id if v_idx == 1 else None, v_label, p))

        deliv_title = deliv.title
        edition_id = deliv.edition_id
        primary_book_id = primary_book.id
        total_volumes = max(len(volume_book_ids), len(extra_vol_paths), 1)

        deliv.status = "sending_smtp"
        deliv.trigger = trigger
        deliv.target_email = ", ".join(recipients)
        deliv.error_message = None
        deliv.finished_at = None
        await session.commit()

    await publish_event(
        "delivery.stage",
        {"delivery_id": delivery_id, "edition_id": edition_id, "stage": "sending_smtp"},
    )

    newly_sent_bids: list[int] = []
    try:
        for sent_bid, vol_title, vol_path in send_plan:
            await send_ebook_via_smtp(
                smtp_config=smtp_cfg,
                recipients=recipients,
                subject=f"InkPress · {vol_title}",
                body_text=f"Retried and delivered by InkPress: {vol_title}",
                attachments=[vol_path],
            )
            if sent_bid is not None:
                newly_sent_bids.append(sent_bid)

        async with factory() as done_session:
            now_done = utcnow()
            all_sent_bids = list(dict.fromkeys([*sent_bids, *newly_sent_bids, *volume_book_ids]))
            for bid in volume_book_ids:
                b_row = await done_session.get(Book, bid)
                if b_row is not None:
                    b_row.pushed_count = (b_row.pushed_count or 0) + 1
                    b_row.last_pushed_at = now_done
                    if bid == primary_book_id:
                        bmeta = dict(b_row.metadata_json or {})
                        bmeta["sent_book_ids"] = all_sent_bids
                        b_row.metadata_json = bmeta

            if edition_id is not None:
                if article_ids:
                    await _record_article_deliveries(
                        done_session,
                        edition_id=edition_id,
                        delivery_id=delivery_id,
                        article_ids=article_ids,
                    )
                    await _record_digest_delivery_snapshots(
                        done_session,
                        edition_id=edition_id,
                        article_ids=article_ids,
                    )
                ed_row = await done_session.get(Edition, edition_id)
                if ed_row is not None:
                    if issue_number > (ed_row.issue_counter or 0):
                        ed_row.issue_counter = issue_number
                    ed_row.last_run_at = now_done

            deliv_row = await done_session.get(Delivery, delivery_id)
            if deliv_row is not None:
                deliv_row.status = "sent"
                deliv_row.error_message = None
                deliv_row.volume_count = total_volumes
                deliv_row.duration_ms = int((time.monotonic() - started) * 1000)
                deliv_row.finished_at = now_done
            await done_session.commit()

        await publish_event(
            "delivery.completed",
            {
                "delivery_id": delivery_id,
                "edition_id": edition_id,
                "book_id": primary_book_id,
                "status": "sent",
                "title": deliv_title,
            },
        )
        return {
            "delivery_id": delivery_id,
            "edition_id": edition_id,
            "book_id": primary_book_id,
            "volume_count": total_volumes,
            "status": "sent",
        }
    except Exception as exc:
        async with factory() as err_session:
            try:
                if newly_sent_bids:
                    pb_err = await err_session.get(Book, primary_book_id)
                    if pb_err is not None:
                        emeta = dict(pb_err.metadata_json or {})
                        emeta["sent_book_ids"] = list(dict.fromkeys([*sent_bids, *newly_sent_bids]))
                        pb_err.metadata_json = emeta
                deliv_row = await err_session.get(Delivery, delivery_id)
                if deliv_row is not None:
                    deliv_row.status = "failed"
                    deliv_row.error_message = str(exc)[:2000]
                    deliv_row.duration_ms = int((time.monotonic() - started) * 1000)
                    deliv_row.finished_at = utcnow()
                await err_session.commit()
            except Exception:
                await err_session.rollback()
        await publish_event(
            "delivery.failed",
            {
                "delivery_id": delivery_id,
                "edition_id": edition_id,
                "book_id": primary_book_id,
                "title": deliv_title,
                "error": str(exc),
            },
        )
        raise


async def run_sync_source_bg(source_id: int) -> None:
    try:
        await run_sync_source(source_id)
    except Exception as exc:
        logger.warning("bg_sync_source_error", source_id=source_id, err=str(exc))


async def run_build_and_deliver_edition_bg(
    edition_id: int,
    *,
    trigger: str = "manual",
    send_email: bool = True,
    ignore_dedup: bool = False,
    delivery_id: int | None = None,
) -> None:
    try:
        await run_build_and_deliver_edition(
            edition_id,
            trigger=trigger,
            send_email=send_email,
            ignore_dedup=ignore_dedup,
            delivery_id=delivery_id,
        )
    except Exception as exc:
        logger.warning("bg_edition_error", edition_id=edition_id, err=str(exc))


async def run_convert_book_bg(book_id: int) -> None:
    try:
        await run_convert_book(book_id)
    except Exception as exc:
        logger.warning("bg_convert_book_error", book_id=book_id, err=str(exc))


async def run_deliver_book_bg(
    book_id: int,
    *,
    target_email: str = "",
    trigger: str = "manual",
) -> None:
    try:
        await run_deliver_book(book_id, target_email=target_email, trigger=trigger)
    except Exception as exc:
        logger.warning("bg_deliver_book_error", book_id=book_id, err=str(exc))


async def run_retry_delivery_bg(delivery_id: int, *, trigger: str = "retry") -> None:
    try:
        await run_retry_delivery(delivery_id, trigger=trigger)
    except Exception as exc:
        logger.warning("bg_retry_delivery_error", delivery_id=delivery_id, err=str(exc))


class _JobHandle:
    def __init__(self, job_type: str) -> None:
        self.job_type = job_type

    async def kiq(self, *args: Any, **kwargs: Any) -> str:
        mapped = dict(kwargs)
        if self.job_type == "sync_source" and args:
            mapped["source_id"] = args[0]
        elif self.job_type == "build_and_deliver_edition" and args:
            mapped["edition_id"] = args[0]
        elif (self.job_type == "convert_book" and args) or (
            self.job_type == "deliver_book" and args
        ):
            mapped["book_id"] = args[0]
        elif self.job_type == "retry_delivery" and args:
            mapped["delivery_id"] = args[0]
        return await enqueue_job(self.job_type, mapped)


sync_source_task = _JobHandle("sync_source")
build_and_deliver_edition_task = _JobHandle("build_and_deliver_edition")
convert_book_task = _JobHandle("convert_book")
deliver_book_task = _JobHandle("deliver_book")
retry_delivery_task = _JobHandle("retry_delivery")
