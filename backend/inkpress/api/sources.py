from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.api.deps import get_current_user
from inkpress.api.schemas import SourceCreate, SourceUpdate
from inkpress.db.base import get_db
from inkpress.db.models import Article, Source, User
from inkpress.ingestion.newsnow_catalog import CURATED_RSS_PRESETS, NEWSNOW_CATALOG
from inkpress.ingestion.newsnow_client import fetch_newsnow_source
from inkpress.ingestion.rss_client import fetch_rss_entries
from inkpress.settings_store import get_network_config
from inkpress.worker.tasks import run_sync_source, run_sync_source_bg, sync_source_task

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


def _source_dict(src: Source, article_count: int = 0) -> dict[str, Any]:
    return {
        "id": src.id,
        "name": src.name,
        "kind": src.kind,
        "identifier": src.identifier,
        "category": src.category,
        "fetch_strategy": src.fetch_strategy,
        "use_proxy": src.use_proxy,
        "ai_options": src.ai_options or {},
        "max_items": src.max_items,
        "enabled": src.enabled,
        "last_fetched_at": src.last_fetched_at.isoformat() if src.last_fetched_at else None,
        "last_error": src.last_error,
        "consecutive_failures": src.consecutive_failures,
        "article_count": article_count,
        "created_at": src.created_at.isoformat(),
    }


@router.get("/catalog/newsnow")
async def get_newsnow_catalog(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    subscribed = set(
        (await session.execute(select(Source.identifier).where(Source.kind == "newsnow")))
        .scalars()
        .all()
    )
    items = [
        {
            "id": item.id,
            "name": item.name,
            "category": item.category,
            "feed_type": item.feed_type,
            "default_strategy": item.default_strategy,
            "language": item.language,
            "description": item.description,
            "subscribed": item.id in subscribed,
        }
        for item in NEWSNOW_CATALOG
    ]
    return {"items": items, "presets": list(CURATED_RSS_PRESETS)}


@router.get("/catalog/newsnow/{source_id}/preview")
async def preview_newsnow(
    source_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    net = await get_network_config(session)
    try:
        entries = await fetch_newsnow_source(source_id, net["newsnow_base_url"])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"NewsNow sidecar error for '{source_id}': {exc}",
        ) from exc
    return {
        "source_id": source_id,
        "items": [
            {
                "title": e.title,
                "url": e.url,
                "pub_date": e.pub_date.isoformat(),
                "summary_hint": e.summary_hint,
                "info_tag": e.info_tag,
            }
            for e in entries[:20]
        ],
    }


@router.get("/preview-rss")
async def preview_rss(
    url: str,
    use_proxy: str = "auto",
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    net = await get_network_config(session)
    try:
        feed_title, entries = await fetch_rss_entries(
            url,
            rsshub_base_url=net["rsshub_base_url"],
            use_proxy=use_proxy,
            outbound_proxy=net["outbound_proxy"],
            byparr_base_url=net["byparr_base_url"],
            max_items=8,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch feed: {exc}") from exc
    return {
        "feed_title": feed_title,
        "recommended_strategy": (
            "fulltext_rss"
            if entries and sum(1 for e in entries if e.is_fulltext_candidate) >= len(entries) / 2
            else "extract_web"
        ),
        "items": [
            {
                "title": e.title,
                "url": e.url,
                "published_at": e.published_at.isoformat(),
                "is_fulltext_candidate": e.is_fulltext_candidate,
            }
            for e in entries
        ],
    }


@router.get("")
async def list_sources(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sources = (await session.execute(select(Source).order_by(desc(Source.id)))).scalars().all()
    counts_rows = (
        await session.execute(
            select(Article.source_id, func.count(Article.id)).group_by(Article.source_id)
        )
    ).all()
    counts = {sid: int(cnt) for sid, cnt in counts_rows if sid is not None}
    return {"items": [_source_dict(s, counts.get(s.id, 0)) for s in sources]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_source(
    payload: SourceCreate,
    background_tasks: BackgroundTasks,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    dup = (
        await session.execute(
            select(Source).where(
                Source.kind == payload.kind, Source.identifier == payload.identifier.strip()
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="该订阅源已存在")

    src = Source(
        name=payload.name.strip(),
        kind=payload.kind,
        identifier=payload.identifier.strip(),
        category=payload.category.strip() or "综合",
        fetch_strategy=payload.fetch_strategy,
        use_proxy=payload.use_proxy,
        ai_options=payload.ai_options,
        max_items=payload.max_items,
        enabled=payload.enabled,
    )
    session.add(src)
    await session.commit()
    await session.refresh(src)

    try:
        await sync_source_task.kiq(src.id)
    except Exception:
        background_tasks.add_task(run_sync_source_bg, src.id)

    return {"item": _source_dict(src, 0)}


@router.patch("/{source_id}")
async def update_source(
    source_id: int,
    payload: SourceUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    src = await session.get(Source, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Source not found")
    for field in (
        "name",
        "category",
        "fetch_strategy",
        "use_proxy",
        "ai_options",
        "max_items",
        "enabled",
    ):
        val = getattr(payload, field)
        if val is not None:
            setattr(src, field, val)
    await session.commit()
    await session.refresh(src)
    return {"item": _source_dict(src)}


@router.delete("/{source_id}")
async def delete_source(
    source_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    src = await session.get(Source, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Source not found")
    await session.delete(src)
    await session.commit()
    return {"status": "deleted"}


@router.post("/{source_id}/sync")
async def sync_source_now(
    source_id: int,
    background_tasks: BackgroundTasks,
    inline: bool = False,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    src = await session.get(Source, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if inline:
        stats = await run_sync_source(src.id)
        return {"status": "completed", "stats": stats}
    try:
        await sync_source_task.kiq(src.id)
        return {"status": "queued", "source_id": src.id}
    except Exception:
        background_tasks.add_task(run_sync_source, src.id)
        return {"status": "queued_local", "source_id": src.id}


@router.post("/import-opml")
async def import_opml(
    file: UploadFile = File(...),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    raw = await file.read()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid OPML XML: {exc}") from exc

    added = 0
    skipped = 0
    for outline in root.findall(".//outline"):
        xml_url = (outline.attrib.get("xmlUrl") or "").strip()
        if not xml_url:
            continue
        title = (outline.attrib.get("title") or outline.attrib.get("text") or xml_url).strip()
        exists = (
            await session.execute(
                select(Source).where(Source.kind == "rss", Source.identifier == xml_url)
            )
        ).scalar_one_or_none()
        if exists:
            skipped += 1
            continue
        session.add(
            Source(
                name=title[:160],
                kind="rss",
                identifier=xml_url[:1024],
                category="导入",
                fetch_strategy="extract_web",
                use_proxy="auto",
                max_items=15,
            )
        )
        added += 1
    await session.commit()
    return {"added": added, "skipped": skipped}


@router.get("/export-opml")
async def export_opml(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Response:
    sources = (
        (await session.execute(select(Source).where(Source.kind.in_(["rss", "rsshub"]))))
        .scalars()
        .all()
    )
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "InkPress Subscriptions"
    body = ET.SubElement(opml, "body")
    for src in sources:
        ET.SubElement(
            body,
            "outline",
            type="rss",
            text=src.name,
            title=src.name,
            xmlUrl=src.identifier,
            category=src.category,
        )
    xml_bytes = ET.tostring(opml, encoding="utf-8", xml_declaration=True)
    return Response(
        content=xml_bytes,
        media_type="text/xml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="inkpress-subscriptions.opml"'},
    )
