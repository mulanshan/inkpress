from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import nh3
import trafilatura
from bs4 import BeautifulSoup, Tag
from readability import Document

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
    "img": {"src", "alt", "title"},
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
)

PLACEHOLDER_LEAD_IMAGE_PATTERNS = (
    *SPACER_IMAGE_PATTERNS,
    "logo_rebang",
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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

        alt = str(img.get("alt") or "").strip()
        # Clean problematic control characters from alt (Kindle Send-to-Kindle E999 trigger)
        alt = re.sub(r"[\x00-\x1f\x7f<>\"']", " ", alt).strip()
        img["alt"] = alt[:120]

        if (
            lead_img is None
            and _is_valid_http_url(src_str)
            and not _is_placeholder_lead_image(src_str)
        ):
            lead_img = src_str

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
    try:
        meta = trafilatura.extract_metadata(prepared_for_meta, default_url=url)
        if meta:
            if not title and meta.title:
                title = meta.title.strip()
            if meta.author:
                author = meta.author.strip()
    except Exception:
        pass

    if remove_selectors:
        for sel in remove_selectors:
            cleaned_sel = (sel or "").strip()
            if not cleaned_sel:
                continue
            try:
                for node in soup.select(cleaned_sel):
                    node.decompose()
            except Exception as exc:
                logger.warning("invalid_remove_selector", selector=cleaned_sel, err=str(exc))

    # Optional explicit CSS content selector (e.g. webpage_list / custom editorial sites)
    cleaned_content_sel = (content_selector or "").strip()
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
    author = _strip_control_chars(author).strip()
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
    )
