from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import nh3
import trafilatura
from bs4 import BeautifulSoup
from readability import Document

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
    """Normalize lazy images, resolve relative URLs, remove empty images, and sanitize via nh3."""
    if not html or not html.strip():
        return "", "", None

    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["script", "style", "iframe", "noscript", "form", "button", "svg"]):
        tag.decompose()

    lead_img: str | None = None
    for img in soup.find_all("img"):
        src = (
            img.get("data-original")
            or img.get("data-src")
            or img.get("data-actualsrc")
            or img.get("src")
            or ""
        )
        src_str = str(src).strip()
        if not src_str or src_str.startswith("data:image/svg") or src_str.startswith("javascript:"):
            img.decompose()
            continue
        if base_url and not src_str.startswith(("http://", "https://", "data:")):
            src_str = urljoin(base_url, src_str)
        img["src"] = src_str
        alt = str(img.get("alt") or "").strip()
        # Clean problematic control characters from alt (Kindle Send-to-Kindle E999 trigger)
        alt = re.sub(r"[\x00-\x1f\x7f<>\"']", " ", alt).strip()
        img["alt"] = alt[:120]
        if lead_img is None and src_str.startswith(("http://", "https://")):
            lead_img = src_str

    for a in soup.find_all("a"):
        href = str(a.get("href") or "").strip()
        if base_url and href and not href.startswith(("http://", "https://", "mailto:", "#")):
            a["href"] = urljoin(base_url, href)

    body = soup.body if soup.body else soup
    raw_clean = body.decode_contents()

    sanitized = nh3.clean(
        raw_clean,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        strip_comments=True,
    )

    # Second pass: drop empty paragraphs and broken images without src
    clean_soup = BeautifulSoup(sanitized, "lxml")
    for img in clean_soup.find_all("img"):
        if not str(img.get("src") or "").strip():
            img.decompose()
    final_body = clean_soup.body if clean_soup.body else clean_soup
    final_html = final_body.decode_contents().strip()
    plain_text = re.sub(r"\n{3,}", "\n\n", final_body.get_text("\n", strip=True)).strip()
    return final_html, plain_text, lead_img


def extract_from_webpage(
    raw_html: str,
    url: str,
    *,
    fallback_title: str = "",
) -> ExtractedArticle:
    """Extract clean article content using Trafilatura first, falling back to Readability."""
    title = fallback_title
    author = ""
    extracted_html = ""

    try:
        meta = trafilatura.extract_metadata(raw_html, default_url=url)
        if meta:
            if not title and meta.title:
                title = meta.title.strip()
            if meta.author:
                author = meta.author.strip()
        tf_html = trafilatura.extract(
            raw_html,
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
            doc = Document(raw_html)
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
        soup = BeautifulSoup(raw_html[:20000], "lxml")
        title = soup.title.string.strip() if soup.title and soup.title.string else url

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
