from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from inkpress.config import get_settings
from inkpress.logging import get_logger

logger = get_logger(__name__)

EVENTS_CHANNEL = "inkpress:events"
JOBS_QUEUE_KEY = "inkpress:jobs"  # Legacy key name retained for compatibility
JOBS_STREAM_KEY = "inkpress:jobs:stream"
JOBS_GROUP_NAME = "inkpress-workers"
WORKER_HEARTBEAT_KEY = "inkpress:worker:heartbeat"


async def publish_event(event_type: str, payload: dict[str, Any]) -> None:
    """Publish real-time job/delivery telemetry to Redis pub/sub for SSE clients."""
    try:
        client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            message = json.dumps(
                {"type": event_type, "data": payload}, ensure_ascii=False, default=str
            )
            await client.publish(EVENTS_CHANNEL, message)
        finally:
            await client.aclose()
    except Exception as exc:
        logger.debug("sse_publish_skipped", event_type=event_type, err=str(exc))


async def ensure_consumer_group(client: aioredis.Redis) -> None:
    """Ensure the Redis Streams consumer group exists (idempotent)."""
    try:
        await client.xgroup_create(JOBS_STREAM_KEY, JOBS_GROUP_NAME, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue_job(job_type: str, kwargs: dict[str, Any]) -> str:
    """Append an async job payload onto the Redis Streams worker queue."""
    job_id = uuid.uuid4().hex[:12]
    payload = json.dumps(
        {"id": job_id, "type": job_type, "kwargs": kwargs},
        ensure_ascii=False,
        default=str,
    )
    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await client.xadd(
            JOBS_STREAM_KEY,
            {"payload": payload, "job_id": job_id, "type": job_type},
            maxlen=10000,
            approximate=True,
        )
    finally:
        await client.aclose()
    logger.info("job_enqueued", job_id=job_id, job_type=job_type)
    return job_id


async def publish_worker_heartbeat(
    client: aioredis.Redis,
    *,
    ttl_seconds: int = 90,
) -> dict[str, Any]:
    """Publish worker liveness & tool availability to Redis."""
    heartbeat: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "calibre_ebook_convert": shutil.which("ebook-convert") is not None,
        "pandoc": shutil.which("pandoc") is not None,
        "pid": os.getpid(),
    }
    await client.set(
        WORKER_HEARTBEAT_KEY,
        json.dumps(heartbeat, ensure_ascii=False),
        ex=ttl_seconds,
    )
    return heartbeat


async def read_worker_heartbeat(client: aioredis.Redis) -> dict[str, Any] | None:
    raw = await client.get(WORKER_HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def get_stream_queue_depth(client: aioredis.Redis) -> int:
    try:
        return int(await client.xlen(JOBS_STREAM_KEY))
    except Exception:
        return 0


async def has_live_worker(*, require_calibre: bool = False) -> bool:
    """Return True if an active `inkpress-worker` published a fresh heartbeat in Redis."""
    try:
        client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            hb = await read_worker_heartbeat(client)
            if not hb:
                return False
            return not (require_calibre and not hb.get("calibre_ebook_convert"))
        finally:
            await client.aclose()
    except Exception:
        return False
