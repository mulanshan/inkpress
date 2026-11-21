from __future__ import annotations

import asyncio
import html as html_escape
import re
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.config import get_settings
from inkpress.db.models import Article, Source, utcnow
from inkpress.ingestion.extractor import (
    canonicalize_url,
    compute_content_hash,
    detect_language,
    estimate_word_count_and_time,
    extract_from_webpage,
    extract_links_from_list_page,
    sanitize_article_html,
)
from inkpress.ingestion.fetcher import smart_fetch_text
from inkpress.ingestion.newsnow_client import fetch_newsnow_source
from inkpress.ingestion.rss_client import fetch_rss_entries
from inkpress.logging import get_logger
from inkpress.settings_store import get_network_config

logger = get_logger(__name__)


def _safe_http_url(url: str) -> str:
    cleaned = html_escape.unescape((url or "").strip())
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return cleaned
    return ""


def _compile_optional_regex(
    pattern: object,
    *,
    field: str,
    source_id: int | None,
) -> re.Pattern[str] | None:
    if not isinstance(pattern, str):
        return None
    cleaned = pattern.strip()
    if not cleaned:
        return None
    try:
        return re.compile(cleaned, re.IGNORECASE)
    except re.error as exc:
        logger.warning(
            "invalid_source_regex",
            source_id=source_id,
            field=field,
            pattern=cleaned,
            err=str(exc),
        )
        return None


def _matches_title_filters(
    title: str,
    include_re: re.Pattern[str] | None,
    exclude_re: re.Pattern[str] | None,
) -> bool:
    if include_re is not None and not include_re.search(title):
        return False
    return not (exclude_re is not None and exclude_re.search(title))


def _parse_remove_selectors(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if isinstance(s, str) and s.strip()]
    if isinstance(raw, str) and raw.strip():
        return [part.strip() for part in re.split(r"[\n,]+", raw) if part.strip()]
    return []


async def _upsert_article(
    session: AsyncSession,
    *,
    source: Source | None,
    url: str,
    title: str,
    author: str,
    published_at: datetime,
    clean_html: str,
    plain_text: str,
    lead_image_url: str | None,
    language: str,
    category: str,
    source_name: str,
    extra_meta: dict[str, object] | None = None,
    update_existing: bool = False,
) -> tuple[Article, bool]:
    canon = canonicalize_url(url)
    existing = (
        await session.execute(select(Article).where(Article.canonical_url == canon))
    ).scalar_one_or_none()
    if existing is not None:
        if update_existing:
            new_hash = compute_content_hash(title, plain_text)
            words, mins = estimate_word_count_and_time(plain_text)
            content_changed = existing.content_hash != new_hash
            existing.title = title[:500]
            existing.author = author[:250]
            existing.source_name = source_name[:160]
            existing.category = category[:64]
            existing.language = language[:16]
            existing.published_at = published_at
            existing.fetched_at = utcnow()
            existing.lead_image_url = lead_image_url[:2040] if lead_image_url else None
            existing.clean_html = clean_html
            existing.plain_text = plain_text
            existing.content_hash = new_hash
            existing.word_count = words
            existing.reading_time_min = mins
            existing.extra_meta = extra_meta or {}
            if content_changed:
                existing.ai_summary = None
                existing.bilingual_html = None
            await session.flush()
        return existing, False

    content_hash = compute_content_hash(title, plain_text)
    dup_by_hash = (
        await session.execute(
            select(Article).where(
                Article.content_hash == content_hash,
                Article.source_id == (source.id if source else None),
            )
        )
    ).scalar_one_or_none()
    if dup_by_hash is not None:
        return dup_by_hash, False

    words, mins = estimate_word_count_and_time(plain_text)
    article = Article(
        source_id=source.id if source else None,
        canonical_url=canon,
        content_hash=content_hash,
        title=title[:500],
        author=author[:250],
        source_name=source_name[:160],
        category=category[:64],
        language=language[:16],
        published_at=published_at,
        fetched_at=utcnow(),
        lead_image_url=lead_image_url[:2040] if lead_image_url else None,
        clean_html=clean_html,
        plain_text=plain_text,
        word_count=words,
        reading_time_min=mins,
        extra_meta=extra_meta or {},
    )
    session.add(article)
    await session.flush()
    return article, True


async def sync_source(session: AsyncSession, source: Source) -> dict[str, int]:
    """Fetch new items for a Source and persist deduplicated articles."""
    net = await get_network_config(session)
    new_count = 0
    skipped_count = 0
    failed_items = 0

    source_id = source.id
    source_name = source.name
    opts = source.ai_options if isinstance(source.ai_options, dict) else {}
    include_re = _compile_optional_regex(
        opts.get("title_include_regex"),
        field="title_include_regex",
        source_id=source_id,
    )
    exclude_re = _compile_optional_regex(
        opts.get("title_exclude_regex"),
        field="title_exclude_regex",
        source_id=source_id,
    )
    link_selector = str(opts.get("link_selector") or "").strip()
    content_selector = str(opts.get("content_selector") or "").strip()
    remove_selectors = _parse_remove_selectors(opts.get("remove_selectors"))

    try:
        if source.kind == "newsnow":
            raw_entries = await fetch_newsnow_source(
                source.identifier,
                net["newsnow_base_url"],
                latest=True,
            )
            if not raw_entries:
                logger.warning(
                    "newsnow_sync_empty",
                    source_id=source_id,
                    identifier=source.identifier,
                )
                source.last_fetched_at = utcnow()
                source.last_error = (
                    f"NewsNow returned empty items for '{source.identifier}'"
                )
                source.consecutive_failures = (source.consecutive_failures or 0) + 1
                await session.commit()
                return {"new": 0, "skipped": 0, "failed": 0}

            filtered_entries = [
                e for e in raw_entries if _matches_title_filters(e.title, include_re, exclude_re)
            ]
            skipped_count += len(raw_entries) - len(filtered_entries)
            entries = filtered_entries[: source.max_items]

            if source.fetch_strategy == "newsnow_digest":
                # Aggregate real-time ticker / hotlist into one daily digest article updated in place
                if entries:
                    try:
                        tz = ZoneInfo(get_settings().tz)
                    except Exception:
                        tz = ZoneInfo("Asia/Shanghai")
                    now_local = datetime.now(tz)
                    date_slot = now_local.strftime("%Y-%m-%d")
                    digest_url = f"https://inkpress.local/newsnow/{source.identifier}/{date_slot}"
                    blocks: list[str] = []
                    plain_parts: list[str] = []
                    for idx, item in enumerate(entries, start=1):
                        safe_title = html_escape.escape(item.title)
                        safe_url = _safe_http_url(item.url)
                        badge = (
                            f' <span class="meta-tag">[{html_escape.escape(item.info_tag)}]</span>'
                            if item.info_tag
                            else ""
                        )
                        hint_html = (
                            f"<p>{html_escape.escape(item.summary_hint)}</p>"
                            if item.summary_hint
                            else ""
                        )
                        link_html = (
                            f'<p class="source-link"><a href="{html_escape.escape(safe_url, quote=True)}">原文链接</a></p>'
                            if safe_url
                            else ""
                        )
                        blocks.append(
                            f'<div class="digest-item">'
                            f"<h3>{idx}. {safe_title}{badge}</h3>"
                            f"{hint_html}"
                            f"{link_html}"
                            f"</div>"
                        )
                        plain_parts.append(f"{idx}. {item.title} {item.summary_hint}".strip())
                    raw_digest_html = "\n".join(blocks)
                    digest_html, sanitized_plain, _ = sanitize_article_html(raw_digest_html)
                    digest_plain = sanitized_plain or "\n".join(plain_parts)
                    digest_title = f"{source.name} · 热点速览 ({date_slot})"
                    _, created = await _upsert_article(
                        session,
                        source=source,
                        url=digest_url,
                        title=digest_title,
                        author=source.name,
                        published_at=now_local,
                        clean_html=digest_html,
                        plain_text=digest_plain,
                        lead_image_url=None,
                        language=detect_language(digest_plain),
                        category=source.category,
                        source_name=source.name,
                        extra_meta={
                            "newsnow_id": source.identifier,
                            "item_count": len(entries),
                            "date_slot": date_slot,
                        },
                        update_existing=True,
                    )
                    if created:
                        new_count += 1
                    else:
                        skipped_count += 1
            else:
                sem = asyncio.Semaphore(1 if source.fetch_strategy == "byparr_browser" else 4)

                async def _extract_one(
                    url: str, title: str, hint: str, pub_dt: datetime, info_tag: str
                ) -> tuple[str, str, str, datetime, str, str, str | None, str] | None:
                    safe_url = _safe_http_url(url)
                    if not safe_url:
                        return None
                    async with sem:
                        try:
                            raw_html, final_url = await smart_fetch_text(
                                safe_url,
                                use_proxy=source.use_proxy,
                                outbound_proxy=net["outbound_proxy"],
                                byparr_base_url=net["byparr_base_url"],
                                force_byparr=source.fetch_strategy == "byparr_browser",
                                timeout=18.0,
                            )
                            extracted = await asyncio.to_thread(
                                extract_from_webpage,
                                raw_html,
                                final_url,
                                fallback_title=title,
                                content_selector=content_selector,
                                remove_selectors=remove_selectors,
                            )
                            if len(extracted.plain_text) >= 60:
                                return (
                                    final_url,
                                    extracted.title or title,
                                    extracted.author or source.name,
                                    pub_dt,
                                    extracted.clean_html,
                                    extracted.plain_text,
                                    extracted.lead_image_url,
                                    extracted.language,
                                )
                        except Exception:
                            pass
                        # Graceful fallback to title + summary hint so NewsNow items are never lost
                        fallback_text = hint or f"{title}（来源：{source.name}）"
                        raw_fallback_html = (
                            f"<p>{html_escape.escape(fallback_text)}</p>"
                            f'<p><a href="{html_escape.escape(safe_url, quote=True)}">阅读原文</a></p>'
                        )
                        fallback_html, sanitized_plain, _ = sanitize_article_html(raw_fallback_html)
                        final_plain = sanitized_plain or fallback_text
                        return (
                            safe_url,
                            title,
                            source.name,
                            pub_dt,
                            fallback_html,
                            final_plain,
                            None,
                            detect_language(f"{title} {final_plain}"),
                        )

                # Filter already-known URLs before hitting remote servers
                valid_entries = [e for e in entries if _safe_http_url(e.url)]
                known_urls = (
                    set(
                        (
                            await session.execute(
                                select(Article.canonical_url).where(
                                    Article.canonical_url.in_(
                                        [canonicalize_url(e.url) for e in valid_entries]
                                    )
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if valid_entries
                    else set()
                )
                to_fetch = [e for e in valid_entries if canonicalize_url(e.url) not in known_urls]
                skipped_count += len(valid_entries) - len(to_fetch)

                extracted_list = await asyncio.gather(
                    *[
                        _extract_one(e.url, e.title, e.summary_hint, e.pub_date, e.info_tag)
                        for e in to_fetch
                    ],
                    return_exceptions=True,
                )
                for res in extracted_list:
                    if isinstance(res, BaseException) or res is None:
                        failed_items += 1
                        continue
                    (
                        final_url,
                        art_title,
                        art_author,
                        pub_dt,
                        c_html,
                        p_text,
                        lead_img,
                        lang,
                    ) = res
                    _, created = await _upsert_article(
                        session,
                        source=source,
                        url=final_url,
                        title=art_title,
                        author=art_author,
                        published_at=pub_dt,
                        clean_html=c_html,
                        plain_text=p_text,
                        lead_image_url=lead_img,
                        language=lang,
                        category=source.category,
                        source_name=source.name,
                    )
                    if created:
                        new_count += 1
                    else:
                        skipped_count += 1

        elif source.kind in ("rss", "rsshub"):
            has_title_filter = include_re is not None or exclude_re is not None
            rss_fetch_limit = max(source.max_items * 4, 60) if has_title_filter else source.max_items
            _, raw_rss_entries = await fetch_rss_entries(
                source.identifier,
                rsshub_base_url=net["rsshub_base_url"],
                use_proxy=source.use_proxy,
                outbound_proxy=net["outbound_proxy"],
                byparr_base_url=net["byparr_base_url"],
                max_items=rss_fetch_limit,
            )
            filtered_rss = [
                e for e in raw_rss_entries if _matches_title_filters(e.title, include_re, exclude_re)
            ]
            skipped_count += len(raw_rss_entries) - len(filtered_rss)
            rss_entries = filtered_rss[: source.max_items]

            known_urls = (
                set(
                    (
                        await session.execute(
                            select(Article.canonical_url).where(
                                Article.canonical_url.in_(
                                    [canonicalize_url(e.url) for e in rss_entries]
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if rss_entries
                else set()
            )
            for rss_item in rss_entries:
                if canonicalize_url(rss_item.url) in known_urls:
                    skipped_count += 1
                    continue

                use_feed_body = (
                    source.fetch_strategy == "fulltext_rss" or rss_item.is_fulltext_candidate
                )
                raw_feed_html = rss_item.content_html or rss_item.summary_html
                clean_html, plain_text, lead_img = sanitize_article_html(
                    raw_feed_html, base_url=rss_item.url
                )
                art_title = rss_item.title
                art_author = rss_item.author or source.name
                lang = detect_language(f"{art_title} {plain_text}")

                if not use_feed_body or len(plain_text) < 160:
                    try:
                        web_html, final_url = await smart_fetch_text(
                            rss_item.url,
                            use_proxy=source.use_proxy,
                            outbound_proxy=net["outbound_proxy"],
                            byparr_base_url=net["byparr_base_url"],
                            force_byparr=source.fetch_strategy == "byparr_browser",
                            timeout=20.0,
                        )
                        extracted = await asyncio.to_thread(
                            extract_from_webpage,
                            web_html,
                            final_url,
                            fallback_title=rss_item.title,
                            content_selector=content_selector,
                            remove_selectors=remove_selectors,
                        )
                        if len(extracted.plain_text) > len(plain_text):
                            clean_html = extracted.clean_html
                            plain_text = extracted.plain_text
                            lead_img = extracted.lead_image_url or lead_img
                            art_author = extracted.author or art_author
                            lang = extracted.language
                    except Exception as exc:
                        logger.debug("rss_web_extract_fallback", url=rss_item.url, err=str(exc))

                if not plain_text:
                    failed_items += 1
                    continue

                _, created = await _upsert_article(
                    session,
                    source=source,
                    url=rss_item.url,
                    title=art_title,
                    author=art_author,
                    published_at=rss_item.published_at,
                    clean_html=clean_html,
                    plain_text=plain_text,
                    lead_image_url=lead_img,
                    language=lang,
                    category=source.category,
                    source_name=source.name,
                )
                if created:
                    new_count += 1
                else:
                    skipped_count += 1

        elif source.kind == "webpage_list":
            list_html, list_final_url = await smart_fetch_text(
                source.identifier,
                use_proxy=source.use_proxy,
                outbound_proxy=net["outbound_proxy"],
                byparr_base_url=net["byparr_base_url"],
                force_byparr=source.fetch_strategy == "byparr_browser",
                timeout=25.0,
            )
            has_title_filter = include_re is not None or exclude_re is not None
            link_fetch_limit = max(source.max_items * 3, 60) if has_title_filter else source.max_items
            raw_links = await asyncio.to_thread(
                extract_links_from_list_page,
                list_html,
                list_final_url,
                link_selector,
                link_fetch_limit,
            )
            candidate_links: list[tuple[str, str]] = []
            for link_url, link_text in raw_links:
                if link_text and not _matches_title_filters(link_text, include_re, exclude_re):
                    skipped_count += 1
                    continue
                candidate_links.append((link_url, link_text))
                if len(candidate_links) >= source.max_items:
                    break

            known_urls = (
                set(
                    (
                        await session.execute(
                            select(Article.canonical_url).where(
                                Article.canonical_url.in_(
                                    [canonicalize_url(u) for u, _ in candidate_links]
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if candidate_links
                else set()
            )
            to_fetch_links = [
                (u, t) for u, t in candidate_links if canonicalize_url(u) not in known_urls
            ]
            skipped_count += len(candidate_links) - len(to_fetch_links)

            sem = asyncio.Semaphore(1 if source.fetch_strategy == "byparr_browser" else 4)

            async def _fetch_list_item(
                item_url: str, item_text: str
            ) -> tuple[str, str, str, str, str, str | None, str, bool] | None:
                async with sem:
                    raw_art_html, final_url = await smart_fetch_text(
                        item_url,
                        use_proxy=source.use_proxy,
                        outbound_proxy=net["outbound_proxy"],
                        byparr_base_url=net["byparr_base_url"],
                        force_byparr=source.fetch_strategy == "byparr_browser",
                        timeout=25.0,
                    )
                    extracted = await asyncio.to_thread(
                        extract_from_webpage,
                        raw_art_html,
                        final_url,
                        fallback_title=item_text,
                        content_selector=content_selector,
                        remove_selectors=remove_selectors,
                    )
                    final_title = extracted.title or item_text
                    if not item_text and not _matches_title_filters(
                        final_title, include_re, exclude_re
                    ):
                        return (final_url, final_title, "", "", "", None, "zh-CN", True)
                    if len(extracted.plain_text) < 40:
                        return None
                    return (
                        final_url,
                        final_title,
                        extracted.author or source.name,
                        extracted.clean_html,
                        extracted.plain_text,
                        extracted.lead_image_url,
                        extracted.language,
                        False,
                    )

            fetched_items = await asyncio.gather(
                *[_fetch_list_item(u, t) for u, t in to_fetch_links],
                return_exceptions=True,
            )
            for list_res in fetched_items:
                if isinstance(list_res, BaseException) or list_res is None:
                    failed_items += 1
                    continue
                (
                    wl_url,
                    wl_title,
                    wl_author,
                    wl_html,
                    wl_plain,
                    wl_lead_img,
                    wl_lang,
                    wl_filtered_out,
                ) = list_res
                if wl_filtered_out:
                    skipped_count += 1
                    continue
                _, created = await _upsert_article(
                    session,
                    source=source,
                    url=wl_url,
                    title=wl_title,
                    author=wl_author,
                    published_at=utcnow(),
                    clean_html=wl_html,
                    plain_text=wl_plain,
                    lead_image_url=wl_lead_img,
                    language=wl_lang,
                    category=source.category,
                    source_name=source.name,
                )
                if created:
                    new_count += 1
                else:
                    skipped_count += 1

        elif source.kind == "webpage":
            raw_html, final_url = await smart_fetch_text(
                source.identifier,
                use_proxy=source.use_proxy,
                outbound_proxy=net["outbound_proxy"],
                byparr_base_url=net["byparr_base_url"],
                force_byparr=source.fetch_strategy == "byparr_browser",
            )
            extracted = await asyncio.to_thread(
                extract_from_webpage,
                raw_html,
                final_url,
                fallback_title=source.name,
                content_selector=content_selector,
                remove_selectors=remove_selectors,
            )
            _, created = await _upsert_article(
                session,
                source=source,
                url=final_url,
                title=extracted.title,
                author=extracted.author or source.name,
                published_at=utcnow(),
                clean_html=extracted.clean_html,
                plain_text=extracted.plain_text,
                lead_image_url=extracted.lead_image_url,
                language=extracted.language,
                category=source.category,
                source_name=source.name,
            )
            if created:
                new_count += 1
            else:
                skipped_count += 1

        source.last_fetched_at = utcnow()
        source.last_error = None
        source.consecutive_failures = 0
        await session.commit()
    except Exception as exc:
        await session.rollback()
        db_source = await session.get(Source, source_id) if source_id is not None else None
        target = db_source if db_source is not None else source
        now = utcnow()
        err_msg = str(exc)[:1000]
        target.last_fetched_at = now
        target.last_error = err_msg
        target.consecutive_failures = (target.consecutive_failures or 0) + 1
        if target is not source:
            source.last_fetched_at = now
            source.last_error = err_msg
            source.consecutive_failures = target.consecutive_failures
        await session.commit()
        logger.warning("source_sync_failed", source_id=source_id, name=source_name, err=str(exc))
        raise

    return {"new": new_count, "skipped": skipped_count, "failed": failed_items}
