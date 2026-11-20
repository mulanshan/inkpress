from __future__ import annotations

from inkpress.ingestion.extractor import (
    canonicalize_url,
    detect_language,
    extract_from_webpage,
    sanitize_article_html,
)
from inkpress.ingestion.fetcher import should_use_proxy
from inkpress.ingestion.newsnow_catalog import NEWSNOW_CATALOG
from inkpress.ingestion.rss_client import resolve_feed_url


def test_canonicalize_url_strips_tracking() -> None:
    u1 = canonicalize_url("https://Example.COM/post?utm_source=twitter&id=42&fbclid=abc#section")
    u2 = canonicalize_url("https://example.com/post?id=42")
    assert u1 == u2


def test_sanitize_article_html_cleans_scripts_and_lazy_imgs() -> None:
    raw = (
        "<div><script>alert(1)</script><p>正文第一段</p>"
        '<img data-src="/img/photo.jpg" alt="hello\x00<world>"/>'
        '<img src="" alt="empty"/></div>'
    )
    clean_html, plain, lead = sanitize_article_html(raw, base_url="https://example.com/posts/1")
    assert "<script" not in clean_html
    assert "https://example.com/img/photo.jpg" in clean_html
    assert lead == "https://example.com/img/photo.jpg"
    assert 'src=""' not in clean_html
    assert "正文第一段" in plain


def test_extract_from_webpage_and_language() -> None:
    html = (
        "<html><head><title>深度报道：新一代电子纸阅读器</title></head>"
        "<body><article><h1>深度报道：新一代电子纸阅读器</h1>"
        "<p>电子墨水屏因为低功耗、无蓝光与类纸观感，一直是深度长文阅读的理想载体。"
        "近年来随着高刷屏幕与开源聚合生态的成熟，自托管个人日报正重新焕发生机。</p>"
        "<p>结合自动化正文清洗与严格的 EPUB3 排版规范，我们可以彻底避免推送乱码与格式异常。</p>"
        "</article></body></html>"
    )
    ex = extract_from_webpage(html, "https://example.com/eink")
    assert "电子纸" in ex.title
    assert ex.language == "zh-CN"
    assert ex.word_count > 40
    assert detect_language("This is an English article about systems engineering.") == "en"


def test_rsshub_resolution_and_proxy_routing() -> None:
    assert (
        resolve_feed_url("rsshub://cw/today", "http://rsshub:1200") == "http://rsshub:1200/cw/today"
    )
    assert should_use_proxy("https://www.economist.com/world", "auto", "http://127.0.0.1:7890")
    assert not should_use_proxy(
        "https://www.dapenti.com/blog/rss2.asp", "auto", "http://127.0.0.1:7890"
    )
    assert not should_use_proxy("http://rsshub:1200/cw/today", "auto", "http://127.0.0.1:7890")
    assert len(NEWSNOW_CATALOG) >= 40
