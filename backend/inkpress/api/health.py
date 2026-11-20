from __future__ import annotations

import shutil
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress import __version__
from inkpress.api.deps import get_current_user
from inkpress.config import get_settings
from inkpress.db.base import get_db
from inkpress.db.models import User
from inkpress.settings_store import get_ai_config, get_network_config, get_smtp_config

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


async def _probe_http(url: str) -> dict[str, Any]:
    if not url:
        return {"ok": False, "status": "unconfigured"}
    try:
        async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
            resp = await client.get(url)
            return {"ok": resp.status_code < 500, "http_status": resp.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@router.get("")
async def deep_health(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Authenticated deep diagnostics across database, Redis, NewsNow, RSSHub, Byparr, and tools."""
    settings = get_settings()
    net = await get_network_config(session)
    smtp = await get_smtp_config(session)
    ai = await get_ai_config(session)

    db_status: dict[str, Any] = {"ok": False}
    try:
        await session.execute(text("SELECT 1"))
        db_status = {
            "ok": True,
            "dialect": session.bind.dialect.name if session.bind else "unknown",
        }
    except Exception as exc:
        db_status = {"ok": False, "error": str(exc)[:200]}

    redis_status: dict[str, Any] = {"ok": False}
    try:
        r = aioredis.from_url(settings.redis_url)
        try:
            pong = await r.execute_command("PING")
            redis_status = {"ok": bool(pong)}
        finally:
            await r.aclose()
    except Exception as exc:
        redis_status = {"ok": False, "error": str(exc)[:200]}

    newsnow_status = await _probe_http(net["newsnow_base_url"])
    rsshub_status = await _probe_http(net["rsshub_base_url"])
    byparr_status = await _probe_http(f"{net['byparr_base_url'].rstrip('/')}/openapi.json")

    return {
        "version": __version__,
        "database": db_status,
        "redis": redis_status,
        "newsnow": newsnow_status,
        "rsshub": rsshub_status,
        "byparr": byparr_status,
        "smtp_configured": bool(smtp.get("host") and smtp.get("username") and smtp.get("password")),
        "kindle_email_configured": bool(smtp.get("default_kindle_email")),
        "ai_enabled": bool(ai.get("enabled") and ai.get("api_key")),
        "ai_model": ai.get("model") if ai.get("enabled") else None,
        "tools": {
            "calibre_ebook_convert": shutil.which("ebook-convert") is not None,
            "pandoc": shutil.which("pandoc") is not None,
        },
    }
