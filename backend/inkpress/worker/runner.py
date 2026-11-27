from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from sqlalchemy import select

from inkpress.config import get_settings
from inkpress.db.base import get_session_factory
from inkpress.db.models import Book, Delivery, utcnow
from inkpress.logging import configure_logging, get_logger
from inkpress.worker.broker import (
    JOBS_GROUP_NAME,
    JOBS_STREAM_KEY,
    ensure_consumer_group,
    publish_worker_heartbeat,
)
from inkpress.worker.tasks import (
    run_build_and_deliver_edition,
    run_convert_book,
    run_deliver_book,
    run_retry_delivery,
    run_sync_source,
)

logger = get_logger(__name__)

STALE_DELIVERY_MINUTES = 15
HEARTBEAT_INTERVAL_SECONDS = 20.0
RECLAIM_INTERVAL_SECONDS = 60.0
RECLAIM_MIN_IDLE_MS = 300_000
LEASE_RENEW_INTERVAL_SECONDS = 60.0
MAX_STREAM_DELIVERIES = 3
DEAD_LETTER_STREAM_KEY = "inkpress:jobs:dlq"


async def dispatch_job(job: dict[str, Any]) -> Any:
    job_type = str(job.get("type") or "")
    raw_kwargs = job.get("kwargs")
    kwargs: dict[str, Any] = raw_kwargs if isinstance(raw_kwargs, dict) else {}
    if job_type == "sync_source":
        return await run_sync_source(int(kwargs["source_id"]))
    if job_type == "build_and_deliver_edition":
        deliv_id = kwargs.get("delivery_id")
        return await run_build_and_deliver_edition(
            int(kwargs["edition_id"]),
            trigger=str(kwargs.get("trigger") or "manual"),
            send_email=bool(kwargs.get("send_email", True)),
            ignore_dedup=bool(kwargs.get("ignore_dedup", False)),
            delivery_id=int(deliv_id) if deliv_id is not None else None,
        )
    if job_type == "convert_book":
        return await run_convert_book(int(kwargs["book_id"]))
    if job_type == "deliver_book":
        return await run_deliver_book(
            int(kwargs["book_id"]),
            target_email=str(kwargs.get("target_email") or ""),
            trigger=str(kwargs.get("trigger") or "manual"),
        )
    if job_type == "retry_delivery":
        return await run_retry_delivery(
            int(kwargs["delivery_id"]),
            trigger=str(kwargs.get("trigger") or "retry"),
        )
    raise ValueError(f"Unknown job type: {job_type}")


async def recover_stale_running_deliveries(stale_minutes: int = STALE_DELIVERY_MINUTES) -> int:
    """Mark stale 'pending' / 'running' / 'sending_smtp' deliveries and 'converting' books as failed on startup."""
    factory = get_session_factory()
    now = utcnow()
    cutoff = now - timedelta(minutes=max(1, stale_minutes))
    recovered = 0
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(Delivery).where(
                        Delivery.status.in_(("pending", "running", "sending_smtp")),
                        Delivery.created_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for delivery in rows:
            delivery.status = "failed"
            delivery.error_message = (
                delivery.error_message
                or "Worker restarted while delivery was still marked running (stale recovery)"
            )
            delivery.finished_at = now
            recovered += 1

        stale_books = (
            (
                await session.execute(
                    select(Book).where(
                        Book.conversion_status == "converting",
                        Book.created_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for book in stale_books:
            book.conversion_status = "failed"
            book.conversion_error = (
                book.conversion_error
                or "Worker restarted while book conversion was still in progress (stale recovery)"
            )
            recovered += 1

        if recovered:
            await session.commit()
            logger.warning("recovered_stale_running_deliveries", count=recovered)
    return recovered


async def _mark_evicted_job_failed_in_db(raw_payload: str, times: int) -> None:
    """Mark any Delivery or Book linked to a poison-pill stream job as failed upon DLQ eviction."""
    if not raw_payload:
        return
    try:
        parsed = json.loads(raw_payload)
    except Exception:
        return
    if not isinstance(parsed, dict):
        return
    job_type = str(parsed.get("type") or "")
    raw_kw = parsed.get("kwargs")
    kw: dict[str, Any] = raw_kw if isinstance(raw_kw, dict) else {}

    deliv_id = kw.get("delivery_id")
    book_id = kw.get("book_id") if job_type == "convert_book" else None
    if deliv_id is None and book_id is None:
        return

    try:
        factory = get_session_factory()
        now = utcnow()
        async with factory() as session:
            dirty = False
            if deliv_id is not None:
                deliv = await session.get(Delivery, int(deliv_id))
                if deliv is not None and deliv.status in ("pending", "running", "sending_smtp"):
                    deliv.status = "failed"
                    deliv.error_message = (
                        f"任务连续异常中断 {times} 次，已移入死信队列 (DLQ)"
                    )
                    deliv.finished_at = now
                    dirty = True
            if book_id is not None:
                book = await session.get(Book, int(book_id))
                if book is not None and book.conversion_status == "converting":
                    book.conversion_status = "failed"
                    book.conversion_error = (
                        f"转码连续异常中断 {times} 次，已移入死信队列 (DLQ)"
                    )
                    dirty = True
            if dirty:
                await session.commit()
    except Exception as exc:
        logger.debug("dlq_db_fail_mark_skipped", err=str(exc))


async def _process_stream_entry(
    client: aioredis.Redis,
    msg_id: str,
    fields: dict[str, Any],
    *,
    worker_label: str,
    lease_renew_seconds: float = LEASE_RENEW_INTERVAL_SECONDS,
) -> None:
    raw = str(fields.get("payload") or "")
    try:
        job = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        logger.error("invalid_job_payload", worker=worker_label, msg_id=msg_id, err=str(exc))
        await client.xack(JOBS_STREAM_KEY, JOBS_GROUP_NAME, msg_id)
        await client.xdel(JOBS_STREAM_KEY, msg_id)
        return

    logger.info(
        "job_started",
        worker=worker_label,
        msg_id=msg_id,
        job_id=job.get("id"),
        type=job.get("type"),
    )

    async def _renew_lease_loop() -> None:
        while True:
            await asyncio.sleep(max(0.01, lease_renew_seconds))
            if hasattr(client, "xclaim"):
                with contextlib.suppress(Exception):
                    await client.xclaim(
                        JOBS_STREAM_KEY,
                        JOBS_GROUP_NAME,
                        worker_label,
                        min_idle_time=0,
                        message_ids=[msg_id],
                        justid=True,
                    )

    renew_task = asyncio.create_task(_renew_lease_loop())
    try:
        await dispatch_job(job)
        logger.info(
            "job_finished",
            worker=worker_label,
            msg_id=msg_id,
            job_id=job.get("id"),
            type=job.get("type"),
        )
    except asyncio.CancelledError:
        # Leave unacked so another worker / restart can XAUTOCLAIM it
        raise
    except Exception as exc:
        logger.exception(
            "job_failed",
            worker=worker_label,
            msg_id=msg_id,
            job_id=job.get("id"),
            type=job.get("type"),
            err=str(exc),
        )
    finally:
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await renew_task

    await client.xack(JOBS_STREAM_KEY, JOBS_GROUP_NAME, msg_id)
    await client.xdel(JOBS_STREAM_KEY, msg_id)


async def _reclaim_once(
    client: aioredis.Redis,
    consumer_name: str,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Evict poison-pill jobs exceeding MAX_STREAM_DELIVERIES to DLQ, then XAUTOCLAIM idle jobs."""
    evicted = 0
    if hasattr(client, "xpending_range"):
        pending_rows = await client.xpending_range(
            JOBS_STREAM_KEY,
            JOBS_GROUP_NAME,
            min="-",
            max="+",
            count=50,
            idle=RECLAIM_MIN_IDLE_MS,
        )
        for p in pending_rows or []:
            if not isinstance(p, dict):
                continue
            mid = str(p.get("message_id") or "")
            times = int(p.get("times_delivered") or 0)
            if mid and times >= MAX_STREAM_DELIVERIES:
                dlq_fields: dict[Any, Any] = {
                    "failed_msg_id": mid,
                    "times_delivered": str(times),
                }
                if hasattr(client, "xrange"):
                    with contextlib.suppress(Exception):
                        entries = await client.xrange(JOBS_STREAM_KEY, min=mid, max=mid, count=1)
                        if entries and len(entries[0]) >= 2 and isinstance(entries[0][1], dict):
                            for k, v in entries[0][1].items():
                                dlq_fields[str(k)] = str(v)
                await client.xadd(
                    DEAD_LETTER_STREAM_KEY,
                    dlq_fields,
                    maxlen=1000,
                    approximate=True,
                )
                await _mark_evicted_job_failed_in_db(str(dlq_fields.get("payload") or ""), times)
                await client.xack(JOBS_STREAM_KEY, JOBS_GROUP_NAME, mid)
                await client.xdel(JOBS_STREAM_KEY, mid)
                evicted += 1
                logger.error(
                    "job_moved_to_dlq",
                    msg_id=mid,
                    times_delivered=times,
                )

    claimed = await client.xautoclaim(
        JOBS_STREAM_KEY,
        JOBS_GROUP_NAME,
        consumer_name,
        min_idle_time=RECLAIM_MIN_IDLE_MS,
        start_id="0-0",
        count=10,
    )
    messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) >= 2 else []
    for msg_id, fields in messages:
        if stop_event is not None and stop_event.is_set():
            break
        logger.warning("job_reclaimed", msg_id=msg_id, consumer=consumer_name)
        await _process_stream_entry(
            client, str(msg_id), dict(fields), worker_label=consumer_name
        )
    return evicted


async def _heartbeat_loop(stop_event: asyncio.Event) -> None:
    cfg = get_settings()
    client = aioredis.from_url(cfg.redis_url, decode_responses=True)
    try:
        while not stop_event.is_set():
            try:
                await publish_worker_heartbeat(client)
            except Exception as exc:
                logger.warning("worker_heartbeat_error", err=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
    finally:
        await client.aclose()


async def _reclaim_loop(stop_event: asyncio.Event, consumer_name: str) -> None:
    cfg = get_settings()
    client = aioredis.from_url(cfg.redis_url, decode_responses=True)
    try:
        await ensure_consumer_group(client)
        while not stop_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=RECLAIM_INTERVAL_SECONDS)
            if stop_event.is_set():
                break
            try:
                await _reclaim_once(client, consumer_name, stop_event)
            except ResponseError as exc:
                if "NOGROUP" in str(exc):
                    await ensure_consumer_group(client)
                else:
                    logger.warning("worker_reclaim_error", err=str(exc))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("worker_reclaim_error", err=str(exc))
    finally:
        await client.aclose()


async def _worker_loop(worker_idx: int, stop_event: asyncio.Event) -> None:
    cfg = get_settings()
    consumer_name = f"worker-{os.getpid()}-{worker_idx}"
    client = aioredis.from_url(cfg.redis_url, decode_responses=True)
    logger.info("worker_started", worker_idx=worker_idx, consumer=consumer_name)
    try:
        await ensure_consumer_group(client)
        while not stop_event.is_set():
            try:
                resp = await client.xreadgroup(
                    JOBS_GROUP_NAME,
                    consumer_name,
                    {JOBS_STREAM_KEY: ">"},
                    count=1,
                    block=5000,
                )
                if not resp:
                    continue
                for _stream_name, entries in resp:
                    for msg_id, fields in entries:
                        await _process_stream_entry(
                            client,
                            str(msg_id),
                            dict(fields),
                            worker_label=consumer_name,
                        )
            except asyncio.CancelledError:
                break
            except ResponseError as exc:
                if "NOGROUP" in str(exc):
                    await ensure_consumer_group(client)
                else:
                    logger.warning("worker_stream_error", worker_idx=worker_idx, err=str(exc))
                    await asyncio.sleep(1)
            except Exception as exc:
                logger.exception("worker_loop_error", worker_idx=worker_idx, err=str(exc))
                await asyncio.sleep(1)
    finally:
        await client.aclose()


async def main(concurrency: int = 2) -> None:
    configure_logging()
    get_settings().ensure_storage_dirs()
    try:
        await recover_stale_running_deliveries()
    except Exception as exc:
        logger.warning("stale_delivery_recovery_failed", err=str(exc))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    reclaim_consumer = f"reclaimer-{os.getpid()}"
    tasks = [
        asyncio.create_task(_heartbeat_loop(stop_event)),
        asyncio.create_task(_reclaim_loop(stop_event, reclaim_consumer)),
        *[asyncio.create_task(_worker_loop(i, stop_event)) for i in range(concurrency)],
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
