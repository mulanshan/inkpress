from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from inkpress.config import get_settings
from inkpress.db.base import Base, get_engine, get_session_factory, reset_db_globals
from inkpress.db.models import Article, Source
from inkpress.ingestion.extractor import (
    canonicalize_url,
    detect_language,
    extract_from_webpage,
    extract_links_from_list_page,
    sanitize_article_html,
)
from inkpress.ingestion.fetcher import (
    assert_safe_public_url,
    decode_response_bytes,
    is_safe_public_url,
    should_use_proxy,
    smart_fetch_bytes,
    smart_fetch_text,
)
from inkpress.ingestion.newsnow_catalog import CURATED_RSS_PRESETS, NEWSNOW_BY_ID, NEWSNOW_CATALOG
from inkpress.ingestion.newsnow_client import NewsNowEntry, fetch_newsnow_source
from inkpress.ingestion.rss_client import RssEntry, resolve_feed_url
from inkpress.ingestion.service import sync_source
from sqlalchemy import select


def test_canonicalize_url_strips_tracking() -> None:
    u1 = canonicalize_url("https://Example.COM/post?utm_source=twitter&id=42&fbclid=abc#section")
    u2 = canonicalize_url("https://example.com/post?id=42")
    assert u1 == u2


def test_catalog_replacements_hackernews_and_presets() -> None:
    assert len(NEWSNOW_CATALOG) >= 40
    broken_ids = {"linuxdo", "ghxi", "smzdm", "pcbeta-windows11", "kaopu"}
    for broken in broken_ids:
        assert broken not in NEWSNOW_BY_ID

    replacement_ids = {"aihot", "fastbull-news", "chongbuluo-hot", "tieba", "bilibili-ranking"}
    for rep in replacement_ids:
        assert rep in NEWSNOW_BY_ID

    assert NEWSNOW_BY_ID["hackernews"].default_strategy == "newsnow_digest"

    presets_by_name = {p["name"]: p for p in CURATED_RSS_PRESETS}
    cw = presets_by_name["今日最新－天下雜誌"]
    assert cw["kind"] == "webpage_list"
    assert cw["identifier"] == "https://www.cw.com.tw/today"
    assert cw["fetch_strategy"] == "byparr_browser"
    assert cw["use_proxy"] == "proxy"
    assert cw["ai_options"] == {"link_selector": "a[href*='/article/']"}

    dapenti = presets_by_name["喷嚏图卦"]
    assert dapenti["ai_options"] == {"title_include_regex": "喷嚏图卦"}


def test_sanitize_article_html_cleans_scripts_schemes_controls_and_placeholders() -> None:
    raw = (
        "<div><script>alert(1)</script><p>正文第一段\x00\x0b控制符</p>"
        '<a href="javascript:alert(2)">恶意链接</a>'
        '<a href="https://example.com/ok">正常链接</a>'
        '<img src="https://cdn.example.com/logo_rebang.png" alt="logo"/>'
        '<img src="/images/v2/t.png" data-original="/img/photo.jpg" alt="hello\x00<world>"/>'
        '<img src="/images/v2/t.png" alt="pure-spacer"/>'
        '<img src="javascript:alert(3)" alt="bad"/>'
        '<img src="" alt="empty"/></div>'
    )
    clean_html, plain, lead = sanitize_article_html(raw, base_url="https://example.com/posts/1")
    assert "<script" not in clean_html
    assert "javascript:" not in clean_html
    assert "\x00" not in clean_html
    assert "\x0b" not in clean_html
    assert "\x00" not in plain
    assert "https://example.com/img/photo.jpg" in clean_html
    assert "/images/v2/t.png" not in clean_html
    assert lead == "https://example.com/img/photo.jpg"
    assert 'src=""' not in clean_html
    assert "正文第一段控制符" in plain


def test_extract_from_webpage_lazy_images_before_trafilatura_and_selectors() -> None:
    html = (
        "<html><head><title>深度报道：新一代电子纸阅读器</title></head>"
        "<body>"
        '<div class="ad-banner">广告噪声内容不应该出现在正文</div>'
        "<article class='main-body'><h1>深度报道：新一代电子纸阅读器</h1>"
        '<p><img src="/images/v2/t.png" data-original="https://img.ithome.com/newsuploadfiles/2026/11/hero.jpg" alt="电子纸实拍"/></p>'
        "<p>电子墨水屏因为低功耗、无蓝光与类纸观感，一直是深度长文阅读的理想载体。"
        "近年来随着高刷屏幕与开源聚合生态的成熟，自托管个人日报正重新焕发生机。</p>"
        "<p>结合自动化正文清洗与严格的 EPUB3 排版规范，我们可以彻底避免推送乱码与格式异常。</p>"
        '<div class="related-links">延伸阅读推荐列表</div>'
        "</article></body></html>"
    )
    ex = extract_from_webpage(
        html,
        "https://www.ithome.com/0/800/123.htm",
        remove_selectors=[".ad-banner", ".related-links"],
    )
    assert "电子纸" in ex.title
    assert ex.language == "zh-CN"
    assert ex.word_count > 40
    assert ex.lead_image_url == "https://img.ithome.com/newsuploadfiles/2026/11/hero.jpg"
    assert "t.png" not in ex.clean_html
    assert "hero.jpg" in ex.clean_html
    assert "广告噪声" not in ex.plain_text
    assert "延伸阅读推荐列表" not in ex.plain_text
    assert detect_language("This is an English article about systems engineering.") == "en"

    # Explicit content_selector extracts exact region cleanly
    ex_sel = extract_from_webpage(
        html,
        "https://www.ithome.com/0/800/123.htm",
        content_selector="article.main-body",
        remove_selectors=[".related-links"],
    )
    assert "电子墨水屏" in ex_sel.plain_text
    assert "延伸阅读推荐列表" not in ex_sel.plain_text
    assert ex_sel.lead_image_url == "https://img.ithome.com/newsuploadfiles/2026/11/hero.jpg"


def test_extract_links_from_list_page_validates_and_deduplicates() -> None:
    html = """
    <html><body>
      <div class="list">
        <a href="/article/5130001"><img src="thumb.jpg"/></a>
        <a href="/article/5130001">天下今日头条：半导体供应链重塑</a>
        <a href="https://www.cw.com.tw/article/5130002?utm_source=fb#top">第二篇专题报道</a>
        <a href="javascript:void(0)">非法脚本</a>
        <a href="mailto:test@cw.com.tw">联系邮箱</a>
        <a href="#section">页内锚点</a>
        <a href="/other/ignore">非文章链接</a>
      </div>
    </body></html>
    """
    links = extract_links_from_list_page(
        html,
        base_url="https://www.cw.com.tw/today",
        link_selector="a[href*='/article/']",
        max_links=10,
    )
    assert len(links) == 2
    assert links[0] == ("https://www.cw.com.tw/article/5130001", "天下今日头条：半导体供应链重塑")
    assert links[1][0].startswith("https://www.cw.com.tw/article/5130002")
    assert "#" not in links[1][0]
    assert links[1][1] == "第二篇专题报道"


def test_rsshub_resolution_and_proxy_routing() -> None:
    proxy = "http://127.0.0.1:7890"
    assert (
        resolve_feed_url("rsshub://cw/today", "http://rsshub:1200") == "http://rsshub:1200/cw/today"
    )
    assert should_use_proxy("https://www.economist.com/world", "auto", proxy)
    assert should_use_proxy("https://www.zaochenbao.com/news/2026", "auto", proxy)
    assert should_use_proxy("https://v2ex.com/t/1000", "auto", proxy)
    assert should_use_proxy("https://mktnews.net/flash", "auto", proxy)
    assert should_use_proxy("https://www.fastbull.com/news", "auto", proxy)
    assert should_use_proxy("https://x.com/inkpress", "auto", proxy)
    assert should_use_proxy("https://api.x.com/2/tweets", "auto", proxy)
    # Domain boundary check: box.com must NOT match x.com!
    assert not should_use_proxy("https://box.com/shared/doc", "auto", proxy)
    assert not should_use_proxy("https://www.dapenti.com/blog/rss2.asp", "auto", proxy)
    assert not should_use_proxy("http://rsshub:1200/cw/today", "auto", proxy)
    # Private IP check uses ipaddress, not naive "172." prefix
    assert not should_use_proxy("http://172.16.0.10/feed", "auto", proxy)
    assert not should_use_proxy("http://192.168.1.50/feed", "auto", proxy)


def test_decode_response_bytes_gbk_meta_and_big5() -> None:
    gbk_html = "<html><head><title>联合早报测试</title></head><body><p>新加坡国际要闻</p></body></html>"
    gbk_bytes = gbk_html.encode("gb18030")
    # No HTTP charset and no <meta charset>: must decode cleanly via gb18030 without mojibake
    decoded = decode_response_bytes(gbk_bytes, content_type="text/html")
    assert "联合早报测试" in decoded
    assert "新加坡国际要闻" in decoded
    assert "\ufffd" not in decoded

    # Explicit HTTP charset
    decoded_hdr = decode_response_bytes(gbk_bytes, content_type="text/html; charset=gb2312")
    assert "联合早报测试" in decoded_hdr

    # Big5 via <meta charset="big5">
    big5_html = '<html><head><meta charset="big5"></head><body><p>繁體中文天下雜誌測試</p></body></html>'
    decoded_big5 = decode_response_bytes(big5_html.encode("big5"), content_type="text/html")
    assert "繁體中文天下雜誌測試" in decoded_big5


async def test_assert_safe_public_url_blocks_private_and_allows_public_or_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scheme enforcement
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        await assert_safe_public_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        await assert_safe_public_url("javascript:alert(1)")

    # Direct private / loopback / link-local / multicast / reserved literals blocked
    for blocked_url in (
        "http://127.0.0.1:8060/admin",
        "http://localhost/secret",
        "http://10.0.0.1/internal",
        "http://172.16.0.5/internal",
        "http://192.168.1.50:8060/",
        "http://169.254.169.254/latest/meta-data/",
        "http://224.0.0.1/",
        "http://[::1]:8060/",
    ):
        assert not await is_safe_public_url(blocked_url)

    # Public IP literal allowed (including Cloudflare 172.64.x.x!)
    await assert_safe_public_url("https://172.64.1.1/article")
    await assert_safe_public_url("https://1.1.1.1/")

    # Internal sidecar explicitly allowed via allow_internal_hosts
    await assert_safe_public_url("http://rsshub:1200/cw/today", allow_internal_hosts={"rsshub"})

    # DNS resolving to private IP is blocked
    def fake_getaddrinfo_private(host: str, port: int) -> list[Any]:
        if host == "rebind.evil.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("inkpress.ingestion.fetcher.socket.getaddrinfo", fake_getaddrinfo_private)
    with pytest.raises(ValueError, match="non-public IP"):
        await assert_safe_public_url("https://rebind.evil.test/payload")
    await assert_safe_public_url("https://public.example.test/ok")


async def test_newsnow_client_unescapes_validates_and_sends_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "1",
                        "title": "贴吧热议 &amp; 讨论",
                        "url": "https://tieba.baidu.com/hottopic?topic_id=1&amp;topic_name=abc",
                        "extra": {"info": "热", "hover": "摘要 &amp; 详情"},
                    },
                    {
                        "id": "2",
                        "title": "恶意条目",
                        "url": "javascript:alert(1)",
                    },
                ]
            },
        )

    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "inkpress.ingestion.newsnow_client.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=transport),
    )

    entries = await fetch_newsnow_source("tieba", "http://newsnow:4444")
    assert captured_params.get("id") == "tieba"
    assert captured_params.get("latest") == "true"
    assert len(entries) == 1
    assert entries[0].url == "https://tieba.baidu.com/hottopic?topic_id=1&topic_name=abc"
    assert entries[0].title == "贴吧热议 & 讨论"

    # Empty items logs warning cleanly
    warnings: list[str] = []
    monkeypatch.setattr(
        "inkpress.ingestion.newsnow_client.logger.warning",
        lambda event, **kw: warnings.append(event),
    )
    empty_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"items": []}))
    monkeypatch.setattr(
        "inkpress.ingestion.newsnow_client.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=empty_transport),
    )
    empty_entries = await fetch_newsnow_source("tieba", "http://newsnow:4444")
    assert empty_entries == []
    assert "newsnow_empty_items" in warnings


async def test_smart_fetch_text_byparr_policy_charset_and_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "inkpress.ingestion.fetcher.socket.getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    byparr_calls: list[str] = []

    async def fake_byparr(
        url: str, byparr_base_url: str, *, timeout: float = 45.0, allow_internal_hosts: set[str] | None = None
    ) -> tuple[str, str]:
        byparr_calls.append(url)
        return "<html><body><p>Byparr bypassed OK</p></body></html>", url

    monkeypatch.setattr("inkpress.ingestion.fetcher.fetch_via_byparr", fake_byparr)

    real_async_client = httpx.AsyncClient

    # 1. 404 and 410 must NOT trigger Byparr
    for code in (404, 410):
        transport_err = httpx.MockTransport(lambda req, c=code: httpx.Response(c, text="Gone"))
        monkeypatch.setattr(
            "inkpress.ingestion.fetcher.httpx.AsyncClient",
            lambda _t=transport_err, **kwargs: real_async_client(transport=_t),
        )
        with pytest.raises(RuntimeError):
            await smart_fetch_text(
                f"https://example.com/status-{code}", byparr_base_url="http://byparr:8191"
            )
    assert byparr_calls == []

    # 1b. DNS failure must NOT trigger Byparr
    def fail_dns(host: str, port: int) -> list[Any]:
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr("inkpress.ingestion.fetcher.socket.getaddrinfo", fail_dns)
    with pytest.raises(ValueError, match="Unable to resolve"):
        await smart_fetch_text("https://nxdomain.invalid/post", byparr_base_url="http://byparr:8191")
    assert byparr_calls == []

    # Restore DNS mock
    monkeypatch.setattr(
        "inkpress.ingestion.fetcher.socket.getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    # 1c. Redirect to loopback must be blocked by SSRF guard without calling Byparr
    transport_redir = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "http://127.0.0.1:8060/secret"})
    )
    monkeypatch.setattr(
        "inkpress.ingestion.fetcher.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=transport_redir),
    )
    with pytest.raises(ValueError, match="non-public IP"):
        await smart_fetch_text("https://example.com/redir", byparr_base_url="http://byparr:8191")
    assert byparr_calls == []

    # 2. 403 Cloudflare challenge MUST trigger Byparr
    transport_cf = httpx.MockTransport(
        lambda req: httpx.Response(
            403, text="<html><title>Just a moment...</title>cf-browser-verification</html>"
        )
    )
    monkeypatch.setattr(
        "inkpress.ingestion.fetcher.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=transport_cf),
    )
    html, final_url = await smart_fetch_text(
        "https://www.cw.com.tw/today", byparr_base_url="http://byparr:8191"
    )
    assert "Byparr bypassed OK" in html
    assert final_url == "https://www.cw.com.tw/today"
    assert len(byparr_calls) == 1

    # 2b. Timeout MUST trigger Byparr fallback
    def raise_timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=req)

    transport_to = httpx.MockTransport(raise_timeout)
    monkeypatch.setattr(
        "inkpress.ingestion.fetcher.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=transport_to),
    )
    html_to, _ = await smart_fetch_text(
        "https://slow.example.com/page", byparr_base_url="http://byparr:8191"
    )
    assert "Byparr bypassed OK" in html_to
    assert len(byparr_calls) == 2

    # 3. GBK body without HTTP charset decoded cleanly
    gbk_body = "<html><body><p>联合早报无头编码正文</p></body></html>".encode("gb18030")
    transport_gbk = httpx.MockTransport(
        lambda req: httpx.Response(200, content=gbk_body, headers={"content-type": "text/html"})
    )
    monkeypatch.setattr(
        "inkpress.ingestion.fetcher.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=transport_gbk),
    )
    text, _ = await smart_fetch_text("https://www.zaochenbao.com/news/1")
    assert "联合早报无头编码正文" in text

    # 4. Size cap enforced on both smart_fetch_text and smart_fetch_bytes
    big_payload = b"A" * 2048
    transport_big = httpx.MockTransport(lambda req: httpx.Response(200, content=big_payload))
    monkeypatch.setattr(
        "inkpress.ingestion.fetcher.httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=transport_big),
    )
    with pytest.raises(ValueError, match="limit"):
        await smart_fetch_text("https://example.com/huge", max_bytes=512)
    with pytest.raises(ValueError, match="limit"):
        await smart_fetch_bytes("https://example.com/huge.jpg", max_bytes=512)


@pytest.fixture
async def db_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db_file = tmp_path / "service_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("LIBRARY_DATA_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-at-least-32-bytes-long!!")
    get_settings.cache_clear()
    reset_db_globals()

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = get_session_factory()
    async with factory() as session:
        yield session

    await engine.dispose()
    reset_db_globals()
    get_settings.cache_clear()


async def test_sync_source_newsnow_digest_daily_update_empty_error_and_xss(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = Source(
        name="知乎热榜",
        kind="newsnow",
        identifier="zhihu",
        category="综合",
        fetch_strategy="newsnow_digest",
        max_items=10,
    )
    db_session.add(src)
    await db_session.commit()

    # 1. Empty return must record error and increment consecutive_failures
    async def fake_empty(source_id: str, base_url: str, *, latest: bool = True, timeout: float = 20.0) -> list[NewsNowEntry]:
        assert latest is True
        return []

    monkeypatch.setattr("inkpress.ingestion.service.fetch_newsnow_source", fake_empty)
    stats_empty = await sync_source(db_session, src)
    assert stats_empty == {"new": 0, "skipped": 0, "failed": 0}
    assert src.consecutive_failures == 1
    assert src.last_error is not None

    # 2. First real sync creates daily digest and strips javascript: hrefs
    now = datetime.now(UTC)
    batch1 = [
        NewsNowEntry("1", "热点一", "https://zhihu.com/q/1", "", now, "简介一", "100万", {}),
        NewsNowEntry("2", "危险链接", "javascript:alert(1)", "", now, "注入测试", "", {}),
    ]

    async def fake_batch1(source_id: str, base_url: str, *, latest: bool = True, timeout: float = 20.0) -> list[NewsNowEntry]:
        assert latest is True
        return batch1

    monkeypatch.setattr("inkpress.ingestion.service.fetch_newsnow_source", fake_batch1)
    stats1 = await sync_source(db_session, src)
    assert stats1["new"] == 1
    assert src.consecutive_failures == 0
    assert src.last_error is None

    arts1 = (await db_session.execute(select(Article))).scalars().all()
    assert len(arts1) == 1
    assert "javascript:" not in arts1[0].clean_html
    assert "热点一" in arts1[0].plain_text

    # 3. Same-day re-sync updates existing digest IN PLACE instead of creating a duplicate
    batch2 = [
        NewsNowEntry("1", "热点一", "https://zhihu.com/q/1", "", now, "简介一", "120万", {}),
        NewsNowEntry("3", "新增热点三", "https://zhihu.com/q/3", "", now, "简介三", "80万", {}),
    ]

    async def fake_batch2(source_id: str, base_url: str, *, latest: bool = True, timeout: float = 20.0) -> list[NewsNowEntry]:
        return batch2

    monkeypatch.setattr("inkpress.ingestion.service.fetch_newsnow_source", fake_batch2)
    stats2 = await sync_source(db_session, src)
    assert stats2["new"] == 0
    assert stats2["skipped"] == 1

    arts2 = (await db_session.execute(select(Article))).scalars().all()
    assert len(arts2) == 1
    assert arts2[0].id == arts1[0].id
    assert "新增热点三" in arts2[0].plain_text


async def test_sync_source_rss_regex_filter_and_webpage_list(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. RSS with title_include_regex & title_exclude_regex (e.g. 喷嚏图卦)
    rss_src = Source(
        name="喷嚏图卦",
        kind="rss",
        identifier="https://www.dapenti.com/blog/rss2.asp?name=xilei",
        category="综合",
        fetch_strategy="fulltext_rss",
        ai_options={"title_include_regex": "喷嚏图卦", "title_exclude_regex": "广告"},
        max_items=5,
    )
    db_session.add(rss_src)
    await db_session.commit()

    now = datetime.now(UTC)
    long_body = "<p>" + "这是一段足够长的喷嚏图卦正文内容，用于验证全文 RSS 抓取与正则过滤。" * 10 + "</p>"

    async def fake_rss(*args: Any, **kwargs: Any) -> tuple[str, list[RssEntry]]:
        return (
            "喷嚏网",
            [
                RssEntry("【喷嚏图卦20261121】今日世界要闻", "https://dapenti.com/1", "xilei", now, long_body, "", True),
                RssEntry("【喷嚏图卦】广告推广专场", "https://dapenti.com/2", "xilei", now, long_body, "", True),
                RssEntry("【意图】每日一图", "https://dapenti.com/3", "xilei", now, long_body, "", True),
            ],
        )

    monkeypatch.setattr("inkpress.ingestion.service.fetch_rss_entries", fake_rss)
    rss_stats = await sync_source(db_session, rss_src)
    assert rss_stats["new"] == 1
    assert rss_stats["skipped"] == 2

    rss_arts = (
        await db_session.execute(select(Article).where(Article.source_id == rss_src.id))
    ).scalars().all()
    assert len(rss_arts) == 1
    assert "20261121" in rss_arts[0].title

    # 2. webpage_list source with link_selector, content_selector, remove_selectors
    wl_src = Source(
        name="天下杂志今日",
        kind="webpage_list",
        identifier="https://www.cw.com.tw/today",
        category="财经",
        fetch_strategy="byparr_browser",
        ai_options={
            "link_selector": "a[href*='/article/']",
            "content_selector": "div.article-body",
            "remove_selectors": [".promo-box"],
            "title_exclude_regex": "業配",
        },
        max_items=5,
    )
    db_session.add(wl_src)
    await db_session.commit()

    list_page_html = """
    <html><body>
      <a href="/article/5131001">台積電全球佈局新篇章</a>
      <a href="/article/5131002">業配贊助專文</a>
    </body></html>
    """
    article_page_html = """
    <html><head><title>台積電全球佈局新篇章</title></head><body>
      <div class="article-body">
        <p>台積電在熊本與亞利桑那州的新廠進度持續推進，帶動整體半導體設備與材料供應鏈跨國協同升級。</p>
        <p>業內分析師指出，先進製程產能多元化將成為未來五年全球科技產業的核心變數。</p>
        <div class="promo-box">立即訂閱天下雜誌會員</div>
      </div>
    </body></html>
    """

    async def fake_fetch(url: str, **kwargs: Any) -> tuple[str, str]:
        if url.endswith("/today"):
            return list_page_html, url
        return article_page_html, url

    monkeypatch.setattr("inkpress.ingestion.service.smart_fetch_text", fake_fetch)
    wl_stats = await sync_source(db_session, wl_src)
    assert wl_stats["new"] == 1
    assert wl_stats["skipped"] == 1

    wl_arts = (
        await db_session.execute(select(Article).where(Article.source_id == wl_src.id))
    ).scalars().all()
    assert len(wl_arts) == 1
    assert "台積電" in wl_arts[0].title
    assert "立即訂閱天下雜誌會員" not in wl_arts[0].plain_text

    # 3. Invalid regex in ai_options is guarded cleanly without crashing sync
    bad_re_src = Source(
        name="非法正则源",
        kind="rss",
        identifier="https://example.com/bad-regex.xml",
        category="综合",
        fetch_strategy="fulltext_rss",
        ai_options={"title_include_regex": "[unclosed", "title_exclude_regex": "(bad"},
        max_items=5,
    )
    db_session.add(bad_re_src)
    await db_session.commit()

    async def fake_rss_bad_re(*args: Any, **kwargs: Any) -> tuple[str, list[RssEntry]]:
        return (
            "Test",
            [RssEntry("正常文章", "https://example.com/valid-after-bad-re", "auth", now, long_body, "", True)],
        )

    monkeypatch.setattr("inkpress.ingestion.service.fetch_rss_entries", fake_rss_bad_re)
    bad_re_stats = await sync_source(db_session, bad_re_src)
    assert bad_re_stats["new"] == 1
    assert bad_re_src.last_error is None


async def test_sync_source_rolls_back_session_on_flush_error(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = Source(
        name="异常源",
        kind="webpage",
        identifier="https://example.com/fail-flush",
        category="科技",
        fetch_strategy="extract_web",
    )
    db_session.add(src)
    await db_session.commit()

    async def fake_fetch(url: str, **kwargs: Any) -> tuple[str, str]:
        return "<html><body><article><p>" + "测试正文内容" * 30 + "</p></article></body></html>", url

    async def boom_upsert(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated db flush failure")

    monkeypatch.setattr("inkpress.ingestion.service.smart_fetch_text", fake_fetch)
    monkeypatch.setattr("inkpress.ingestion.service._upsert_article", boom_upsert)

    with pytest.raises(RuntimeError, match="simulated db flush failure"):
        await sync_source(db_session, src)

    # Session must remain usable after rollback & error recording
    refreshed = await db_session.get(Source, src.id)
    assert refreshed is not None
    assert refreshed.consecutive_failures == 1
    assert refreshed.last_error is not None
    assert "simulated db flush failure" in refreshed.last_error
