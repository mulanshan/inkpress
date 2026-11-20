from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser

from inkpress.ingestion.fetcher import smart_fetch_text


@dataclass
class RssEntry:
    title: str
    url: str
    author: str
    published_at: datetime
    content_html: str
    summary_html: str
    is_fulltext_candidate: bool


def resolve_feed_url(identifier: str, rsshub_base_url: str) -> str:
    cleaned = identifier.strip()
    if cleaned.startswith("rsshub://"):
        route = cleaned[len("rsshub://") :].lstrip("/")
        return f"{rsshub_base_url.rstrip('/')}/{route}"
    return cleaned


def _parse_entry_date(entry: Any) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val is not None:
            try:
                return datetime(
                    val.tm_year,
                    val.tm_mon,
                    val.tm_mday,
                    val.tm_hour,
                    val.tm_min,
                    val.tm_sec,
                    tzinfo=UTC,
                )
            except ValueError:
                pass
    for key in ("published", "updated"):
        raw = entry.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                dt = parsedate_to_datetime(raw.strip())
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                pass
    return datetime.now(UTC)


def _strip_tags_len(html: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html).strip())


async def fetch_rss_entries(
    identifier: str,
    *,
    rsshub_base_url: str,
    use_proxy: str = "auto",
    outbound_proxy: str = "",
    byparr_base_url: str = "",
    max_items: int = 20,
) -> tuple[str, list[RssEntry]]:
    """Fetch and parse an RSS/Atom/JSON feed (including rsshub:// shortcuts)."""
    url = resolve_feed_url(identifier, rsshub_base_url)
    # Internal RSSHub calls never need proxy or SSL verify
    is_internal = url.startswith(rsshub_base_url.rstrip("/"))
    raw_xml, _ = await smart_fetch_text(
        url,
        use_proxy="direct" if is_internal else use_proxy,
        outbound_proxy="" if is_internal else outbound_proxy,
        byparr_base_url="" if is_internal else byparr_base_url,
        verify_ssl=not url.startswith("https://www.dapenti.com"),
    )
    parsed = feedparser.parse(raw_xml)
    feed_title = str(parsed.feed.get("title") or "").strip()

    results: list[RssEntry] = []
    for entry in parsed.entries[:max_items]:
        title = str(entry.get("title") or "").strip()
        link = str(entry.get("link") or "").strip()
        if not title or not link:
            continue

        content_html = ""
        contents = entry.get("content")
        if isinstance(contents, list) and contents:
            content_html = str(contents[0].get("value") or "")
        summary_html = str(entry.get("summary") or entry.get("description") or "")
        best_html = content_html if len(content_html) >= len(summary_html) else summary_html
        text_len = _strip_tags_len(best_html)
        has_rich_markup = (
            "<p" in best_html.lower() or "<img" in best_html.lower() or "<br" in best_html.lower()
        )
        is_fulltext = text_len >= 400 or (text_len >= 180 and has_rich_markup)

        results.append(
            RssEntry(
                title=title,
                url=link,
                author=str(entry.get("author") or "").strip(),
                published_at=_parse_entry_date(entry),
                content_html=content_html,
                summary_html=summary_html,
                is_fulltext_candidate=is_fulltext,
            )
        )
    return feed_title, results
