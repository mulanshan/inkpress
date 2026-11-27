from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from inkpress.config import get_settings
from inkpress.logging import get_logger

logger = get_logger(__name__)

_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

WSCN_API_BASE = "https://api-one-wscn.awtmt.com"
ZHIHU_HOTLIST_URL = "https://api.zhihu.com/topstory/hot-lists/total?limit=30"
AIHOT_API_URL = "https://aihot.news/api/v1/items?limit=30"
DOUBAN_HOTLIST_URL = (
    "https://m.douban.com/rexxar/api/v2/subject_collection/movie_real_time_hotest/items"
    "?type=movie&start=0&count=20&for_mobile=1"
)
TOUTIAO_HOTBOARD_URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
MKTNEWS_FLASH_URL = "https://api.mktnews.net/api/flash?type=0&limit=40"

DIRECT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


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
    image_url: str = ""


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _clean_url(raw: Any) -> str:
    if raw is None:
        return ""
    unescaped = html.unescape(str(raw)).strip()
    return unescaped if _is_valid_http_url(unescaped) else ""


def _strip_html_text(raw: Any) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(str(raw))
    no_tags = _TAG_STRIP_RE.sub(" ", unescaped)
    return _WS_RE.sub(" ", no_tags).strip()


def _default_local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().tz)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


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
        local_tz = _default_local_tz()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=local_tz)
            return aware.astimezone(UTC)
        except ValueError:
            pass
        try:
            dt_rfc = parsedate_to_datetime(s)
            aware_rfc = dt_rfc if dt_rfc.tzinfo is not None else dt_rfc.replace(tzinfo=local_tz)
            return aware_rfc.astimezone(UTC)
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


def _derive_short_headline(full_text: str, explicit_title: str = "") -> tuple[str, str]:
    """Ensure flash dispatches have both a concise headline and a rich summary body."""
    cleaned_title = explicit_title.strip()
    cleaned_body = full_text.strip()
    if cleaned_title and cleaned_body and cleaned_body != cleaned_title:
        return cleaned_title, cleaned_body

    text = cleaned_body or cleaned_title
    if not text:
        return "", ""

    # Bracketed lead like 【蚂蚁OceanBase披露2026年经营数据】...
    bracket_m = re.match(r"^【([^】]{4,80})】\s*(.*)$", text)
    if bracket_m:
        head = bracket_m.group(1).strip()
        rest = bracket_m.group(2).strip()
        return head, text if len(rest) >= 15 else rest

    # Split first sentence if the dispatch is a full paragraph
    if len(text) > 56:
        sent_m = re.split(r"(?<=[。！？；!?;])\s*", text, maxsplit=1)
        first = sent_m[0].rstrip("。；;")
        if 8 <= len(first) <= 72 and len(sent_m) > 1 and sent_m[1].strip():
            return first, text
        return text[:52].rstrip("，、 ") + "…", text

    return text, ""


def _extract_wscn_image(item: dict[str, Any]) -> str:
    img_obj = item.get("image")
    if isinstance(img_obj, dict):
        u = _clean_url(img_obj.get("uri") or img_obj.get("url"))
        if u:
            return u
    for key in ("image_uris", "images", "cover_images", "content_args"):
        arr = item.get(key)
        if not isinstance(arr, list):
            continue
        for elem in arr:
            if isinstance(elem, str):
                u = _clean_url(elem)
                if u:
                    return u
            elif isinstance(elem, dict):
                u = _clean_url(elem.get("uri") or elem.get("src") or elem.get("url"))
                if u:
                    return u
    return ""


async def _fetch_wallstreetcn_direct(source_id: str, timeout: float) -> list[NewsNowEntry]:
    """Fetch WallstreetCN dispatches/articles directly from official WSCN API with full text & charts."""
    if source_id in ("wallstreetcn", "wallstreetcn-quick"):
        endpoint = f"{WSCN_API_BASE}/apiv1/content/lives?channel=global-channel&limit=30"
    elif source_id == "wallstreetcn-news":
        endpoint = (
            f"{WSCN_API_BASE}/apiv1/content/information-flow"
            "?channel=global-channel&accept=article&limit=30"
        )
    elif source_id == "wallstreetcn-hot":
        endpoint = f"{WSCN_API_BASE}/apiv1/content/articles/hot?period=all"
    else:
        return []

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(endpoint, headers=DIRECT_HEADERS)
        resp.raise_for_status()
        payload = resp.json()

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []

    entries: list[NewsNowEntry] = []
    if source_id in ("wallstreetcn", "wallstreetcn-quick"):
        items = data.get("items") or []
        for k in items:
            if not isinstance(k, dict):
                continue
            item_id = str(k.get("id") or "")
            raw_title = _strip_html_text(k.get("title") or k.get("highlight_title"))
            main_text = _strip_html_text(k.get("content_text") or k.get("content"))
            more_text = _strip_html_text(k.get("content_more"))
            combined = f"{main_text} {more_text}".strip() if more_text else main_text
            title, summary = _derive_short_headline(combined, raw_title)
            link = _clean_url(k.get("uri")) or (
                f"https://wallstreetcn.com/livenews/{item_id}" if item_id else ""
            )
            if not title or not link:
                continue
            entries.append(
                NewsNowEntry(
                    item_id=item_id or link,
                    title=title,
                    url=link,
                    mobile_url=link,
                    pub_date=_parse_pub_date(k.get("display_time")),
                    summary_hint=summary,
                    info_tag="快讯",
                    raw_extra={"channel": k.get("global_channel_name") or "7x24快讯"},
                    image_url=_extract_wscn_image(k),
                )
            )
    elif source_id == "wallstreetcn-news":
        items = data.get("items") or []
        for wrapper in items:
            if not isinstance(wrapper, dict):
                continue
            if wrapper.get("resource_type") in ("theme", "ad"):
                continue
            h = wrapper.get("resource")
            if not isinstance(h, dict) or h.get("type") == "live":
                continue
            title = _strip_html_text(h.get("title"))
            summary = _strip_html_text(h.get("content_short"))
            link = _clean_url(h.get("uri"))
            if not title or not link:
                continue
            entries.append(
                NewsNowEntry(
                    item_id=str(h.get("id") or link),
                    title=title,
                    url=link,
                    mobile_url=link,
                    pub_date=_parse_pub_date(h.get("display_time")),
                    summary_hint=summary,
                    info_tag="",
                    raw_extra={},
                    image_url=_extract_wscn_image(h),
                )
            )
    else:
        items = data.get("day_items") or []
        for h in items:
            if not isinstance(h, dict):
                continue
            title = _strip_html_text(h.get("title"))
            summary = _strip_html_text(h.get("content_short"))
            link = _clean_url(h.get("uri"))
            if not title or not link:
                continue
            entries.append(
                NewsNowEntry(
                    item_id=str(h.get("id") or link),
                    title=title,
                    url=link,
                    mobile_url=link,
                    pub_date=_parse_pub_date(h.get("display_time")),
                    summary_hint=summary,
                    info_tag="热门",
                    raw_extra={},
                    image_url=_extract_wscn_image(h),
                )
            )

    if entries:
        logger.info("wscn_direct_fetched", source_id=source_id, count=len(entries))
    return entries


async def _fetch_zhihu_direct(timeout: float) -> list[NewsNowEntry]:
    """Fetch Zhihu hotlist directly with excerpt descriptions and 720p answer thumbnails."""
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(ZHIHU_HOTLIST_URL, headers=DIRECT_HEADERS)
        resp.raise_for_status()
        payload = resp.json()

    items_raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items_raw, list):
        return []

    entries: list[NewsNowEntry] = []
    for row in items_raw:
        if not isinstance(row, dict):
            continue
        target = row.get("target")
        if not isinstance(target, dict):
            continue
        qid = target.get("id")
        title = _strip_html_text(target.get("title") or target.get("title_area", {}).get("text"))
        excerpt = _strip_html_text(target.get("excerpt") or target.get("excerpt_area", {}).get("text"))
        link = f"https://www.zhihu.com/question/{qid}" if qid else _clean_url(target.get("url"))
        if not title or not link:
            continue
        detail_text = _strip_html_text(row.get("detail_text"))
        thumb = ""
        children = row.get("children")
        if isinstance(children, list):
            for ch in children:
                if isinstance(ch, dict):
                    cand = _clean_url(ch.get("thumbnail"))
                    if cand:
                        thumb = cand
                        break
        entries.append(
            NewsNowEntry(
                item_id=str(qid or link),
                title=title,
                url=link,
                mobile_url=link,
                pub_date=_parse_pub_date(target.get("created")),
                summary_hint=excerpt,
                info_tag=detail_text,
                raw_extra={"answer_count": target.get("answer_count", 0)},
                image_url=thumb,
            )
        )

    if entries:
        logger.info("zhihu_direct_fetched", count=len(entries))
    return entries


async def _fetch_aihot_direct(timeout: float, outbound_proxy: str = "") -> list[NewsNowEntry]:
    """Fetch AI 热点 from official v1 JSON endpoint with rich summaries and scores."""
    attempts: list[str | None] = [None]
    if outbound_proxy:
        attempts.append(outbound_proxy)

    payload: Any = None
    last_exc: Exception | None = None
    for proxy in attempts:
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False) as client:
                resp = await client.get(AIHOT_API_URL, headers=DIRECT_HEADERS)
                resp.raise_for_status()
                payload = resp.json()
                break
        except Exception as exc:
            last_exc = exc
    if payload is None:
        if last_exc is not None:
            raise last_exc
        return []

    items_raw = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items_raw, list):
        return []

    entries: list[NewsNowEntry] = []
    for row in items_raw:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id") or "")
        title = _strip_html_text(row.get("title") or row.get("originalTitle"))
        links_raw = row.get("links")
        links: dict[str, Any] = links_raw if isinstance(links_raw, dict) else {}
        link = _clean_url(links.get("original") or links.get("aihot"))
        if not title or not link:
            continue
        summary = _strip_html_text(row.get("summary") or row.get("reason"))
        src_raw = row.get("source")
        src_obj: dict[str, Any] = src_raw if isinstance(src_raw, dict) else {}
        src_name = _strip_html_text(src_obj.get("name"))
        if src_name and summary and src_name not in summary:
            summary = f"（{src_name}）{summary}"
        score = row.get("score")
        info_tag = f"{int(score)}分" if isinstance(score, (int, float)) else src_name
        entries.append(
            NewsNowEntry(
                item_id=item_id or link,
                title=title,
                url=link,
                mobile_url=link,
                pub_date=_parse_pub_date(row.get("publishedAt") or row.get("discoveredAt")),
                summary_hint=summary,
                info_tag=info_tag,
                raw_extra={"source": src_name},
                image_url=_clean_url(row.get("image") or row.get("cover")),
            )
        )

    if entries:
        logger.info("aihot_direct_fetched", count=len(entries))
    return entries


async def _fetch_douban_direct(timeout: float) -> list[NewsNowEntry]:
    """Fetch Douban real-time movie/culture hotlist with Referer header, posters, ratings & reviews."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
        ),
        "Accept": "application/json",
        "Referer": "https://m.douban.com/movie/",
    }
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(DOUBAN_HOTLIST_URL, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    items_raw = payload.get("subject_collection_items") if isinstance(payload, dict) else None
    if not isinstance(items_raw, list):
        return []

    entries: list[NewsNowEntry] = []
    for row in items_raw:
        if not isinstance(row, dict):
            continue
        subj_id = str(row.get("id") or "")
        raw_title = _strip_html_text(row.get("title"))
        if not raw_title:
            continue
        title = f"《{raw_title}》" if not raw_title.startswith("《") else raw_title
        link = (
            f"https://movie.douban.com/subject/{subj_id}/"
            if subj_id
            else _clean_url(row.get("url"))
        )
        if not link:
            continue
        rating_raw = row.get("rating")
        rating_obj: dict[str, Any] = rating_raw if isinstance(rating_raw, dict) else {}
        rating_val = rating_obj.get("value")
        type_name = _strip_html_text(row.get("type_name"))
        info_tag = f"豆瓣 {rating_val}" if rating_val else (type_name or "热门")

        subtitle = _strip_html_text(row.get("card_subtitle") or row.get("info"))
        rec = _strip_html_text(row.get("recommended_reason") or row.get("description"))
        comments = row.get("comments") if isinstance(row.get("comments"), list) else []
        top_comment = (
            _strip_html_text(comments[0].get("comment"))
            if comments and isinstance(comments[0], dict)
            else ""
        )
        summary_parts = [p for p in (subtitle, rec, f"热评：{top_comment}" if top_comment else "") if p]
        summary_hint = " ｜ ".join(summary_parts)

        cover_raw = row.get("cover")
        pic_raw = row.get("pic")
        cover: dict[str, Any] = cover_raw if isinstance(cover_raw, dict) else {}
        pic: dict[str, Any] = pic_raw if isinstance(pic_raw, dict) else {}
        poster = _clean_url(cover.get("url") or pic.get("large") or pic.get("normal"))

        entries.append(
            NewsNowEntry(
                item_id=subj_id or link,
                title=title,
                url=link,
                mobile_url=_clean_url(row.get("url")) or link,
                pub_date=datetime.now(UTC),
                summary_hint=summary_hint,
                info_tag=info_tag,
                raw_extra={"rating": rating_val},
                image_url=poster,
            )
        )

    if entries:
        logger.info("douban_direct_fetched", count=len(entries))
    return entries


async def _fetch_toutiao_direct(timeout: float) -> list[NewsNowEntry]:
    """Fetch Toutiao hotboard directly with clean trending URLs, formatted heat, and cover photos."""
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(TOUTIAO_HOTBOARD_URL, headers=DIRECT_HEADERS)
        resp.raise_for_status()
        payload = resp.json()

    items_raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items_raw, list):
        return []

    entries: list[NewsNowEntry] = []
    for row in items_raw:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("ClusterIdStr") or row.get("ClusterId") or "").strip()
        title = _strip_html_text(row.get("Title") or row.get("QueryWord"))
        link = f"https://www.toutiao.com/trending/{cid}/" if cid else _clean_url(row.get("Url"))
        if not title or not link:
            continue
        hot_raw = str(row.get("HotValue") or "").strip()
        if hot_raw.isdigit() and int(hot_raw) >= 10000:
            info_tag = f"{int(hot_raw) // 10000}万热度"
        else:
            info_tag = _strip_html_text(row.get("LabelDesc") or row.get("Label")) or hot_raw
        img_raw = row.get("Image")
        img_obj: dict[str, Any] = img_raw if isinstance(img_raw, dict) else {}
        img_url = _clean_url(img_obj.get("url"))
        entries.append(
            NewsNowEntry(
                item_id=cid or link,
                title=title,
                url=link,
                mobile_url=link,
                pub_date=datetime.now(UTC),
                summary_hint="",
                info_tag=info_tag,
                raw_extra={},
                image_url=img_url,
            )
        )

    if entries:
        logger.info("toutiao_direct_fetched", count=len(entries))
    return entries


async def _fetch_mktnews_direct(timeout: float, outbound_proxy: str = "") -> list[NewsNowEntry]:
    """Fetch MKTNews flashes & macro indicator releases (extracting actual/consensus/previous)."""
    headers = {
        **DIRECT_HEADERS,
        "Origin": "https://mktnews.net",
        "Referer": "https://mktnews.net/",
    }
    attempts: list[str | None] = [outbound_proxy] if outbound_proxy else []
    attempts.append(None)

    payload: Any = None
    last_exc: Exception | None = None
    for proxy in attempts:
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False) as client:
                resp = await client.get(MKTNEWS_FLASH_URL, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
                break
        except Exception as exc:
            last_exc = exc
    if payload is None:
        if last_exc is not None:
            raise last_exc
        return []

    items_raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items_raw, list):
        return []

    entries: list[NewsNowEntry] = []
    for row in items_raw:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id") or "")
        link = f"https://mktnews.net/flashDetail.html?id={item_id}" if item_id else ""
        if not link:
            continue
        d_raw = row.get("data")
        d: dict[str, Any] = d_raw if isinstance(d_raw, dict) else {}
        pub_dt = _parse_pub_date(row.get("time"))
        important = row.get("important") == 1
        img_url = _clean_url(d.get("pic"))

        if row.get("type") == 1:
            raw_name = _strip_html_text(d.get("title") or d.get("name"))
            country = _strip_html_text(d.get("country"))
            unit = str(d.get("unit") or "").strip()
            actual = d.get("actual")
            consensus = d.get("consensus")
            previous = d.get("previous")
            revised = d.get("revised")
            metrics: list[str] = []
            if actual is not None and str(actual) != "":
                metrics.append(f"今值: {actual}{unit}")
            if consensus is not None and str(consensus) != "":
                metrics.append(f"预期: {consensus}{unit}")
            if previous is not None and str(previous) != "":
                metrics.append(f"前值: {previous}{unit}")
            if revised is not None and str(revised) != "":
                metrics.append(f"修正: {revised}{unit}")
            if not raw_name or not metrics:
                continue
            title = f"[{country}] {raw_name}" if country and country not in raw_name else raw_name
            summary = f"{title} — {'，'.join(metrics)}"
        else:
            content = _strip_html_text(d.get("content"))
            raw_title = _strip_html_text(d.get("title"))
            title, summary = _derive_short_headline(content, raw_title)
            if not title:
                continue

        entries.append(
            NewsNowEntry(
                item_id=item_id,
                title=title,
                url=link,
                mobile_url=link,
                pub_date=pub_dt,
                summary_hint=summary,
                info_tag="重要" if important else "",
                raw_extra={},
                image_url=img_url,
            )
        )

    if entries:
        logger.info("mktnews_direct_fetched", count=len(entries))
    return entries


async def fetch_newsnow_source(
    source_id: str,
    base_url: str,
    *,
    latest: bool = True,
    timeout: float = 20.0,
    outbound_proxy: str | None = None,
) -> list[NewsNowEntry]:
    """Fetch items for a given NewsNow source ID (with enriched direct handlers & sidecar fallback)."""
    eff_proxy = (
        outbound_proxy.strip()
        if outbound_proxy is not None
        else get_settings().outbound_proxy.strip()
    )
    direct_timeout = min(timeout, 12.0)

    if source_id in ("wallstreetcn", "wallstreetcn-quick", "wallstreetcn-news", "wallstreetcn-hot"):
        try:
            direct_wscn = await _fetch_wallstreetcn_direct(source_id, timeout=direct_timeout)
            if direct_wscn:
                return direct_wscn
        except Exception as exc:
            logger.debug("wscn_direct_fallback_sidecar", source_id=source_id, err=str(exc))
    elif source_id == "zhihu":
        try:
            direct_zh = await _fetch_zhihu_direct(timeout=direct_timeout)
            if direct_zh:
                return direct_zh
        except Exception as exc:
            logger.debug("zhihu_direct_fallback_sidecar", err=str(exc))
    elif source_id == "aihot":
        try:
            direct_ai = await _fetch_aihot_direct(timeout=direct_timeout, outbound_proxy=eff_proxy)
            if direct_ai:
                return direct_ai
        except Exception as exc:
            logger.debug("aihot_direct_fallback_sidecar", err=str(exc))
    elif source_id == "douban":
        try:
            direct_db = await _fetch_douban_direct(timeout=direct_timeout)
            if direct_db:
                return direct_db
        except Exception as exc:
            logger.debug("douban_direct_fallback_sidecar", err=str(exc))
    elif source_id == "toutiao":
        try:
            direct_tt = await _fetch_toutiao_direct(timeout=direct_timeout)
            if direct_tt:
                return direct_tt
        except Exception as exc:
            logger.debug("toutiao_direct_fallback_sidecar", err=str(exc))
    elif source_id in ("mktnews", "mktnews-flash"):
        try:
            direct_mkt = await _fetch_mktnews_direct(
                timeout=direct_timeout, outbound_proxy=eff_proxy
            )
            if direct_mkt:
                return direct_mkt
        except Exception as exc:
            logger.debug("mktnews_direct_fallback_sidecar", err=str(exc))

    url = f"{base_url.rstrip('/')}/api/s"
    params: dict[str, str] = {"id": source_id}
    if latest:
        params["latest"] = "true"

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.get(url, params=params, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()

    is_stale_cache = False
    if isinstance(data, dict) and data.get("status") == "cache":
        updated_raw = data.get("updatedTime")
        if updated_raw:
            updated_dt = _parse_pub_date(updated_raw)
            if datetime.now(UTC) - updated_dt > timedelta(hours=12):
                is_stale_cache = True
                logger.warning(
                    "newsnow_stale_cache_detected",
                    source_id=source_id,
                    updated_at=updated_dt.isoformat(),
                )

    if is_stale_cache:
        return []

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
        # Never use `extra.icon` here — on Weibo/Toutiao it is a tiny 16px "热/新" UI badge!
        img_candidate = _clean_url(
            raw.get("pic") or extra.get("cover") or extra.get("thumbnail")
        )
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
                image_url=img_candidate,
            )
        )

    if not entries:
        logger.warning("newsnow_empty_items", source_id=source_id, raw_count=len(items_raw))
    else:
        logger.info("newsnow_fetched", source_id=source_id, count=len(entries))
    return entries
