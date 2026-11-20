from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.api.deps import get_current_user
from inkpress.config import get_settings
from inkpress.db.base import get_db
from inkpress.db.models import Delivery, User
from inkpress.worker.broker import EVENTS_CHANNEL
from inkpress.worker.tasks import (
    build_and_deliver_edition_task,
    deliver_book_task,
    run_build_and_deliver_edition,
    run_deliver_book,
)

router = APIRouter(prefix="/api/v1/deliveries", tags=["deliveries"])


def _delivery_dict(d: Delivery) -> dict[str, Any]:
    return {
        "id": d.id,
        "edition_id": d.edition_id,
        "book_id": d.book_id,
        "title": d.title,
        "trigger": d.trigger,
        "target_email": d.target_email,
        "status": d.status,
        "article_count": d.article_count,
        "volume_count": d.volume_count,
        "file_size_bytes": d.file_size_bytes,
        "duration_ms": d.duration_ms,
        "stage_timings": d.stage_timings or {},
        "error_message": d.error_message,
        "created_at": d.created_at.isoformat(),
        "finished_at": d.finished_at.isoformat() if d.finished_at else None,
    }


@router.get("")
async def list_deliveries(
    limit: int = 50,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        (await session.execute(select(Delivery).order_by(desc(Delivery.id)).limit(min(limit, 200))))
        .scalars()
        .all()
    )
    total_sent = int(
        (
            await session.execute(select(func.count(Delivery.id)).where(Delivery.status == "sent"))
        ).scalar_one()
    )
    total_failed = int(
        (
            await session.execute(
                select(func.count(Delivery.id)).where(Delivery.status == "failed")
            )
        ).scalar_one()
    )
    return {
        "items": [_delivery_dict(d) for d in rows],
        "summary": {"total_sent": total_sent, "total_failed": total_failed},
    }


@router.post("/{delivery_id}/retry")
async def retry_delivery(
    delivery_id: int,
    background_tasks: BackgroundTasks,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    d = await session.get(Delivery, delivery_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    if d.book_id:
        try:
            await deliver_book_task.kiq(d.book_id, target_email=d.target_email, trigger="retry")
        except Exception:
            background_tasks.add_task(
                run_deliver_book, d.book_id, target_email=d.target_email, trigger="retry"
            )
        return {"status": "queued", "book_id": d.book_id}
    if d.edition_id:
        try:
            await build_and_deliver_edition_task.kiq(d.edition_id, trigger="retry", send_email=True)
        except Exception:
            background_tasks.add_task(
                run_build_and_deliver_edition, d.edition_id, trigger="retry", send_email=True
            )
        return {"status": "queued", "edition_id": d.edition_id}
    raise HTTPException(status_code=400, detail="无法重试该记录")


@router.get("/stream")
async def stream_delivery_events(
    _user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events endpoint forwarding Redis job telemetry to Studio UI."""

    async def event_gen() -> AsyncIterator[str]:
        yield 'data: {"type":"connected"}\n\n'
        client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(EVENTS_CHANNEL)
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                if msg and msg.get("data"):
                    yield f"data: {msg['data']}\n\n"
                else:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        except Exception:
            yield 'data: {"type":"disconnected"}\n\n'
        finally:
            await pubsub.aclose()
            await client.aclose()

    return StreamingResponse(event_gen(), media_type="text/event-stream")
