from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from inkpress.config import DEFAULT_INSECURE_SECRET_KEY, Settings, get_settings
from inkpress.db.base import get_session_factory
from inkpress.db.models import Article, Book, utcnow
from inkpress.main import create_app, validate_runtime_security
from inkpress.security import verify_password
from PIL import Image


async def _bootstrap_and_headers(client: httpx.AsyncClient) -> dict[str, str]:
    r = await client.post(
        "/api/v1/auth/bootstrap",
        json={
            "username": "admin",
            "password": "StrongPassword123!",
            "display_name": "Ink Reader",
            "kindle_email": "reader@example.com",
        },
    )
    assert r.status_code == 201
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _make_tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(120, 140, 160)).save(buf, format="PNG")
    return buf.getvalue()


def _make_sample_epub(path: Path) -> None:
    png_bytes = _make_tiny_png()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Sample</dc:title><dc:language>zh-CN</dc:language></metadata>"
            '<manifest><item id="c1" href="ch_001.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="img1" href="images/pic.png" media-type="image/png"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        zf.writestr(
            "OEBPS/ch_001.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch1</title></head>'
            '<body><h1>第一章</h1><p>内容正文</p><script>alert(1)</script></body></html>',
        )
        zf.writestr("OEBPS/evil.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        zf.writestr("OEBPS/images/pic.png", png_bytes)


async def test_auth_bootstrap_sources_clip_editions_and_mcp(
    test_client: httpx.AsyncClient,
) -> None:
    # 1. Uninitialized state
    r = await test_client.get("/api/v1/auth/status")
    assert r.status_code == 200
    assert r.json()["initialized"] is False

    # 2. Bootstrap admin account
    headers = await _bootstrap_and_headers(test_client)

    # 3. Settings encryption round-trip (secrets never echoed back)
    r = await test_client.put(
        "/api/v1/settings/smtp",
        headers=headers,
        json={
            "host": "smtp.example.com",
            "port": 465,
            "use_ssl": True,
            "username": "sender@example.com",
            "password": "super-secret-app-password",
            "default_kindle_email": "reader@example.com",
        },
    )
    assert r.status_code == 200
    assert r.json()["smtp"]["password_set"] is True
    assert "password" not in r.json()["smtp"]

    # 4. Clip text article and search it with total & unread_only
    r = await test_client.post(
        "/api/v1/articles/clip-text",
        headers=headers,
        json={
            "title": "自托管晨报工程笔记",
            "content": "## 核心架构\n\n使用 PostgreSQL ParadeDB 实现毫秒级检索。",
            "content_format": "markdown",
            "push_to_kindle": False,
        },
    )
    assert r.status_code == 200
    art_id = r.json()["item"]["id"]

    r = await test_client.get("/api/v1/articles?q=ParadeDB&unread_only=true", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert any(item["id"] == art_id for item in data["items"])

    # Mark read -> excluded from unread_only
    r = await test_client.patch(
        f"/api/v1/articles/{art_id}", headers=headers, json={"is_read": True}
    )
    assert r.status_code == 200
    r = await test_client.get("/api/v1/articles?q=ParadeDB&unread_only=true", headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] == 0

    # 5. Create NewsNow source & Edition (verify strip_links defaults True & retention_days 30)
    r = await test_client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "name": "Hacker News",
            "kind": "newsnow",
            "identifier": "hackernews",
            "category": "科技",
            "fetch_strategy": "newsnow_digest",
        },
    )
    assert r.status_code == 201
    src_id = r.json()["item"]["id"]

    r = await test_client.post(
        "/api/v1/editions",
        headers=headers,
        json={
            "name": "每日科技晨报",
            "cron_expr": "0 6 * * *",
            "sources": [{"source_id": src_id, "sort_order": 0}],
        },
    )
    assert r.status_code == 201
    ed = r.json()["item"]
    assert ed["next_run_at"] is not None
    assert ed["strip_links"] is True
    assert ed["retention_days"] == 30

    # Update strip_links & retention_days
    r = await test_client.patch(
        f"/api/v1/editions/{ed['id']}",
        headers=headers,
        json={"strip_links": False, "retention_days": 14},
    )
    assert r.status_code == 200
    assert r.json()["item"]["strip_links"] is False
    assert r.json()["item"]["retention_days"] == 14

    # 6. Create read-only MCP token & verify JSON-RPC enforcement
    r = await test_client.post(
        "/api/v1/settings/mcp-tokens",
        headers=headers,
        json={"name": "ai-ops-ro", "readonly": True},
    )
    assert r.status_code == 201
    mcp_token = r.json()["token"]
    mcp_headers = {"Authorization": f"Bearer {mcp_token}"}

    # Unauthenticated MCP call must be 401
    r = await test_client.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401

    # Read tool succeeds
    r = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_system_status", "arguments": {}},
        },
    )
    assert r.status_code == 200
    assert "sources_count" in r.json()["result"]["content"][0]["text"]

    # Write tool blocked by readonly token
    r = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "add_source",
                "arguments": {"name": "X", "kind": "rss", "identifier": "https://a.example/rss"},
            },
        },
    )
    assert r.status_code == 403


async def test_spa_path_traversal_and_api_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>SPA</body></html>", encoding="utf-8")
    (static_dir / "robots.txt").write_text("User-agent: *", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP_SECRET_VALUE", encoding="utf-8")

    monkeypatch.setenv("STATIC_DIR", str(static_dir))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-at-least-32-bytes-long!!")
    get_settings.cache_clear()

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Unknown API route must return 404 JSON, never 200 SPA HTML
        r_api = await client.get("/api/unknown")
        assert r_api.status_code == 404
        assert r_api.headers["content-type"].startswith("application/json")

        r_api_v1 = await client.get("/api/v1/does-not-exist")
        assert r_api_v1.status_code == 404
        assert r_api_v1.headers["content-type"].startswith("application/json")

        # Path traversal attempt must be blocked
        r_trav = await client.get("/..%2fsecret.txt")
        assert r_trav.status_code == 404
        assert "TOP_SECRET_VALUE" not in r_trav.text

        # Valid static file and SPA route still work
        assert (await client.get("/robots.txt")).status_code == 200
        r_spa = await client.get("/today")
        assert r_spa.status_code == 200
        assert "SPA" in r_spa.text

    get_settings.cache_clear()


def test_runtime_security_and_invalid_password_hash() -> None:
    prod_default = Settings(APP_ENV="production", APP_SECRET_KEY=DEFAULT_INSECURE_SECRET_KEY)
    with pytest.raises(RuntimeError, match="APP_SECRET_KEY"):
        validate_runtime_security(prod_default)

    dev_default = Settings(APP_ENV="development", APP_SECRET_KEY=DEFAULT_INSECURE_SECRET_KEY)
    validate_runtime_security(dev_default)

    prod_strong = Settings(APP_ENV="production", APP_SECRET_KEY="a-very-strong-random-key-1234567890")
    validate_runtime_security(prod_strong)

    assert verify_password("any-password", "not-a-valid-argon2-hash") is False


async def test_auth_change_password_https_cookie_and_rate_limit(
    test_client: httpx.AsyncClient,
) -> None:
    headers = await _bootstrap_and_headers(test_client)

    # Wrong current password -> 400
    r = await test_client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": "WrongPassword!", "new_password": "NewStrongPassword456!"},
    )
    assert r.status_code == 400

    # Valid change password with X-Forwarded-Proto: https sets Secure cookie
    r = await test_client.post(
        "/api/v1/auth/change-password",
        headers={**headers, "X-Forwarded-Proto": "https"},
        json={"current_password": "StrongPassword123!", "new_password": "NewStrongPassword456!"},
    )
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "Secure" in set_cookie

    # Old password fails; new password works
    r_old = await test_client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "198.51.100.10"},
        json={"username": "admin", "password": "StrongPassword123!"},
    )
    assert r_old.status_code == 401

    r_new = await test_client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "198.51.100.10"},
        json={"username": "admin", "password": "NewStrongPassword456!"},
    )
    assert r_new.status_code == 200

    # Rate limit: 10 failed attempts -> 11th returns 429
    attacker_ip = {"X-Forwarded-For": "198.51.100.99"}
    for _ in range(10):
        resp = await test_client.post(
            "/api/v1/auth/login",
            headers=attacker_ip,
            json={"username": "admin", "password": "bad-password"},
        )
        assert resp.status_code == 401

    resp_429 = await test_client.post(
        "/api/v1/auth/login",
        headers=attacker_ip,
        json={"username": "admin", "password": "NewStrongPassword456!"},
    )
    assert resp_429.status_code == 429


async def test_book_asset_hardening_upload_metadata_and_reconvert(
    test_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    headers = await _bootstrap_and_headers(test_client)

    epub_file = tmp_path / "sample.epub"
    _make_sample_epub(epub_file)
    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4\n%%EOF\n")

    factory = get_session_factory()
    async with factory() as session:
        epub_book = Book(
            kind="uploaded_book",
            title="Test EPUB",
            original_format="epub",
            original_path=str(epub_file),
            kindle_epub_path=str(epub_file),
            file_size_bytes=epub_file.stat().st_size,
            conversion_status="ready",
        )
        pdf_book = Book(
            kind="uploaded_book",
            title="Test PDF",
            original_format="pdf",
            original_path=str(pdf_file),
            kindle_epub_path=str(pdf_file),
            file_size_bytes=pdf_file.stat().st_size,
            conversion_status="failed",
            conversion_error="test failure",
        )
        session.add_all([epub_book, pdf_book])
        await session.commit()
        epub_id = epub_book.id
        pdf_id = pdf_book.id

    # Disallowed XHTML and SVG assets MUST be blocked (403)
    r_xhtml = await test_client.get(
        f"/api/v1/books/{epub_id}/asset/OEBPS/ch_001.xhtml", headers=headers
    )
    assert r_xhtml.status_code == 403

    r_svg = await test_client.get(
        f"/api/v1/books/{epub_id}/asset/OEBPS/evil.svg", headers=headers
    )
    assert r_svg.status_code == 403

    # Allowed raster PNG succeeds with hardened headers
    r_png = await test_client.get(
        f"/api/v1/books/{epub_id}/asset/OEBPS/images/pic.png", headers=headers
    )
    assert r_png.status_code == 200
    assert r_png.headers["content-type"] == "image/png"
    assert r_png.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r_png.headers["content-security-policy"]
    assert "max-age=86400" in r_png.headers["cache-control"]

    # Non-EPUB chapters endpoint returns 400 (never 500)
    r_pdf_ch = await test_client.get(f"/api/v1/books/{pdf_id}/chapters", headers=headers)
    assert r_pdf_ch.status_code == 400

    # Reconvert endpoint resets failed book
    r_reconv = await test_client.post(f"/api/v1/books/{pdf_id}/reconvert", headers=headers)
    assert r_reconv.status_code == 200
    assert r_reconv.json()["item"]["conversion_status"] == "converting"

    # Upload without custom title records has_custom_title=False
    r_up = await test_client.post(
        "/api/v1/books/upload",
        headers=headers,
        files={"file": ("my_novel.txt", b"Hello book world", "text/plain")},
    )
    assert r_up.status_code == 201
    assert r_up.json()["item"]["metadata"]["has_custom_title"] is False


async def test_source_delete_preserves_starred_articles_and_opml_import(
    test_client: httpx.AsyncClient,
) -> None:
    headers = await _bootstrap_and_headers(test_client)

    # Create source & attach a starred article
    r = await test_client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "name": "CW Magazine",
            "kind": "rsshub",
            "identifier": "rsshub://cw/today",
            "category": "商业",
        },
    )
    assert r.status_code == 201
    src_id = r.json()["item"]["id"]

    factory = get_session_factory()
    async with factory() as session:
        art = Article(
            source_id=src_id,
            canonical_url="https://example.com/starred-keep-me",
            content_hash="abc123",
            title="Important Starred Article",
            source_name="CW Magazine",
            category="商业",
            clean_html="<p>Keep me safe</p>",
            plain_text="Keep me safe",
            is_starred=True,
            published_at=utcnow(),
            fetched_at=utcnow(),
        )
        session.add(art)
        await session.commit()
        art_id = art.id

    # PATCH source kind -> webpage_list & identifier -> returns accurate article_count=1
    r_patch = await test_client.patch(
        f"/api/v1/sources/{src_id}",
        headers=headers,
        json={
            "kind": "webpage_list",
            "identifier": "https://example.com/today",
            "fetch_strategy": "byparr_browser",
            "ai_options": {"link_selector": "a[href*='/article/']"},
        },
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["item"]["kind"] == "webpage_list"
    assert r_patch.json()["item"]["identifier"] == "https://example.com/today"
    assert r_patch.json()["item"]["article_count"] == 1

    # DELETE source -> starred article MUST survive with source_id=None
    r_del = await test_client.delete(f"/api/v1/sources/{src_id}", headers=headers)
    assert r_del.status_code == 200

    r_art = await test_client.get(f"/api/v1/articles/{art_id}", headers=headers)
    assert r_art.status_code == 200
    assert r_art.json()["item"]["is_starred"] is True
    assert r_art.json()["item"]["source_id"] is None

    # OPML import: preserve nested folder category, detect rsshub://, dedup duplicates in same file
    opml_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <opml version="2.0">
      <body>
        <outline text="\xe7\xa7\x91\xe6\x8a\x80">
          <outline text="Feed A" xmlUrl="https://example.com/feed.xml" />
          <outline text="Feed A Dup" xmlUrl="https://example.com/feed.xml" />
          <outline text="RSSHub Feed" xmlUrl="rsshub://sspai/matrix" />
        </outline>
      </body>
    </opml>
    """
    r_opml = await test_client.post(
        "/api/v1/sources/import-opml",
        headers=headers,
        files={"file": ("subs.opml", opml_xml, "text/xml")},
    )
    assert r_opml.status_code == 200
    assert r_opml.json() == {"added": 2, "skipped": 1}

    sources_list = (await test_client.get("/api/v1/sources", headers=headers)).json()["items"]
    by_ident = {s["identifier"]: s for s in sources_list}
    assert by_ident["https://example.com/feed.xml"]["category"] == "科技"
    assert by_ident["https://example.com/feed.xml"]["kind"] == "rss"
    assert by_ident["rsshub://sspai/matrix"]["kind"] == "rsshub"
    assert by_ident["rsshub://sspai/matrix"]["category"] == "科技"


async def test_media_proxy_ssrf_block_and_referer_fallback(
    test_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _bootstrap_and_headers(test_client)

    # SSRF block on loopback / private IP / non-http scheme
    for bad_url in (
        "http://127.0.0.1:8000/secret.png",
        "http://192.168.1.1/admin.png",
        "file:///etc/passwd",
    ):
        r = await test_client.get(
            "/api/v1/media/proxy",
            headers=headers,
            params={"url": bad_url},
        )
        assert r.status_code == 400

    # Mock safe public URL check and smart_fetch_bytes: 403 with referer -> retries without referer -> 200
    png_bytes = _make_tiny_png()
    calls: list[str] = []

    async def fake_assert_safe(_url: str, **_kw: Any) -> None:
        return None

    async def fake_fetch_bytes(url: str, **kw: Any) -> tuple[bytes, str]:
        ref = str(kw.get("referer") or "")
        calls.append(ref)
        if ref:
            req = httpx.Request("GET", url)
            resp = httpx.Response(403, request=req)
            raise httpx.HTTPStatusError("403 Forbidden", request=req, response=resp)
        if "evil.svg" in url:
            return b"<svg></svg>", "image/svg+xml"
        return png_bytes, "image/png"

    monkeypatch.setattr("inkpress.api.media.assert_safe_public_url", fake_assert_safe)
    monkeypatch.setattr("inkpress.api.media.smart_fetch_bytes", fake_fetch_bytes)

    r_ok = await test_client.get(
        "/api/v1/media/proxy",
        headers=headers,
        params={"url": "https://cdn.example.com/photo.png", "referer": "https://example.com/post"},
    )
    assert r_ok.status_code == 200
    assert r_ok.content == png_bytes
    assert r_ok.headers["x-content-type-options"] == "nosniff"
    assert r_ok.headers["content-security-policy"] == "default-src 'none'"
    assert calls == ["https://example.com/post", ""]

    # SVG content type rejected with 415
    r_svg = await test_client.get(
        "/api/v1/media/proxy",
        headers=headers,
        params={"url": "https://cdn.example.com/evil.svg"},
    )
    assert r_svg.status_code == 415


async def test_ai_enrich_returns_502_on_error_and_422_on_empty(
    test_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _bootstrap_and_headers(test_client)
    await test_client.put(
        "/api/v1/settings/ai",
        headers=headers,
        json={"enabled": True, "api_key": "sk-test-key", "model": "gpt-4o-mini"},
    )
    r_art = await test_client.post(
        "/api/v1/articles/clip-text",
        headers=headers,
        json={"title": "AI Test", "content": "Some body content for testing.", "content_format": "text"},
    )
    art_id = r_art.json()["item"]["id"]

    async def fail_summary(_ai: Any, _art: Any) -> str:
        raise RuntimeError("LLM upstream error")

    monkeypatch.setattr("inkpress.api.articles.summarize_article", fail_summary)
    r_502 = await test_client.post(
        f"/api/v1/articles/{art_id}/ai-enrich?mode=summary", headers=headers
    )
    assert r_502.status_code == 502

    async def empty_summary(_ai: Any, _art: Any) -> str:
        return ""

    monkeypatch.setattr("inkpress.api.articles.summarize_article", empty_summary)
    r_422 = await test_client.post(
        f"/api/v1/articles/{art_id}/ai-enrich?mode=summary", headers=headers
    )
    assert r_422.status_code == 422

    async def empty_bilingual(_ai: Any, _art: Any) -> tuple[str | None, str | None]:
        return None, None

    monkeypatch.setattr("inkpress.api.articles.translate_article_bilingual", empty_bilingual)
    r_422_bi = await test_client.post(
        f"/api/v1/articles/{art_id}/ai-enrich?mode=bilingual", headers=headers
    )
    assert r_422_bi.status_code == 422


async def test_mcp_origin_check_and_new_tools(
    test_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await _bootstrap_and_headers(test_client)

    # Create read-write MCP token
    r_tok = await test_client.post(
        "/api/v1/settings/mcp-tokens",
        headers=headers,
        json={"name": "ai-ops-rw", "readonly": False},
    )
    mcp_token = r_tok.json()["token"]
    mcp_headers = {"Authorization": f"Bearer {mcp_token}"}

    # Cross-origin browser request must be rejected with 403
    r_evil = await test_client.post(
        "/api/v1/mcp",
        headers={**mcp_headers, "Origin": "https://evil.example.com"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert r_evil.status_code == 403

    # Localhost origin allowed
    r_local = await test_client.post(
        "/api/v1/mcp",
        headers={**mcp_headers, "Origin": "http://localhost:3000"},
        json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
    )
    assert r_local.status_code == 200

    # get_health tool works
    r_health = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_health", "arguments": {}},
        },
    )
    assert r_health.status_code == 200
    health_payload = json.loads(r_health.json()["result"]["content"][0]["text"])
    assert health_payload["database"]["ok"] is True
    assert "worker" in health_payload
    assert "storage" in health_payload

    # Create source & edition, then update via MCP update_source & update_edition
    r_add = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "add_source",
                "arguments": {
                    "name": "List Source",
                    "kind": "webpage_list",
                    "identifier": "https://example.com/list",
                },
            },
        },
    )
    src_id = json.loads(r_add.json()["result"]["content"][0]["text"])["id"]

    r_upd_src = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "update_source",
                "arguments": {
                    "source_id": src_id,
                    "identifier": "https://example.com/list-v2",
                    "enabled": False,
                },
            },
        },
    )
    assert r_upd_src.status_code == 200
    upd_src = json.loads(r_upd_src.json()["result"]["content"][0]["text"])
    assert upd_src["identifier"] == "https://example.com/list-v2"
    assert upd_src["enabled"] is False

    ed_resp = await test_client.post(
        "/api/v1/editions",
        headers=headers,
        json={"name": "Morning", "cron_expr": "0 6 * * *"},
    )
    ed_id = ed_resp.json()["item"]["id"]

    r_upd_ed = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "update_edition",
                "arguments": {
                    "edition_id": ed_id,
                    "strip_links": False,
                    "retention_days": 21,
                },
            },
        },
    )
    assert r_upd_ed.status_code == 200
    upd_ed = json.loads(r_upd_ed.json()["result"]["content"][0]["text"])
    assert upd_ed["strip_links"] is False
    assert upd_ed["retention_days"] == 21

    # clip_url tool blocks SSRF private URL cleanly
    r_ssrf = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "clip_url", "arguments": {"url": "http://127.0.0.1/internal"}},
        },
    )
    assert r_ssrf.status_code == 200
    assert r_ssrf.json()["result"]["isError"] is True

    # clip_url tool succeeds on mocked public fetch
    async def fake_safe(_url: str, **_kw: Any) -> None:
        return None

    async def fake_fetch_text(url: str, **_kw: Any) -> tuple[str, str]:
        return "<html><head><title>Clipped</title></head><body><article><h1>Clipped</h1><p>Long body text for clip testing.</p></article></body></html>", url

    monkeypatch.setattr("inkpress.mcp.server.assert_safe_public_url", fake_safe)
    monkeypatch.setattr("inkpress.mcp.server.smart_fetch_text", fake_fetch_text)

    r_clip = await test_client.post(
        "/api/v1/mcp",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "clip_url", "arguments": {"url": "https://example.com/post-1"}},
        },
    )
    assert r_clip.status_code == 200
    clip_data = json.loads(r_clip.json()["result"]["content"][0]["text"])
    assert clip_data["title"] == "Clipped"
