from __future__ import annotations

import asyncio
import html as html_escape
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.db.models import Article, Source, utcnow
from inkpress.ingestion.extractor import (
    canonicalize_url,
    compute_content_hash,
    detect_language,
    estimate_word_count_and_time,
    extract_from_webpage,
    sanitize_article_html,
)
from inkpress.ingestion.fetcher import smart_fetch_text
from inkpress.ingestion.newsnow_client import fetch_newsnow_source
from inkpress.ingestion.rss_client import fetch_rss_entries
from inkpress.logging import get_logger
from inkpress.settings_store import get_network_config

logger = get_logger(__name__)


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
) -> tuple[Article, bool]:
    canon = canonicalize_url(url)
    existing = (
        await session.execute(select(Article).where(Article.canonical_url == canon))
    ).scalar_one_or_none()
    if existing is not None:
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

    try:
        if source.kind == "newsnow":
            entries = await fetch_newsnow_source(
                source.identifier,
                net["newsnow_base_url"],
            )
            entries = entries[: source.max_items]

            if source.fetch_strategy == "newsnow_digest":
                # Aggregate real-time ticker / hotlist into a clean, readable digest article
                if entries:
                    now_local = datetime.now(UTC)
                    date_slot = now_local.strftime("%Y-%m-%d-%H")
                    digest_url = f"https://inkpress.local/newsnow/{source.identifier}/{date_slot}"
                    blocks: list[str] = []
                    plain_parts: list[str] = []
                    for idx, item in enumerate(entries, start=1):
                        safe_title = html_escape.escape(item.title)
                        safe_link = html_escape.escape(item.url)
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
                        blocks.append(
                            f'<div class="digest-item">'
                            f"<h3>{idx}. {safe_title}{badge}</h3>"
                            f"{hint_html}"
                            f'<p class="source-link"><a href="{safe_link}">原文链接</a></p>'
                            f"</div>"
                        )
                        plain_parts.append(f"{idx}. {item.title} {item.summary_hint}".strip())
                    digest_html = "\n".join(blocks)
                    digest_plain = "\n".join(plain_parts)
                    digest_title = f"{source.name} · 热点速览 ({now_local.strftime('%m-%d %H:00')})"
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
                        extra_meta={"newsnow_id": source.identifier, "item_count": len(entries)},
                    )
                    if created:
                        new_count += 1
                    else:
                        skipped_count += 1
            else:
                sem = asyncio.Semaphore(4)

                async def _extract_one(
                    url: str, title: str, hint: str, pub_dt: datetime, info_tag: str
                ) -> tuple[str, str, str, datetime, str, str, str | None, str] | None:
                    async with sem:
                        try:
                            raw_html, final_url = await smart_fetch_text(
                                url,
                                use_proxy=source.use_proxy,
                                outbound_proxy=net["outbound_proxy"],
                                byparr_base_url=net["byparr_base_url"],
                                force_byparr=source.fetch_strategy == "byparr_browser",
                                timeout=18.0,
                            )
                            extracted = extract_from_webpage(
                                raw_html, final_url, fallback_title=title
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
                        fallback_html = (
                            f"<p>{html_escape.escape(fallback_text)}</p>"
                            f'<p><a href="{html_escape.escape(url)}">阅读原文</a></p>'
                        )
                        return (
                            url,
                            title,
                            source.name,
                            pub_dt,
                            fallback_html,
                            fallback_text,
                            None,
                            detect_language(f"{title} {fallback_text}"),
                        )

                # Filter already-known URLs before hitting remote servers
                known_urls = set(
                    (
                        await session.execute(
                            select(Article.canonical_url).where(
                                Article.canonical_url.in_(
                                    [canonicalize_url(e.url) for e in entries]
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                to_fetch = [e for e in entries if canonicalize_url(e.url) not in known_urls]
                skipped_count += len(entries) - len(to_fetch)

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
            _, rss_entries = await fetch_rss_entries(
                source.identifier,
                rsshub_base_url=net["rsshub_base_url"],
                use_proxy=source.use_proxy,
                outbound_proxy=net["outbound_proxy"],
                byparr_base_url=net["byparr_base_url"],
                max_items=source.max_items,
            )
            known_urls = set(
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
                        extracted = extract_from_webpage(
                            web_html, final_url, fallback_title=rss_item.title
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

        elif source.kind == "webpage":
            raw_html, final_url = await smart_fetch_text(
                source.identifier,
                use_proxy=source.use_proxy,
                outbound_proxy=net["outbound_proxy"],
                byparr_base_url=net["byparr_base_url"],
                force_byparr=source.fetch_strategy == "byparr_browser",
            )
            extracted = extract_from_webpage(raw_html, final_url, fallback_title=source.name)
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
        source.last_fetched_at = utcnow()
        source.last_error = str(exc)[:1000]
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        await session.commit()
        logger.warning("source_sync_failed", source_id=source.id, name=source.name, err=str(exc))
        raise

    return {"new": new_count, "skipped": skipped_count, "failed": failed_items}
