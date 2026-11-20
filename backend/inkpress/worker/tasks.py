from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inkpress.ai.client import AIClient
from inkpress.ai.editorial import enrich_articles_with_ai, generate_frontpage_briefing
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
from inkpress.publishing.book_converter import convert_book_to_kindle_epub
from inkpress.publishing.epub_builder import EpubChapterInput, build_kindle_edition_epubs
from inkpress.settings_store import get_ai_config, get_network_config, get_smtp_config
from inkpress.worker.broker import enqueue_job, publish_event

logger = get_logger(__name__)

AI_EDITORIAL_TIMEOUT_SECONDS = 180.0


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
        timeout_seconds=int(ai_cfg.get("timeout_seconds", 60)),
    )


async def _record_article_deliveries(
    session: AsyncSession,
    *,
    edition_id: int,
    delivery_id: int,
    article_ids: list[int],
) -> None:
    """Record delivered articles in the dedup ledger while safely skipping existing duplicates."""
    if not article_ids:
        return
    now = utcnow()
    rows = [
        {
            "edition_id": edition_id,
            "article_id": aid,
            "delivery_id": delivery_id,
            "delivered_at": now,
        }
        for aid in article_ids
    ]
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        stmt = (
            pg_insert(ArticleDelivery)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_article_delivery_edition_article")
        )
        await session.execute(stmt)
        return
    if dialect == "sqlite":
        stmt_sq = (
            sqlite_insert(ArticleDelivery)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["edition_id", "article_id"])
        )
        await session.execute(stmt_sq)
        return

    existing = set(
        (
            await session.execute(
                select(ArticleDelivery.article_id).where(
                    ArticleDelivery.edition_id == edition_id,
                    ArticleDelivery.article_id.in_(article_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    for aid in article_ids:
        if aid not in existing:
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


async def run_build_and_deliver_edition(
    edition_id: int,
    *,
    trigger: str = "manual",
    send_email: bool = True,
) -> dict[str, Any]:
    """End-to-end edition pipeline using short DB transactions across slow I/O phases."""
    settings = get_settings()
    settings.ensure_storage_dirs()
    factory = get_session_factory()
    started = time.monotonic()
    stage_timings: dict[str, int] = {}

    # Phase 1: Load edition/settings snapshot & create running delivery record
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

        active_source_specs: list[tuple[int, int, dict[str, Any]]] = []
        for link in edition.source_links:
            if link.source is not None and link.source.enabled:
                per_limit = link.max_items_override or link.source.max_items or 12
                active_source_specs.append((link.source.id, per_limit, dict(link.source.ai_options or {})))

        delivery = Delivery(
            edition_id=edition.id,
            title=ed_name,
            trigger=trigger,
            target_email=", ".join(recipients),
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
        # Phase 2: Sync all bound enabled sources (isolated short session per source)
        t0 = time.monotonic()
        for src_id, _per_limit, _ai_opts in active_source_specs:
            async with factory() as sync_session:
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
        stage_timings["sync_ms"] = int((time.monotonic() - t0) * 1000)

        # Phase 3: Select fresh, undelivered articles within lookback window
        selected_articles: list[Article] = []
        summarize_ids: set[int] = set()
        bilingual_ids: set[int] = set()
        seen_ids: set[int] = set()

        async with factory() as sel_session:
            cutoff_pub = utcnow() - timedelta(hours=max(1, ed_lookback_hours))
            cutoff_dedup = utcnow() - timedelta(days=max(1, ed_dedup_window_days))

            already_sent_ids = set(
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

            for src_id, per_limit, ai_opts in active_source_specs:
                rows = (
                    (
                        await sel_session.execute(
                            select(Article)
                            .where(
                                Article.source_id == src_id,
                                Article.fetched_at >= cutoff_pub,
                            )
                            .order_by(desc(Article.published_at), desc(Article.id))
                            .limit(per_limit * 2)
                        )
                    )
                    .scalars()
                    .all()
                )
                taken = 0
                for art in rows:
                    if art.id in already_sent_ids or art.id in seen_ids:
                        continue
                    selected_articles.append(art)
                    seen_ids.add(art.id)
                    if ai_opts.get("summarize"):
                        summarize_ids.add(art.id)
                    if ai_opts.get("bilingual"):
                        bilingual_ids.add(art.id)
                    taken += 1
                    if taken >= per_limit or len(selected_articles) >= ed_max_articles:
                        break
                if len(selected_articles) >= ed_max_articles:
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
                if ed_row is not None:
                    ed_row.last_run_at = utcnow()
                await sel_session.commit()
                await publish_event(
                    "delivery.completed",
                    {"delivery_id": delivery_id, "status": "skipped", "title": ed_name},
                )
                return {"delivery_id": delivery_id, "status": "skipped", "article_count": 0}

        # Phase 4: Optional AI enrichment & frontpage briefing (NO DB transaction held open!)
        t1 = time.monotonic()
        briefing_html = ""
        ai_client = await _make_ai_client(ai_cfg, net_cfg["outbound_proxy"])
        if ai_client is not None:
            await publish_event(
                "delivery.stage",
                {"delivery_id": delivery_id, "edition_id": edition_id, "stage": "ai_editorial"},
            )
            try:

                async def _run_ai_stage() -> str:
                    await enrich_articles_with_ai(
                        ai_client,
                        selected_articles,
                        summarize_ids=summarize_ids,
                        bilingual_ids=bilingual_ids,
                    )
                    if ed_ai_briefing_enabled:
                        return await generate_frontpage_briefing(
                            ai_client, ed_name, selected_articles
                        )
                    return ""

                briefing_html = await asyncio.wait_for(
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
            finally:
                await ai_client.close()

            # Persist any AI fields generated back to DB in a short transaction
            async with factory() as ai_save_session:
                for art in selected_articles:
                    db_art = await ai_save_session.get(Article, art.id)
                    if db_art is not None:
                        if art.ai_summary and not db_art.ai_summary:
                            db_art.ai_summary = art.ai_summary
                        if art.translated_title and not db_art.translated_title:
                            db_art.translated_title = art.translated_title
                        if art.bilingual_html and not db_art.bilingual_html:
                            db_art.bilingual_html = art.bilingual_html
                await ai_save_session.commit()
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

        chapters_input = [
            EpubChapterInput(
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
            )
            for art in selected_articles
        ]

        volumes = await build_kindle_edition_epubs(
            output_dir=settings.library_data_dir / "editions",
            edition_title=ed_name,
            subtitle=ed_subtitle,
            issue_number=next_issue,
            issue_date=now_local,
            chapters_input=chapters_input,
            briefing_html=briefing_html,
            cover_style=ed_cover_style,
            max_mb_per_volume=ed_max_mb_per_volume,
            grayscale_images=ed_grayscale_images,
            include_qr_links=ed_include_qr_links,
            strip_links=ed_strip_links,
            outbound_proxy=net_cfg["outbound_proxy"],
        )
        stage_timings["epub_ms"] = int((time.monotonic() - t2) * 1000)

        # Phase 6: Archive generated books & attach delivery.book_id BEFORE SMTP
        # so failed SMTP retries can reuse the already-built EPUB!
        saved_book_ids: list[int] = []
        total_size_bytes = sum(v.size_bytes for v in volumes)
        primary_title = volumes[0].title

        async with factory() as archive_session:
            saved_books: list[Book] = []
            for vol in volumes:
                cover_file = settings.library_data_dir / "covers" / f"{vol.path.stem}.jpg"
                cover_file.write_bytes(vol.cover_bytes)
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
                    metadata_json={
                        "issue_number": next_issue,
                        "volume_index": vol.volume_index,
                        "total_volumes": vol.total_volumes,
                        "article_count": vol.article_count,
                    },
                )
                archive_session.add(book)
                saved_books.append(book)
            await archive_session.flush()
            saved_book_ids = [b.id for b in saved_books]

            deliv = await archive_session.get(Delivery, delivery_id)
            if deliv is not None:
                deliv.book_id = saved_book_ids[0] if saved_book_ids else None
                deliv.title = primary_title
                deliv.article_count = len(selected_articles)
                deliv.volume_count = len(volumes)
                deliv.file_size_bytes = total_size_bytes
                deliv.stage_timings = dict(stage_timings)
            await archive_session.commit()

        # Phase 7: Deliver via SMTP if enabled & configured (NO DB transaction held open!)
        t3 = time.monotonic()
        if send_email:
            if not recipients:
                raise ValueError("未配置 Kindle 收件邮箱，请在工作室设置或刊物中填写")
            await publish_event(
                "delivery.stage",
                {"delivery_id": delivery_id, "edition_id": edition_id, "stage": "sending_smtp"},
            )
            for vol in volumes:
                await send_ebook_via_smtp(
                    smtp_config=smtp_cfg,
                    recipients=recipients,
                    subject=f"InkPress · {vol.title}",
                    body_text=f"Delivered by InkPress ({vol.article_count} articles).",
                    attachments=[vol.path],
                )
        stage_timings["smtp_ms"] = int((time.monotonic() - t3) * 1000)

        # Phase 8: Finalize DB state & dedup ledger (dedup ledger ONLY when send_email=True)
        async with factory() as final_session:
            now_done = utcnow()
            if send_email:
                for bid in saved_book_ids:
                    b_row = await final_session.get(Book, bid)
                    if b_row is not None:
                        b_row.pushed_count += 1
                        b_row.last_pushed_at = now_done
                await _record_article_deliveries(
                    final_session,
                    edition_id=edition_id,
                    delivery_id=delivery_id,
                    article_ids=[art.id for art in selected_articles],
                )

            ed_row = await final_session.get(Edition, edition_id)
            if ed_row is not None:
                ed_row.issue_counter = next_issue
                ed_row.last_run_at = now_done

            deliv = await final_session.get(Delivery, delivery_id)
            if deliv is not None:
                deliv.status = "sent"
                deliv.duration_ms = int((time.monotonic() - started) * 1000)
                deliv.stage_timings = stage_timings
                deliv.finished_at = now_done
            await final_session.commit()

        await publish_event(
            "delivery.completed",
            {
                "delivery_id": delivery_id,
                "edition_id": edition_id,
                "status": "sent",
                "title": primary_title,
                "article_count": len(selected_articles),
                "size_bytes": total_size_bytes,
            },
        )
        return {
            "delivery_id": delivery_id,
            "status": "sent",
            "title": primary_title,
            "article_count": len(selected_articles),
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
            status="running",
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
) -> None:
    try:
        await run_build_and_deliver_edition(edition_id, trigger=trigger, send_email=send_email)
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
        return await enqueue_job(self.job_type, mapped)


sync_source_task = _JobHandle("sync_source")
build_and_deliver_edition_task = _JobHandle("build_and_deliver_edition")
convert_book_task = _JobHandle("convert_book")
deliver_book_task = _JobHandle("deliver_book")
