from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

import redis.asyncio as aioredis

from inkpress.config import get_settings
from inkpress.logging import configure_logging, get_logger
from inkpress.worker.broker import JOBS_QUEUE_KEY
from inkpress.worker.tasks import (
    run_build_and_deliver_edition,
    run_convert_book,
    run_deliver_book,
    run_sync_source,
)

logger = get_logger(__name__)


async def dispatch_job(job: dict[str, Any]) -> Any:
    job_type = str(job.get("type") or "")
    raw_kwargs = job.get("kwargs")
    kwargs: dict[str, Any] = raw_kwargs if isinstance(raw_kwargs, dict) else {}
    if job_type == "sync_source":
        return await run_sync_source(int(kwargs["source_id"]))
    if job_type == "build_and_deliver_edition":
        return await run_build_and_deliver_edition(
            int(kwargs["edition_id"]),
            trigger=str(kwargs.get("trigger") or "manual"),
            send_email=bool(kwargs.get("send_email", True)),
        )
    if job_type == "convert_book":
        return await run_convert_book(int(kwargs["book_id"]))
    if job_type == "deliver_book":
        return await run_deliver_book(
            int(kwargs["book_id"]),
            target_email=str(kwargs.get("target_email") or ""),
            trigger=str(kwargs.get("trigger") or "manual"),
        )
    raise ValueError(f"Unknown job type: {job_type}")


async def _worker_loop(worker_idx: int, stop_event: asyncio.Event) -> None:
    cfg = get_settings()
    client = aioredis.from_url(cfg.redis_url, decode_responses=True)
    logger.info("worker_started", worker_idx=worker_idx)
    try:
        while not stop_event.is_set():
            try:
                item = await client.brpop([JOBS_QUEUE_KEY], timeout=5)  # type: ignore[misc]
                if not item:
                    continue
                _, raw = item
                job = json.loads(raw)
                logger.info(
                    "job_started", worker_idx=worker_idx, job_id=job.get("id"), type=job.get("type")
                )
                await dispatch_job(job)
                logger.info(
                    "job_finished",
                    worker_idx=worker_idx,
                    job_id=job.get("id"),
                    type=job.get("type"),
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("job_failed", worker_idx=worker_idx, err=str(exc))
                await asyncio.sleep(1)
    finally:
        await client.aclose()


async def main(concurrency: int = 2) -> None:
    configure_logging()
    get_settings().ensure_storage_dirs()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    workers = [asyncio.create_task(_worker_loop(i, stop_event)) for i in range(concurrency)]
    await asyncio.gather(*workers)


if __name__ == "__main__":
    asyncio.run(main())
