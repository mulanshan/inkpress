from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from inkpress.api.deps import get_current_user
from inkpress.api.schemas import EditionCreate, EditionUpdate
from inkpress.db.base import get_db
from inkpress.db.models import Edition, EditionSource, User
from inkpress.worker.tasks import (
    build_and_deliver_edition_task,
    run_build_and_deliver_edition,
    run_build_and_deliver_edition_bg,
)

router = APIRouter(prefix="/api/v1/editions", tags=["editions"])


def _next_run_iso(cron_expr: str, tz_name: str) -> str | None:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    try:
        nxt = croniter(cron_expr, datetime.now(tz)).get_next(datetime)
        return nxt.isoformat()
    except Exception:
        return None


def _edition_dict(ed: Edition) -> dict[str, Any]:
    return {
        "id": ed.id,
        "name": ed.name,
        "subtitle": ed.subtitle,
        "cover_style": ed.cover_style,
        "cron_expr": ed.cron_expr,
        "timezone": ed.timezone,
        "lookback_hours": ed.lookback_hours,
        "dedup_window_days": ed.dedup_window_days,
        "max_articles": ed.max_articles,
        "max_mb_per_volume": ed.max_mb_per_volume,
        "grayscale_images": ed.grayscale_images,
        "include_qr_links": ed.include_qr_links,
        "ai_briefing_enabled": ed.ai_briefing_enabled,
        "target_emails": ed.target_emails,
        "issue_counter": ed.issue_counter,
        "enabled": ed.enabled,
        "last_run_at": ed.last_run_at.isoformat() if ed.last_run_at else None,
        "next_run_at": _next_run_iso(ed.cron_expr, ed.timezone) if ed.enabled else None,
        "sources": [
            {
                "source_id": link.source_id,
                "sort_order": link.sort_order,
                "max_items_override": link.max_items_override,
                "source_name": link.source.name if link.source else "",
                "source_kind": link.source.kind if link.source else "",
                "source_category": link.source.category if link.source else "",
            }
            for link in ed.source_links
        ],
    }


@router.get("")
async def list_editions(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(Edition)
                .options(selectinload(Edition.source_links).selectinload(EditionSource.source))
                .order_by(desc(Edition.id))
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_edition_dict(e) for e in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_edition(
    payload: EditionCreate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not croniter.is_valid(payload.cron_expr):
        raise HTTPException(status_code=400, detail=f"无效的 Cron 表达式: {payload.cron_expr}")

    ed = Edition(
        name=payload.name.strip(),
        subtitle=payload.subtitle.strip(),
        cover_style=payload.cover_style,
        cron_expr=payload.cron_expr.strip(),
        timezone=payload.timezone.strip(),
        lookback_hours=payload.lookback_hours,
        dedup_window_days=payload.dedup_window_days,
        max_articles=payload.max_articles,
        max_mb_per_volume=payload.max_mb_per_volume,
        grayscale_images=payload.grayscale_images,
        include_qr_links=payload.include_qr_links,
        ai_briefing_enabled=payload.ai_briefing_enabled,
        target_emails=payload.target_emails.strip(),
        enabled=payload.enabled,
    )
    session.add(ed)
    await session.flush()

    for idx, link in enumerate(payload.sources):
        session.add(
            EditionSource(
                edition_id=ed.id,
                source_id=link.source_id,
                sort_order=link.sort_order or idx,
                max_items_override=link.max_items_override,
            )
        )
    await session.commit()

    loaded = (
        await session.execute(
            select(Edition)
            .options(selectinload(Edition.source_links).selectinload(EditionSource.source))
            .where(Edition.id == ed.id)
        )
    ).scalar_one()
    return {"item": _edition_dict(loaded)}


@router.patch("/{edition_id}")
async def update_edition(
    edition_id: int,
    payload: EditionUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ed = (
        await session.execute(
            select(Edition)
            .options(selectinload(Edition.source_links).selectinload(EditionSource.source))
            .where(Edition.id == edition_id)
        )
    ).scalar_one_or_none()
    if ed is None:
        raise HTTPException(status_code=404, detail="Edition not found")

    if payload.cron_expr is not None and not croniter.is_valid(payload.cron_expr):
        raise HTTPException(status_code=400, detail=f"无效的 Cron 表达式: {payload.cron_expr}")

    for field in (
        "name",
        "subtitle",
        "cover_style",
        "cron_expr",
        "timezone",
        "lookback_hours",
        "dedup_window_days",
        "max_articles",
        "max_mb_per_volume",
        "grayscale_images",
        "include_qr_links",
        "ai_briefing_enabled",
        "target_emails",
        "enabled",
    ):
        val = getattr(payload, field)
        if val is not None:
            setattr(ed, field, val)

    if payload.sources is not None:
        ed.source_links.clear()
        await session.flush()
        for idx, link in enumerate(payload.sources):
            ed.source_links.append(
                EditionSource(
                    edition_id=ed.id,
                    source_id=link.source_id,
                    sort_order=link.sort_order or idx,
                    max_items_override=link.max_items_override,
                )
            )
    await session.commit()

    loaded = (
        await session.execute(
            select(Edition)
            .options(selectinload(Edition.source_links).selectinload(EditionSource.source))
            .where(Edition.id == ed.id)
        )
    ).scalar_one()
    return {"item": _edition_dict(loaded)}


@router.delete("/{edition_id}")
async def delete_edition(
    edition_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    ed = await session.get(Edition, edition_id)
    if ed is None:
        raise HTTPException(status_code=404, detail="Edition not found")
    await session.delete(ed)
    await session.commit()
    return {"status": "deleted"}


@router.post("/{edition_id}/deliver")
async def deliver_edition_now(
    edition_id: int,
    background_tasks: BackgroundTasks,
    send_email: bool = True,
    inline: bool = False,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ed = await session.get(Edition, edition_id)
    if ed is None:
        raise HTTPException(status_code=404, detail="Edition not found")
    if inline:
        res = await run_build_and_deliver_edition(ed.id, trigger="manual", send_email=send_email)
        return {"status": "completed", "result": res}
    try:
        await build_and_deliver_edition_task.kiq(ed.id, trigger="manual", send_email=send_email)
        return {"status": "queued", "edition_id": ed.id}
    except Exception:
        background_tasks.add_task(
            run_build_and_deliver_edition_bg, ed.id, trigger="manual", send_email=send_email
        )
        return {"status": "queued_local", "edition_id": ed.id}
