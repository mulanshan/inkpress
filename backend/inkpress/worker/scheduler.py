from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select

from inkpress.db.base import get_session_factory
from inkpress.db.models import Book, Edition, Source, utcnow
from inkpress.logging import configure_logging, get_logger
from inkpress.worker.tasks import (
    build_and_deliver_edition_task,
    deliver_book_task,
    sync_source_task,
)

logger = get_logger(__name__)


async def _tick_once() -> None:
    factory = get_session_factory()
    now_utc = utcnow()

    async with factory() as session:
        # 1. Check enabled editions against their cron expressions
        editions = (
            (await session.execute(select(Edition).where(Edition.enabled.is_(True))))
            .scalars()
            .all()
        )
        for ed in editions:
            try:
                tz = ZoneInfo(ed.timezone)
            except Exception:
                tz = ZoneInfo("Asia/Shanghai")
            now_local = now_utc.astimezone(tz)
            base_local = (
                ed.last_run_at.astimezone(tz)
                if ed.last_run_at is not None
                else (now_local - timedelta(minutes=2))
            )
            try:
                next_fire = croniter(ed.cron_expr, base_local).get_next(datetime)
            except Exception as exc:
                logger.warning(
                    "invalid_cron_expr", edition_id=ed.id, cron=ed.cron_expr, err=str(exc)
                )
                continue

            if next_fire <= now_local and (
                ed.last_run_at is None or (now_utc - ed.last_run_at) >= timedelta(minutes=5)
            ):
                ed.last_run_at = now_utc
                await session.commit()
                logger.info("scheduler_dispatch_edition", edition_id=ed.id, name=ed.name)
                await build_and_deliver_edition_task.kiq(ed.id, trigger="cron", send_email=True)

        # 2. Check scheduled books waiting for push
        due_books = (
            (
                await session.execute(
                    select(Book).where(
                        Book.scheduled_push_at.is_not(None),
                        Book.scheduled_push_at <= now_utc,
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
        stale_cutoff = now_utc - timedelta(hours=3)
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
            src.last_fetched_at = now_utc
            await session.commit()
            await sync_source_task.kiq(src.id)


async def run_scheduler_loop() -> None:
    configure_logging()
    logger.info("inkpress_scheduler_started")
    while True:
        try:
            await _tick_once()
        except Exception as exc:
            logger.exception("scheduler_tick_error", err=str(exc))
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(run_scheduler_loop())
