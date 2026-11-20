from __future__ import annotations

import shutil
from datetime import UTC, datetime
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
from inkpress.worker.broker import get_stream_queue_depth, read_worker_heartbeat

router = APIRouter(prefix="/api/v1/health", tags=["health"])

WORKER_HEARTBEAT_MAX_AGE_SECONDS = 90.0


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


def _heartbeat_is_fresh(ts_str: str | None) -> bool:
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - dt).total_seconds()
        return age <= WORKER_HEARTBEAT_MAX_AGE_SECONDS
    except ValueError:
        return False


async def collect_deep_health(session: AsyncSession) -> dict[str, Any]:
    """Collect deep diagnostics across database, Redis, worker, sidecars, storage, and tools."""
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
    worker_status: dict[str, Any] = {"ok": False, "queue_depth": 0, "last_seen": None}
    worker_heartbeat: dict[str, Any] | None = None

    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            pong = await r.execute_command("PING")
            redis_status = {"ok": bool(pong)}
            if redis_status["ok"]:
                queue_depth = await get_stream_queue_depth(r)
                worker_heartbeat = await read_worker_heartbeat(r)
                last_seen = str(worker_heartbeat.get("ts")) if worker_heartbeat and worker_heartbeat.get("ts") else None
                worker_ok = _heartbeat_is_fresh(last_seen)
                worker_status = {
                    "ok": worker_ok,
                    "queue_depth": queue_depth,
                    "last_seen": last_seen,
                }
        finally:
            await r.aclose()
    except Exception as exc:
        redis_status = {"ok": False, "error": str(exc)[:200]}

    newsnow_status = await _probe_http(net["newsnow_base_url"])
    rsshub_status = await _probe_http(net["rsshub_base_url"])
    byparr_status = await _probe_http(f"{net['byparr_base_url'].rstrip('/')}/openapi.json")

    if worker_status["ok"] and worker_heartbeat is not None:
        tools = {
            "calibre_ebook_convert": bool(worker_heartbeat.get("calibre_ebook_convert", False)),
            "pandoc": bool(worker_heartbeat.get("pandoc", False)),
        }
    else:
        tools = {
            "calibre_ebook_convert": shutil.which("ebook-convert") is not None,
            "pandoc": shutil.which("pandoc") is not None,
        }

    storage_status: dict[str, Any] = {"free_gb": 0.0}
    try:
        storage_dir = settings.library_data_dir
        storage_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(storage_dir)
        storage_status = {
            "free_gb": round(usage.free / (1024**3), 2),
            "total_gb": round(usage.total / (1024**3), 2),
            "path": str(storage_dir),
        }
    except Exception as exc:
        storage_status = {"free_gb": 0.0, "error": str(exc)[:200]}

    return {
        "version": __version__,
        "database": db_status,
        "redis": redis_status,
        "worker": worker_status,
        "newsnow": newsnow_status,
        "rsshub": rsshub_status,
        "byparr": byparr_status,
        "storage": storage_status,
        "smtp_configured": bool(smtp.get("host") and smtp.get("username") and smtp.get("password")),
        "kindle_email_configured": bool(smtp.get("default_kindle_email")),
        "ai_enabled": bool(ai.get("enabled") and ai.get("api_key")),
        "ai_model": ai.get("model") if ai.get("enabled") else None,
        "tools": tools,
    }


@router.get("")
async def deep_health(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Authenticated deep diagnostics across database, Redis, worker, NewsNow, RSSHub, Byparr, storage, and tools."""
    return await collect_deep_health(session)
