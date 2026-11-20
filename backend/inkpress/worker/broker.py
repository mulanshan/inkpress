from __future__ import annotations

import json
import uuid
from typing import Any

import redis.asyncio as aioredis

from inkpress.config import get_settings
from inkpress.logging import get_logger

logger = get_logger(__name__)

EVENTS_CHANNEL = "inkpress:events"
JOBS_QUEUE_KEY = "inkpress:jobs"


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


async def enqueue_job(job_type: str, kwargs: dict[str, Any]) -> str:
    """Push an async job payload onto the Redis worker queue."""
    job_id = uuid.uuid4().hex[:12]
    payload = json.dumps(
        {"id": job_id, "type": job_type, "kwargs": kwargs},
        ensure_ascii=False,
        default=str,
    )
    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await client.lpush(JOBS_QUEUE_KEY, payload)  # type: ignore[misc]
    finally:
        await client.aclose()
    logger.info("job_enqueued", job_id=job_id, job_type=job_type)
    return job_id
