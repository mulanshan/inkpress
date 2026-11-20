from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from inkpress.logging import get_logger

logger = get_logger(__name__)


@dataclass
class NewsNowEntry:
    item_id: str
    title: str
    url: str
    mobile_url: str
    pub_date: datetime
    summary_hint: str
    info_tag: str
    raw_extra: dict[str, Any]


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _clean_url(raw: Any) -> str:
    if raw is None:
        return ""
    unescaped = html.unescape(str(raw)).strip()
    return unescaped if _is_valid_http_url(unescaped) else ""


def _parse_pub_date(raw: Any) -> datetime:
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return datetime.now(UTC)
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if s.isdigit():
            return _parse_pub_date(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


async def fetch_newsnow_source(
    source_id: str,
    base_url: str,
    *,
    latest: bool = True,
    timeout: float = 20.0,
) -> list[NewsNowEntry]:
    """Fetch items for a given NewsNow source ID from the internal sidecar."""
    url = f"{base_url.rstrip('/')}/api/s"
    params: dict[str, str] = {"id": source_id}
    if latest:
        params["latest"] = "true"

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(url, params=params, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    items_raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items_raw, list):
        items_raw = []

    entries: list[NewsNowEntry] = []
    for raw in items_raw:
        if not isinstance(raw, dict):
            continue
        title = html.unescape(str(raw.get("title") or "")).strip()
        primary_url = _clean_url(raw.get("url"))
        mobile_url = _clean_url(raw.get("mobileUrl"))
        link = primary_url or mobile_url
        if not title or not link:
            continue
        raw_extra_val = raw.get("extra")
        extra: dict[str, Any] = raw_extra_val if isinstance(raw_extra_val, dict) else {}
        hover = html.unescape(str(extra.get("hover") or "")).strip()
        info_val = extra.get("info")
        info_tag = str(info_val).strip() if isinstance(info_val, (str, int, float)) else ""
        entries.append(
            NewsNowEntry(
                item_id=str(raw.get("id") or link),
                title=title,
                url=link,
                mobile_url=mobile_url or link,
                pub_date=_parse_pub_date(raw.get("pubDate") or extra.get("date")),
                summary_hint=hover,
                info_tag=info_tag,
                raw_extra=extra,
            )
        )

    if not entries:
        logger.warning("newsnow_empty_items", source_id=source_id, raw_count=len(items_raw))
    else:
        logger.info("newsnow_fetched", source_id=source_id, count=len(entries))
    return entries
