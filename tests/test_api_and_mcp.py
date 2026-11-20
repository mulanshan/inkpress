from __future__ import annotations

import httpx


async def test_auth_bootstrap_sources_clip_editions_and_mcp(
    test_client: httpx.AsyncClient,
) -> None:
    # 1. Uninitialized state
    r = await test_client.get("/api/v1/auth/status")
    assert r.status_code == 200
    assert r.json()["initialized"] is False

    # 2. Bootstrap admin account
    r = await test_client.post(
        "/api/v1/auth/bootstrap",
        json={
            "username": "admin",
            "password": "StrongPassword123!",
            "display_name": "Mulan Reader",
            "kindle_email": "reader@kindle.com",
        },
    )
    assert r.status_code == 201
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 3. Settings encryption round-trip (secrets never echoed back)
    r = await test_client.put(
        "/api/v1/settings/smtp",
        headers=headers,
        json={
            "host": "smtp.gmail.com",
            "port": 465,
            "use_ssl": True,
            "username": "sender@example.com",
            "password": "super-secret-app-password",
            "default_kindle_email": "reader@kindle.com",
        },
    )
    assert r.status_code == 200
    assert r.json()["smtp"]["password_set"] is True
    assert "password" not in r.json()["smtp"]

    # 4. Clip text article and search it
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

    r = await test_client.get("/api/v1/articles?q=ParadeDB", headers=headers)
    assert r.status_code == 200
    assert any(item["id"] == art_id for item in r.json()["items"])

    # 5. Create NewsNow source & Edition
    r = await test_client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "name": "Hacker News",
            "kind": "newsnow",
            "identifier": "hackernews",
            "category": "科技",
            "fetch_strategy": "extract_web",
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
    assert r.json()["item"]["next_run_at"] is not None

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
                "arguments": {"name": "X", "kind": "rss", "identifier": "https://a.com/rss"},
            },
        },
    )
    assert r.status_code == 403
