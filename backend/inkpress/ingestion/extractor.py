from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import nh3
import trafilatura
from bs4 import BeautifulSoup, Tag
from readability import Document

from inkpress.config import get_settings
from inkpress.logging import get_logger

logger = get_logger(__name__)

ALLOWED_TAGS: set[str] = {
    "p",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "code",
    "ul",
    "ol",
    "li",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "del",
    "a",
    "img",
    "figure",
    "figcaption",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
    "div",
    "span",
    "sup",
    "sub",
}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "class"},
    "figure": {"class"},
    "figcaption": {"class"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "div": {"class"},
    "span": {"class"},
    "p": {"class"},
    "blockquote": {"class"},
}

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "spm",
    "from",
    "isappinstalled",
    "fbclid",
    "gclid",
    "ref",
}

LAZY_IMAGE_ATTRS = ("data-original", "data-src", "data-actualsrc", "data-lazy-src")

SPACER_IMAGE_PATTERNS = (
    "/images/v2/t.png",
    "spacer.gif",
    "blank.gif",
    "pixel.gif",
    "1x1.gif",
    "1x1.png",
    "transparent.png",
    "transparent.gif",
    "grey.gif",
    "loading.gif",
    "/_next/static/media/",
    "logo_rebang",
    "pp_report",
    "logo_kuaixun",
    "tianzi_white",
    "logo_bjjx",
    "scalecode.",
    "wuzhangai.",
    "label_sm_",
    "wx_qrcode",
    "app_download",
)

PLACEHOLDER_LEAD_IMAGE_PATTERNS = (
    *SPACER_IMAGE_PATTERNS,
    "qrcode",
    "avatar",
)

UI_CHROME_ALT_KEYWORDS = (
    "扫码下载",
    "客户端下载",
    "微信公众号",
    "官方微信",
    "澎湃热榜",
    "澎湃早晚报",
    "澎湃财讯",
    "填字游戏",
    "要闻精选",
    "可信网站验证",
    "无障碍阅读",
    "小程序码",
)

_BOILERPLATE_EXACT_LINES = frozenset(
    {
        "+1",
        "收藏",
        "我要举报",
        "查看更多",
        "扫码下载",
        "澎湃新闻客户端",
        "澎湃矩阵",
        "澎湃新闻微博",
        "澎湃新闻公众号",
        "澎湃新闻抖音号",
        "派生万物开放平台",
        "IP SHANGHAI",
        "SIXTH TONE",
        "新闻报料",
        "反馈",
        "登录",
        "喷嚏网官方新浪围脖",
        "天下旗艦訂閱全新上線",
        "立即訂閱",
        "訂閱天下",
    }
)

_BOILERPLATE_SUBSTRINGS = (
    "报料热线:",
    "报料热线：",
    "报料邮箱:",
    "报料邮箱：",
    "互联网新闻信息服务许可证",
    "增值电信业务经营许可证",
    "沪ICP备",
    "沪公网安备",
    "上海东方报业有限公司",
    "Android版 iPhone版 iPad版",
    "喷嚏网官方App",
    "喷嚏网官方网站",
    "每天网络精华尽在",
    "新浪声明：所有会议实录均为现场速记整理",
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMPTY_PUBLISH_SECTION_RE = re.compile(r"^(?:发布|發布|作者|编辑|編輯)\s*[:：]\s*[;；|｜]")
_AUTHOR_TRAILING_CLAUSE_RE = re.compile(
    r"\s*(?:来源|來源|出处|出處|责任编辑|責任編輯|责编|責編|编辑|編輯|微信公众号|微信公眾號)\s*[:：].*$"
)
_AUTHOR_PREFIX_RE = re.compile(
    r"^(?:作者|撰文|撰稿|记者|記者|发布|發布|编译|編譯|整理|编辑|編輯|文|by)\s*[:：/｜|;；\-—]+\s*"
    r"|^(?:作者|撰文|撰稿|记者|記者|编译|編譯)\s+"
    r"|^by\s+",
    re.IGNORECASE,
)
_NON_AUTHOR_LABELS = frozenset(
    {
        "国际时政",
        "即时",
        "财经",
        "科技",
        "国际",
        "新闻",
        "综合",
        "社论",
        "快讯",
        "匿名",
        "佚名",
        "未知",
        "null",
        "none",
        "admin",
    }
)


@dataclass
class ExtractedArticle:
    title: str
    author: str
    clean_html: str
    plain_text: str
    lead_image_url: str | None
    language: str
    word_count: int
    reading_time_min: int
    published_at: datetime | None = None


def clean_author_name(raw_author: str, source_name: str = "") -> str:
    """Sanitize scraped author metadata (prefixes, trailing 来源 clauses, unclosed brackets)."""
    s = re.sub(r"\s+", " ", _strip_control_chars(raw_author or "")).strip()
    if not s:
        return ""
    if _EMPTY_PUBLISH_SECTION_RE.match(s):
        return ""
    s = _AUTHOR_TRAILING_CLAUSE_RE.sub("", s).strip()
    for _ in range(2):
        next_s = _AUTHOR_PREFIX_RE.sub("", s).strip()
        if next_s == s:
            break
        s = next_s
    # Strip unclosed trailing brackets/parentheses (e.g. "吳麗雪（")
    for open_ch, close_ch in (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]")):
        if open_ch in s and close_ch not in s:
            s = s.split(open_ch, 1)[0].strip()
    s = s.strip(" \t\r\n:：;；,，、/｜|-—（(【[")
    if not s or len(s) > 64:
        return ""
    if s.lower() in _NON_AUTHOR_LABELS:
        return ""
    if source_name and s.strip().lower() == source_name.strip().lower():
        return ""
    return s


def _parse_webpage_published_at(soup: BeautifulSoup, meta_date: str | None = None) -> datetime | None:
    """Extract timezone-aware publication timestamp from HTML meta/time tags or Trafilatura date."""
    try:
        local_tz = ZoneInfo(get_settings().tz)
    except Exception:
        local_tz = ZoneInfo("Asia/Shanghai")

    candidates: list[str] = []
    for prop in (
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("name", "pubdate"),
        ("name", "publishdate"),
        ("name", "date"),
        ("itemprop", "datePublished"),
    ):
        tag = soup.find("meta", attrs={prop[0]: prop[1]})
        if isinstance(tag, Tag):
            val = str(tag.get("content") or "").strip()
            if val:
                candidates.append(val)
    time_tag = soup.find("time", attrs={"datetime": True})
    if isinstance(time_tag, Tag):
        tval = str(time_tag.get("datetime") or "").strip()
        if tval:
            candidates.append(tval)
    if meta_date and meta_date.strip():
        candidates.append(meta_date.strip())

    for raw in candidates:
        s = raw.strip()
        if not s:
            continue
        # Date-only YYYY-MM-DD
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            try:
                d_local = datetime.fromisoformat(s).replace(hour=12, minute=0, tzinfo=local_tz)
                today_local = datetime.now(local_tz).date()
                if d_local.date() == today_local:
                    return datetime.now(UTC)
                return d_local.astimezone(UTC)
            except ValueError:
                continue
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
    return None


def _strip_control_chars(text: str) -> str:
    return _CONTROL_CHARS_RE.sub("", text)


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _is_spacer_image(url: str) -> bool:
    lower = url.strip().lower()
    if not lower:
        return True
    if lower.startswith(("data:image/svg", "data:image/gif")):
        return True
    path = urlparse(lower).path
    if path == "/t.png" or path.endswith("/t.png"):
        return True
    return any(pat in lower for pat in SPACER_IMAGE_PATTERNS)


def _is_placeholder_lead_image(url: str) -> bool:
    if _is_spacer_image(url):
        return True
    lower = url.strip().lower()
    return any(pat in lower for pat in PLACEHOLDER_LEAD_IMAGE_PATTERNS)


def _resolve_img_src(raw_src: str, base_url: str) -> str:
    src_str = raw_src.strip()
    if not src_str:
        return ""
    if src_str.startswith("//"):
        src_str = urljoin(base_url, src_str) if base_url else f"https:{src_str}"
    parsed = urlparse(src_str)
    if parsed.scheme:
        return src_str if parsed.scheme.lower() in ("http", "https") and parsed.netloc else ""
    if base_url:
        resolved = urljoin(base_url, src_str)
        return resolved if _is_valid_http_url(resolved) else ""
    # Relative local path (e.g. EPUB rebuild images/rebuilt_001_01.jpg)
    return src_str


def _promote_lazy_images_in_soup(soup: BeautifulSoup, base_url: str = "") -> None:
    """Promote lazy-loaded image attributes to `src` before Trafilatura/Readability extraction."""
    for img in soup.find_all("img"):
        chosen = ""
        for attr in LAZY_IMAGE_ATTRS:
            val = img.get(attr)
            if not val:
                continue
            candidate = _resolve_img_src(str(val), base_url)
            if candidate and not _is_spacer_image(candidate):
                chosen = candidate
                break

        if not chosen:
            current_raw = str(img.get("src") or "").strip()
            current_resolved = _resolve_img_src(current_raw, base_url)
            if current_resolved and not _is_spacer_image(current_resolved):
                chosen = current_resolved

        for attr in LAZY_IMAGE_ATTRS:
            img.attrs.pop(attr, None)

        if chosen:
            img["src"] = chosen
        else:
            img.decompose()


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query_items = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS
    ]
    cleaned = parsed._replace(
        scheme=(parsed.scheme or "https").lower(),
        netloc=parsed.netloc.lower(),
        query=urlencode(sorted(query_items)),
        fragment="",
    )
    return urlunparse(cleaned)


def compute_content_hash(title: str, plain_text: str) -> str:
    normalized = re.sub(r"\s+", " ", f"{title}\n{plain_text[:2000]}").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def detect_language(text: str) -> str:
    sample = text[:1200]
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", sample))
    if cjk_count >= 12 or (sample and cjk_count / max(len(sample), 1) > 0.08):
        return "zh-CN"
    return "en"


def estimate_word_count_and_time(plain_text: str) -> tuple[int, int]:
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", plain_text))
    latin_words = len(re.findall(r"[A-Za-z0-9]+", plain_text))
    total = cjk_chars + latin_words
    minutes = max(1, round(total / 350))
    return total, minutes


def _is_ui_chrome_alt(alt: str) -> bool:
    cleaned = alt.strip()
    if not cleaned:
        return False
    return any(kw in cleaned for kw in UI_CHROME_ALT_KEYWORDS)


def _strip_portal_boilerplate_nodes(soup: BeautifulSoup) -> None:
    """Remove trailing/leading portal chrome, ICP footers, and app download prompts."""
    for node in list(soup.find_all(["p", "li", "div", "span"])):
        if not isinstance(node, Tag) or node.parent is None:
            continue
        if node.find(["img", "table", "pre", "blockquote"]):
            continue
        # Only inspect leaf-like text blocks so container divs wrapping whole articles are safe
        if node.name == "div" and node.find(["p", "h1", "h2", "h3", "ul", "ol"]):
            continue
        txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not txt:
            continue
        if txt in _BOILERPLATE_EXACT_LINES or (
            len(txt) <= 180 and any(sub in txt for sub in _BOILERPLATE_SUBSTRINGS)
        ):
            node.decompose()
    for list_tag in list(soup.find_all(["ul", "ol"])):
        if isinstance(list_tag, Tag) and not list_tag.get_text(strip=True) and not list_tag.find("img"):
            list_tag.decompose()


def sanitize_article_html(html: str, base_url: str = "") -> tuple[str, str, str | None]:
    """Normalize lazy images, enforce http/https URLs, strip control chars, and sanitize via nh3."""
    if not html or not html.strip():
        return "", "", None

    cleaned_input = _strip_control_chars(html)
    soup = BeautifulSoup(cleaned_input, "lxml")

    for tag in soup.find_all(["script", "style", "iframe", "noscript", "form", "button", "svg"]):
        tag.decompose()

    lead_img: str | None = None
    for img in soup.find_all("img"):
        alt_raw = str(img.get("alt") or "").strip()
        if _is_ui_chrome_alt(alt_raw):
            img.decompose()
            continue

        src_str = ""
        for attr in (*LAZY_IMAGE_ATTRS, "src"):
            raw_val = img.get(attr)
            if not raw_val:
                continue
            candidate = _resolve_img_src(str(raw_val), base_url)
            if candidate and not _is_spacer_image(candidate):
                src_str = candidate
                break

        if not src_str:
            img.decompose()
            continue

        for attr in LAZY_IMAGE_ATTRS:
            img.attrs.pop(attr, None)
        img["src"] = src_str

        # Clean problematic control characters from alt (Kindle Send-to-Kindle E999 trigger)
        alt = re.sub(r"[\x00-\x1f\x7f<>\"']", " ", alt_raw).strip()
        img["alt"] = alt[:120]

        if (
            lead_img is None
            and _is_valid_http_url(src_str)
            and not _is_placeholder_lead_image(src_str)
        ):
            lead_img = src_str

    _strip_portal_boilerplate_nodes(soup)

    for a in soup.find_all("a"):
        href = str(a.get("href") or "").strip()
        if not href:
            a.attrs.pop("href", None)
            continue
        parsed_href = urlparse(href)
        if parsed_href.scheme:
            if parsed_href.scheme.lower() not in ("http", "https") or not parsed_href.netloc:
                a.attrs.pop("href", None)
        elif href.startswith("#"):
            continue
        elif href.startswith("//"):
            resolved = urljoin(base_url, href) if base_url else f"https:{href}"
            if _is_valid_http_url(resolved):
                a["href"] = resolved
            else:
                a.attrs.pop("href", None)
        elif base_url:
            resolved = urljoin(base_url, href)
            if _is_valid_http_url(resolved):
                a["href"] = resolved
            else:
                a.attrs.pop("href", None)
        else:
            # Drop ambiguous scheme-less href when no base_url is known
            a.attrs.pop("href", None)

    body = soup.body if soup.body else soup
    raw_clean = body.decode_contents()

    sanitized = nh3.clean(
        raw_clean,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https"},
        strip_comments=True,
    )

    # Second pass: drop broken images without src and strip any residual control chars
    clean_soup = BeautifulSoup(_strip_control_chars(sanitized), "lxml")
    for img in clean_soup.find_all("img"):
        if not str(img.get("src") or "").strip():
            img.decompose()
    final_body = clean_soup.body if clean_soup.body else clean_soup
    final_html = _strip_control_chars(final_body.decode_contents()).strip()
    plain_text = _strip_control_chars(
        re.sub(r"\n{3,}", "\n\n", final_body.get_text("\n", strip=True))
    ).strip()
    return final_html, plain_text, lead_img


def extract_links_from_list_page(
    raw_html: str,
    base_url: str,
    link_selector: str,
    max_links: int = 20,
) -> list[tuple[str, str]]:
    """Extract deduplicated (url, link_text) pairs from an index/list webpage using a CSS selector."""
    if not raw_html or max_links <= 0:
        return []

    soup = BeautifulSoup(raw_html, "lxml")
    selector = (link_selector or "").strip() or "a[href]"
    try:
        elements = soup.select(selector)
    except Exception as exc:
        logger.warning("invalid_link_selector", selector=selector, err=str(exc))
        return []

    results: list[tuple[str, str]] = []
    seen_index: dict[str, int] = {}

    for el in elements:
        anchor = el if el.name == "a" else el.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        raw_href = str(anchor.get("href") or "").strip()
        if not raw_href or raw_href.startswith("#"):
            continue
        parsed_raw = urlparse(raw_href)
        if parsed_raw.scheme and parsed_raw.scheme.lower() not in ("http", "https"):
            continue

        resolved = urljoin(base_url, raw_href)
        parsed = urlparse(resolved)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            continue

        clean_url = urlunparse(parsed._replace(fragment=""))
        canon = canonicalize_url(clean_url)
        link_text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not link_text:
            link_text = re.sub(r"\s+", " ", str(anchor.get("title") or "")).strip()

        if canon in seen_index:
            idx = seen_index[canon]
            if not results[idx][1] and link_text:
                results[idx] = (results[idx][0], link_text)
            continue

        if len(results) >= max_links:
            continue

        seen_index[canon] = len(results)
        results.append((clean_url, link_text))

    return results


def extract_from_webpage(
    raw_html: str,
    url: str,
    *,
    fallback_title: str = "",
    content_selector: str = "",
    remove_selectors: list[str] | None = None,
) -> ExtractedArticle:
    """Extract clean article content with lazy-image promotion, optional CSS selectors, Trafilatura, and Readability fallback."""
    title = fallback_title
    author = ""
    extracted_html = ""
    clean_html = ""
    plain_text = ""
    lead_img: str | None = None

    soup = BeautifulSoup(_strip_control_chars(raw_html or ""), "lxml")
    _promote_lazy_images_in_soup(soup, base_url=url)

    # Extract metadata before stripping user-specified selectors
    prepared_for_meta = str(soup)
    meta_date_str: str | None = None
    try:
        meta = trafilatura.extract_metadata(prepared_for_meta, default_url=url)
        if meta:
            if not title and meta.title:
                title = meta.title.strip()
            if meta.author:
                author = meta.author.strip()
            if getattr(meta, "date", None):
                meta_date_str = str(meta.date).strip()
    except Exception:
        pass

    published_at = _parse_webpage_published_at(soup, meta_date_str)

    host = (urlparse(url).hostname or "").strip().lower()
    is_v2ex = host == "v2ex.com" or host.endswith(".v2ex.com")

    effective_removes = list(remove_selectors or [])
    if is_v2ex:
        effective_removes.extend([".reply_content", "div[id^='r_']", "#Rightbar"])

    for sel in effective_removes:
        cleaned_sel = (sel or "").strip()
        if not cleaned_sel:
            continue
        try:
            for node in soup.select(cleaned_sel):
                node.decompose()
        except Exception as exc:
            logger.warning("invalid_remove_selector", selector=cleaned_sel, err=str(exc))

    # Optional explicit CSS content selector (plus default V2EX topic selector to exclude floor noise)
    cleaned_content_sel = (content_selector or "").strip()
    if not cleaned_content_sel and is_v2ex:
        cleaned_content_sel = "div.topic_content, div.subtle"
    if cleaned_content_sel:
        selected_nodes: list[Tag] = []
        try:
            selected_nodes = list(soup.select(cleaned_content_sel))
        except Exception as exc:
            logger.warning("invalid_content_selector", selector=cleaned_content_sel, err=str(exc))
        if selected_nodes:
            selected_html = "\n".join(str(node) for node in selected_nodes)
            sel_clean, sel_plain, sel_img = sanitize_article_html(selected_html, base_url=url)
            if sel_plain:
                clean_html, plain_text, lead_img = sel_clean, sel_plain, sel_img

    prepared_html = str(soup)

    if not plain_text:
        try:
            tf_html = trafilatura.extract(
                prepared_html,
                url=url,
                output_format="html",
                include_images=True,
                include_links=True,
                include_tables=True,
                include_formatting=True,
                favor_recall=True,
            )
            if tf_html:
                extracted_html = tf_html
        except Exception:
            extracted_html = ""

        clean_html, plain_text, lead_img = sanitize_article_html(extracted_html, base_url=url)

        # Fallback to readability-lxml if trafilatura yielded too little text
        if len(plain_text) < 120:
            try:
                doc = Document(prepared_html)
                if not title:
                    title = (doc.short_title() or "").strip()
                rd_html = doc.summary(html_partial=True)
                rd_clean, rd_plain, rd_img = sanitize_article_html(rd_html, base_url=url)
                if len(rd_plain) > len(plain_text):
                    clean_html, plain_text = rd_clean, rd_plain
                    lead_img = rd_img or lead_img
            except Exception:
                pass

    if not title:
        title = soup.title.string.strip() if soup.title and soup.title.string else url

    title = _strip_control_chars(title).strip()
    author = clean_author_name(author)
    lang = detect_language(f"{title} {plain_text}")
    words, mins = estimate_word_count_and_time(plain_text)
    return ExtractedArticle(
        title=title[:500],
        author=author[:250],
        clean_html=clean_html,
        plain_text=plain_text,
        lead_image_url=lead_img,
        language=lang,
        word_count=words,
        reading_time_min=mins,
        published_at=published_at,
    )
