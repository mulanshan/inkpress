from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

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
from lxml import etree as lxml_etree
from sqlalchemy import desc, func, select, update
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

MAX_OPML_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


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
        entries = await fetch_newsnow_source(source_id, net["newsnow_base_url"], latest=True)
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

    new_kind = payload.kind if payload.kind is not None else src.kind
    new_identifier = (
        payload.identifier.strip() if payload.identifier is not None else src.identifier
    )
    if (new_kind != src.kind) or (new_identifier != src.identifier):
        dup = (
            await session.execute(
                select(Source).where(
                    Source.kind == new_kind,
                    Source.identifier == new_identifier,
                    Source.id != src.id,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status_code=409, detail="相同类型与标识的订阅源已存在")
        src.kind = new_kind
        src.identifier = new_identifier

    if payload.name is not None:
        src.name = payload.name.strip() or src.name
    if payload.category is not None:
        src.category = payload.category.strip() or "综合"
    for field in (
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

    article_count = int(
        (
            await session.execute(
                select(func.count(Article.id)).where(Article.source_id == src.id)
            )
        ).scalar_one()
    )
    return {"item": _source_dict(src, article_count)}


@router.delete("/{source_id}")
async def delete_source(
    source_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    src = await session.get(Source, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Source not found")
    # Explicitly detach existing articles so starred & archived articles survive on both PG and SQLite
    await session.execute(
        update(Article).where(Article.source_id == src.id).values(source_id=None)
    )
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


def _iter_opml_outlines(
    node: lxml_etree._Element,
    parent_category: str = "",
) -> list[tuple[str, str, str]]:
    """Recursively walk OPML <outline> elements, returning (xml_url, title, category)."""
    results: list[tuple[str, str, str]] = []
    for child in node:
        if not isinstance(child.tag, str) or child.tag.lower() != "outline":
            results.extend(_iter_opml_outlines(child, parent_category))
            continue
        xml_url = str(child.attrib.get("xmlUrl") or child.attrib.get("xmlurl") or "").strip()
        own_cat = str(child.attrib.get("category") or "").strip()
        label = str(child.attrib.get("title") or child.attrib.get("text") or "").strip()
        if xml_url:
            category = own_cat or parent_category or "导入"
            results.append((xml_url, label or xml_url, category))
        else:
            folder_cat = label or own_cat or parent_category
            results.extend(_iter_opml_outlines(child, folder_cat))
    return results


@router.post("/import-opml")
async def import_opml(
    file: UploadFile = File(...),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    raw = await file.read(MAX_OPML_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="上传的 OPML 文件为空")
    if len(raw) > MAX_OPML_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="OPML 文件大小不能超过 5 MB")

    try:
        parser = lxml_etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            huge_tree=False,
            recover=False,
        )
        root = lxml_etree.fromstring(raw, parser=parser)
    except lxml_etree.XMLSyntaxError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid OPML XML: {exc}") from exc

    body = root.find("body")
    entries = _iter_opml_outlines(body if body is not None else root)

    existing_keys = set(
        (await session.execute(select(Source.kind, Source.identifier))).all()
    )
    seen_in_batch: set[tuple[str, str]] = set()

    added = 0
    skipped = 0
    for xml_url, title, category in entries:
        if xml_url.lower().startswith("rsshub://"):
            kind = "rsshub"
        else:
            scheme = urlparse(xml_url).scheme.lower()
            if scheme not in ("http", "https"):
                skipped += 1
                continue
            kind = "rss"

        identifier = xml_url[:1024]
        key = (kind, identifier)
        if key in existing_keys or key in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(key)

        session.add(
            Source(
                name=title[:160],
                kind=kind,
                identifier=identifier,
                category=(category or "导入")[:64],
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
