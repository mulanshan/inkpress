from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import delete, exists, func, select

from inkpress.db.base import get_session_factory
from inkpress.db.models import Article, ArticleDelivery, Book, Delivery, Edition, Source, utcnow
from inkpress.logging import configure_logging, get_logger
from inkpress.worker.tasks import (
    build_and_deliver_edition_task,
    deliver_book_task,
    sync_source_task,
)

logger = get_logger(__name__)

MISFIRE_GRACE_MINUTES = 15
MANUAL_COLLISION_WINDOW_MINUTES = 30
RETENTION_GC_INTERVAL_HOURS = 24

_last_gc_at: datetime | None = None


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_edition_due(
    edition: Edition,
    now_utc: datetime,
    *,
    has_recent_delivery: bool = False,
    grace_minutes: int = MISFIRE_GRACE_MINUTES,
) -> bool:
    """Return True iff edition's most recent cron fire falls within the misfire grace window,
    has not already run for that slot, and no recent manual/cron delivery ran within 30 min."""
    if not edition.enabled or has_recent_delivery:
        return False

    try:
        tz = ZoneInfo(edition.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")

    now_local = _as_utc(now_utc).astimezone(tz)
    try:
        prev_fire = croniter(edition.cron_expr, now_local + timedelta(seconds=1)).get_prev(datetime)
    except Exception as exc:
        logger.warning(
            "invalid_cron_expr",
            edition_id=edition.id,
            cron=edition.cron_expr,
            err=str(exc),
        )
        return False

    lag = now_local - prev_fire
    if lag < timedelta(0) or lag > timedelta(minutes=grace_minutes):
        return False

    prev_fire_utc = prev_fire.astimezone(UTC)
    if edition.last_run_at is not None:
        last_run_utc = _as_utc(edition.last_run_at)
        if last_run_utc >= prev_fire_utc:
            return False
        if (_as_utc(now_utc) - last_run_utc) < timedelta(minutes=5):
            return False

    return True


async def run_retention_gc(now_utc: datetime | None = None) -> int:
    """Delete unstarred, undelivered articles older than the max edition retention_days."""
    now = _as_utc(now_utc or utcnow())
    factory = get_session_factory()
    async with factory() as session:
        max_ret = (await session.execute(select(func.max(Edition.retention_days)))).scalar_one()
        retention_days = max(1, int(max_ret or 30))
        cutoff = now - timedelta(days=retention_days)

        delivered_exists = exists(
            select(ArticleDelivery.id).where(ArticleDelivery.article_id == Article.id)
        )
        stmt = delete(Article).where(
            Article.is_starred.is_(False),
            Article.fetched_at < cutoff,
            ~delivered_exists,
        )
        result = await session.execute(stmt)
        await session.commit()
        deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]
        if deleted > 0:
            logger.info(
                "retention_gc_deleted_articles",
                deleted=deleted,
                retention_days=retention_days,
            )
        return deleted


async def _tick_once(now_utc: datetime | None = None) -> None:
    global _last_gc_at
    factory = get_session_factory()
    now = _as_utc(now_utc or utcnow())

    async with factory() as session:
        # 1. Check enabled editions against their cron expressions with 15-min grace window
        editions = (
            (await session.execute(select(Edition).where(Edition.enabled.is_(True))))
            .scalars()
            .all()
        )
        collision_cutoff = now - timedelta(minutes=MANUAL_COLLISION_WINDOW_MINUTES)
        for ed in editions:
            recent_deliv_count = int(
                (
                    await session.execute(
                        select(func.count(Delivery.id)).where(
                            Delivery.edition_id == ed.id,
                            Delivery.status.in_(("running", "sent")),
                            Delivery.created_at >= collision_cutoff,
                        )
                    )
                ).scalar_one()
            )
            if is_edition_due(ed, now, has_recent_delivery=recent_deliv_count > 0):
                ed.last_run_at = now
                await session.commit()
                logger.info("scheduler_dispatch_edition", edition_id=ed.id, name=ed.name)
                await build_and_deliver_edition_task.kiq(ed.id, trigger="cron", send_email=True)

        # 2. Check scheduled books waiting for push
        due_books = (
            (
                await session.execute(
                    select(Book).where(
                        Book.scheduled_push_at.is_not(None),
                        Book.scheduled_push_at <= now,
                        Book.conversion_status == "ready",
                    )
                )
            )
            .scalars()
            .all()
        )
        for book in due_books:
            book.scheduled_push_at = None
            await session.commit()
            logger.info("scheduler_dispatch_book", book_id=book.id, title=book.title)
            await deliver_book_task.kiq(book.id, trigger="scheduled")

        # 3. Periodic background refresh of stale sources (every 3 hours) so Today page stays fresh
        stale_cutoff = now - timedelta(hours=3)
        stale_sources = (
            (
                await session.execute(
                    select(Source).where(
                        Source.enabled.is_(True),
                        (Source.last_fetched_at.is_(None))
                        | (Source.last_fetched_at < stale_cutoff),
                    )
                )
            )
            .scalars()
            .all()
        )
        for src in stale_sources[:4]:
            src.last_fetched_at = now
            await session.commit()
            await sync_source_task.kiq(src.id)

    # 4. Daily retention GC (unstarred, undelivered old articles beyond max edition retention)
    if _last_gc_at is None or (now - _last_gc_at) >= timedelta(hours=RETENTION_GC_INTERVAL_HOURS):
        try:
            await run_retention_gc(now)
            _last_gc_at = now
        except Exception as exc:
            logger.warning("retention_gc_error", err=str(exc))


async def run_scheduler_loop() -> None:
    configure_logging()
    logger.info("inkpress_scheduler_started")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    while not stop_event.is_set():
        try:
            await _tick_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("scheduler_tick_error", err=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=30.0)
    logger.info("inkpress_scheduler_stopped")


if __name__ == "__main__":
    asyncio.run(run_scheduler_loop())
