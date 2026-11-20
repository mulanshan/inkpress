from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from croniter import croniter
from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress import __version__
from inkpress.api.articles import _push_single_article_to_kindle
from inkpress.api.health import collect_deep_health
from inkpress.db.base import get_db
from inkpress.db.models import Article, Book, Delivery, Edition, McpToken, Source, utcnow
from inkpress.db.search import search_articles_fulltext
from inkpress.ingestion.extractor import (
    canonicalize_url,
    compute_content_hash,
    extract_from_webpage,
)
from inkpress.ingestion.fetcher import assert_safe_public_url, smart_fetch_text
from inkpress.ingestion.newsnow_catalog import NEWSNOW_CATALOG
from inkpress.publishing.epub_validator import validate_epub
from inkpress.security import hash_token
from inkpress.settings_store import get_network_config
from inkpress.worker.tasks import run_build_and_deliver_edition, run_deliver_book, run_sync_source

router = APIRouter(tags=["mcp"])

MCP_PROTOCOL_VERSION = "2025-06-18"
LAST_USED_THROTTLE_SECONDS = 300.0
LOCALHOST_ORIGINS = frozenset({"localhost", "127.0.0.1", "::1"})

TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "get_system_status",
        "description": "Inspect InkPress service status, counts of sources, articles, editions, books, and recent failures.",
        "readonly": True,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_health",
        "description": "Run deep health diagnostics across database, Redis, worker heartbeat, queue depth, sidecars, disk space, and conversion tools.",
        "readonly": True,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_newsnow_catalog",
        "description": "List all built-in NewsNow real-time and hottest source IDs supported by the local NewsNow sidecar.",
        "readonly": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Optional Chinese category filter"}
            },
        },
    },
    {
        "name": "list_sources",
        "description": "List configured content sources with last sync status and failure counters.",
        "readonly": True,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_source",
        "description": "Add a new NewsNow, RSS, RSSHub (rsshub://...), webpage, or webpage_list source.",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["name", "kind", "identifier"],
            "properties": {
                "name": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["newsnow", "rss", "rsshub", "webpage", "webpage_list"],
                },
                "identifier": {"type": "string"},
                "category": {"type": "string"},
                "fetch_strategy": {
                    "type": "string",
                    "enum": ["fulltext_rss", "extract_web", "newsnow_digest", "byparr_browser"],
                },
                "use_proxy": {"type": "string", "enum": ["auto", "direct", "proxy"]},
                "ai_options": {"type": "object"},
                "max_items": {"type": "integer"},
            },
        },
    },
    {
        "name": "update_source",
        "description": "Update an existing source's configuration, URL/identifier, kind, regex filters, CSS selectors, or enabled state.",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["source_id"],
            "properties": {
                "source_id": {"type": "integer"},
                "name": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["newsnow", "rss", "rsshub", "webpage", "webpage_list"],
                },
                "identifier": {"type": "string"},
                "category": {"type": "string"},
                "fetch_strategy": {
                    "type": "string",
                    "enum": ["fulltext_rss", "extract_web", "newsnow_digest", "byparr_browser"],
                },
                "use_proxy": {"type": "string", "enum": ["auto", "direct", "proxy"]},
                "ai_options": {"type": "object"},
                "max_items": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
        },
    },
    {
        "name": "sync_source_now",
        "description": "Synchronize a source immediately and return ingestion counts.",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["source_id"],
            "properties": {"source_id": {"type": "integer"}},
        },
    },
    {
        "name": "search_articles",
        "description": "Search archived articles via ParadeDB BM25 / full-text search.",
        "readonly": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "unread_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 15},
            },
        },
    },
    {
        "name": "read_article",
        "description": "Read the clean plain text and AI summary of an article by ID.",
        "readonly": True,
        "inputSchema": {
            "type": "object",
            "required": ["article_id"],
            "properties": {"article_id": {"type": "integer"}},
        },
    },
    {
        "name": "clip_url",
        "description": "Fetch, extract, and save a public web article into InkPress (optionally pushing to Kindle).",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string"},
                "title": {"type": "string"},
                "use_proxy": {
                    "type": "string",
                    "enum": ["auto", "direct", "proxy"],
                    "default": "auto",
                },
                "push_to_kindle": {"type": "boolean", "default": False},
                "target_email": {"type": "string"},
            },
        },
    },
    {
        "name": "list_editions",
        "description": "List scheduled Kindle editions and their cron schedules.",
        "readonly": True,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_edition",
        "description": "Update a Kindle edition's schedule, enabled flag, strip_links, retention_days, or metadata.",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["edition_id"],
            "properties": {
                "edition_id": {"type": "integer"},
                "name": {"type": "string"},
                "subtitle": {"type": "string"},
                "cron_expr": {"type": "string"},
                "timezone": {"type": "string"},
                "cover_style": {"type": "string", "enum": ["editorial", "dark", "minimal"]},
                "lookback_hours": {"type": "integer"},
                "dedup_window_days": {"type": "integer"},
                "max_articles": {"type": "integer"},
                "strip_links": {"type": "boolean"},
                "retention_days": {"type": "integer"},
                "ai_briefing_enabled": {"type": "boolean"},
                "target_emails": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
        },
    },
    {
        "name": "trigger_edition_now",
        "description": "Compile and deliver an edition immediately to Kindle (or build-only if send_email=false).",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["edition_id"],
            "properties": {
                "edition_id": {"type": "integer"},
                "send_email": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "list_books",
        "description": "List books and archived magazine issues in the personal library.",
        "readonly": True,
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "deliver_book_now",
        "description": "Send a library book or edition issue to Kindle immediately.",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["book_id"],
            "properties": {
                "book_id": {"type": "integer"},
                "target_email": {"type": "string"},
            },
        },
    },
    {
        "name": "list_deliveries",
        "description": "Inspect recent Kindle delivery telemetry, stage timings, sizes, and errors.",
        "readonly": True,
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "retry_delivery",
        "description": "Retry a failed delivery (reusing the built EPUB book when available).",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["delivery_id"],
            "properties": {"delivery_id": {"type": "integer"}},
        },
    },
    {
        "name": "test_extract_url",
        "description": "Diagnostic AI-Ops tool: fetch and extract a public URL without saving, reporting word count and preview.",
        "readonly": True,
        "inputSchema": {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string"},
                "use_proxy": {
                    "type": "string",
                    "enum": ["auto", "direct", "proxy"],
                    "default": "auto",
                },
            },
        },
    },
    {
        "name": "validate_book_epub",
        "description": "Run the strict Amazon Send-to-Kindle E999 pre-flight validator on a book in the library.",
        "readonly": True,
        "inputSchema": {
            "type": "object",
            "required": ["book_id"],
            "properties": {"book_id": {"type": "integer"}},
        },
    },
]

TOOL_META = {t["name"]: t for t in TOOLS_SPEC}


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_allowed_mcp_origin(request: Request) -> bool:
    """Allow requests without Origin (CLI/MCP clients) and localhost/same-host origins;
    reject arbitrary cross-origin browser requests to prevent DNS rebinding/CSRF."""
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return True
    parsed = urlparse(origin)
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    origin_host = (parsed.hostname or "").lower()
    if not origin_host:
        return False
    if origin_host in LOCALHOST_ORIGINS:
        return True
    req_host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    return bool(req_host and origin_host == req_host)


async def _authenticate_mcp(session: AsyncSession, authorization: str | None) -> McpToken | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    raw = authorization.split(" ", 1)[1].strip()
    if not raw:
        return None
    digest = hash_token(raw)
    tok = (
        await session.execute(
            select(McpToken).where(McpToken.token_hash == digest, McpToken.enabled.is_(True))
        )
    ).scalar_one_or_none()
    if tok is not None:
        now = utcnow()
        if (
            tok.last_used_at is None
            or (now - _as_utc(tok.last_used_at)) >= timedelta(seconds=LAST_USED_THROTTLE_SECONDS)
        ):
            tok.last_used_at = now
            await session.commit()
    return tok


async def _dispatch_tool(session: AsyncSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "get_system_status":
        sources = (await session.execute(select(Source))).scalars().all()
        editions = (await session.execute(select(Edition))).scalars().all()
        books = (await session.execute(select(Book))).scalars().all()
        recent_failures = [
            {"id": s.id, "name": s.name, "error": s.last_error, "failures": s.consecutive_failures}
            for s in sources
            if s.consecutive_failures > 0
        ]
        return {
            "version": __version__,
            "sources_count": len(sources),
            "editions_count": len(editions),
            "books_count": len(books),
            "failing_sources": recent_failures,
        }

    if name == "get_health":
        return await collect_deep_health(session)

    if name == "list_newsnow_catalog":
        cat = str(args.get("category") or "").strip()
        items = [
            {
                "id": i.id,
                "name": i.name,
                "category": i.category,
                "feed_type": i.feed_type,
                "default_strategy": i.default_strategy,
            }
            for i in NEWSNOW_CATALOG
            if not cat or i.category == cat
        ]
        return {"count": len(items), "items": items}

    if name == "list_sources":
        src_rows = (await session.execute(select(Source).order_by(desc(Source.id)))).scalars().all()
        return {
            "items": [
                {
                    "id": s.id,
                    "name": s.name,
                    "kind": s.kind,
                    "identifier": s.identifier,
                    "category": s.category,
                    "fetch_strategy": s.fetch_strategy,
                    "use_proxy": s.use_proxy,
                    "ai_options": s.ai_options or {},
                    "enabled": s.enabled,
                    "consecutive_failures": s.consecutive_failures,
                    "last_error": s.last_error,
                }
                for s in src_rows
            ]
        }

    if name == "add_source":
        kind = str(args["kind"])
        if kind not in ("newsnow", "rss", "rsshub", "webpage", "webpage_list"):
            raise ValueError(f"Unsupported source kind: {kind}")
        ai_opts = args.get("ai_options")
        src = Source(
            name=str(args["name"]).strip(),
            kind=kind,
            identifier=str(args["identifier"]).strip(),
            category=str(args.get("category") or "综合"),
            fetch_strategy=str(args.get("fetch_strategy") or "extract_web"),
            use_proxy=str(args.get("use_proxy") or "auto"),
            ai_options=ai_opts if isinstance(ai_opts, dict) else {},
            max_items=max(1, min(60, int(args.get("max_items") or 15))),
        )
        session.add(src)
        await session.commit()
        await session.refresh(src)
        return {"id": src.id, "name": src.name, "kind": src.kind, "status": "created"}

    if name == "update_source":
        existing_src = await session.get(Source, int(args["source_id"]))
        if existing_src is None:
            return {"error": "source_not_found"}
        if "kind" in args and args["kind"] is not None:
            new_kind = str(args["kind"])
            if new_kind not in ("newsnow", "rss", "rsshub", "webpage", "webpage_list"):
                raise ValueError(f"Unsupported source kind: {new_kind}")
            existing_src.kind = new_kind
        if "identifier" in args and args["identifier"] is not None:
            existing_src.identifier = str(args["identifier"]).strip()
        if "name" in args and args["name"] is not None:
            existing_src.name = str(args["name"]).strip() or existing_src.name
        if "category" in args and args["category"] is not None:
            existing_src.category = str(args["category"]).strip() or "综合"
        if "fetch_strategy" in args and args["fetch_strategy"] is not None:
            existing_src.fetch_strategy = str(args["fetch_strategy"])
        if "use_proxy" in args and args["use_proxy"] is not None:
            existing_src.use_proxy = str(args["use_proxy"])
        if "ai_options" in args and isinstance(args["ai_options"], dict):
            existing_src.ai_options = args["ai_options"]
        if "max_items" in args and args["max_items"] is not None:
            existing_src.max_items = max(1, min(60, int(args["max_items"])))
        if "enabled" in args and args["enabled"] is not None:
            existing_src.enabled = bool(args["enabled"])
        await session.commit()
        await session.refresh(existing_src)
        return {
            "id": existing_src.id,
            "name": existing_src.name,
            "kind": existing_src.kind,
            "identifier": existing_src.identifier,
            "enabled": existing_src.enabled,
            "status": "updated",
        }

    if name == "sync_source_now":
        return await run_sync_source(int(args["source_id"]))

    if name == "search_articles":
        arts, total = await search_articles_fulltext(
            session,
            str(args.get("query") or ""),
            category=args.get("category"),
            unread_only=bool(args.get("unread_only", False)),
            limit=min(int(args.get("limit") or 15), 50),
        )
        return {
            "total": total,
            "items": [
                {
                    "id": a.id,
                    "title": a.title,
                    "source_name": a.source_name,
                    "category": a.category,
                    "published_at": a.published_at.isoformat(),
                    "ai_summary": a.ai_summary,
                }
                for a in arts
            ],
        }

    if name == "read_article":
        art = await session.get(Article, int(args["article_id"]))
        if art is None:
            return {"error": "article_not_found"}
        return {
            "id": art.id,
            "title": art.title,
            "source_name": art.source_name,
            "canonical_url": art.canonical_url,
            "ai_summary": art.ai_summary,
            "plain_text": art.plain_text[:12000],
        }

    if name == "clip_url":
        target_url = str(args["url"]).strip()
        await assert_safe_public_url(target_url)
        net = await get_network_config(session)
        raw_html, final_url = await smart_fetch_text(
            target_url,
            use_proxy=str(args.get("use_proxy") or "auto"),
            outbound_proxy=net["outbound_proxy"],
            byparr_base_url=net["byparr_base_url"],
        )
        fallback_title = str(args.get("title") or "").strip()
        extracted = extract_from_webpage(raw_html, final_url, fallback_title=fallback_title)
        canon = canonicalize_url(final_url)
        existing = (
            await session.execute(select(Article).where(Article.canonical_url == canon))
        ).scalar_one_or_none()
        if existing is None:
            art = Article(
                canonical_url=canon,
                content_hash=compute_content_hash(extracted.title, extracted.plain_text),
                title=fallback_title or extracted.title,
                author=extracted.author,
                source_name="MCP 剪藏",
                category="剪藏",
                language=extracted.language,
                published_at=utcnow(),
                fetched_at=utcnow(),
                lead_image_url=extracted.lead_image_url,
                clean_html=extracted.clean_html,
                plain_text=extracted.plain_text,
                word_count=extracted.word_count,
                reading_time_min=extracted.reading_time_min,
                is_starred=True,
            )
            session.add(art)
            await session.commit()
            await session.refresh(art)
        else:
            art = existing

        push_result = None
        if bool(args.get("push_to_kindle", False)):
            push_result = await _push_single_article_to_kindle(
                session,
                art,
                target_email=str(args.get("target_email") or ""),
            )
        return {
            "id": art.id,
            "title": art.title,
            "canonical_url": art.canonical_url,
            "word_count": art.word_count,
            "delivery": push_result,
        }

    if name == "list_editions":
        ed_rows = (
            (await session.execute(select(Edition).order_by(desc(Edition.id)))).scalars().all()
        )
        return {
            "items": [
                {
                    "id": e.id,
                    "name": e.name,
                    "cron_expr": e.cron_expr,
                    "enabled": e.enabled,
                    "strip_links": e.strip_links,
                    "retention_days": e.retention_days,
                    "issue_counter": e.issue_counter,
                    "last_run_at": e.last_run_at.isoformat() if e.last_run_at else None,
                }
                for e in ed_rows
            ]
        }

    if name == "update_edition":
        ed = await session.get(Edition, int(args["edition_id"]))
        if ed is None:
            return {"error": "edition_not_found"}
        if "cron_expr" in args and args["cron_expr"] is not None:
            cron_str = str(args["cron_expr"]).strip()
            if not croniter.is_valid(cron_str):
                raise ValueError(f"Invalid cron expression: {cron_str}")
            ed.cron_expr = cron_str
        for str_field in ("name", "subtitle", "timezone", "cover_style", "target_emails"):
            if str_field in args and args[str_field] is not None:
                setattr(ed, str_field, str(args[str_field]).strip())
        for int_field in ("lookback_hours", "dedup_window_days", "max_articles", "retention_days"):
            if int_field in args and args[int_field] is not None:
                setattr(ed, int_field, int(args[int_field]))
        for bool_field in ("strip_links", "ai_briefing_enabled", "enabled"):
            if bool_field in args and args[bool_field] is not None:
                setattr(ed, bool_field, bool(args[bool_field]))
        await session.commit()
        await session.refresh(ed)
        return {
            "id": ed.id,
            "name": ed.name,
            "cron_expr": ed.cron_expr,
            "enabled": ed.enabled,
            "strip_links": ed.strip_links,
            "retention_days": ed.retention_days,
            "status": "updated",
        }

    if name == "trigger_edition_now":
        return await run_build_and_deliver_edition(
            int(args["edition_id"]),
            trigger="mcp",
            send_email=bool(args.get("send_email", True)),
        )

    if name == "list_books":
        book_rows = (
            (
                await session.execute(
                    select(Book)
                    .order_by(desc(Book.id))
                    .limit(min(int(args.get("limit") or 20), 100))
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "id": b.id,
                    "kind": b.kind,
                    "title": b.title,
                    "author": b.author,
                    "conversion_status": b.conversion_status,
                    "file_size_bytes": b.file_size_bytes,
                    "pushed_count": b.pushed_count,
                }
                for b in book_rows
            ]
        }

    if name == "deliver_book_now":
        return await run_deliver_book(
            int(args["book_id"]),
            target_email=str(args.get("target_email") or ""),
            trigger="mcp",
        )

    if name == "list_deliveries":
        deliv_rows = (
            (
                await session.execute(
                    select(Delivery)
                    .order_by(desc(Delivery.id))
                    .limit(min(int(args.get("limit") or 20), 100))
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [
                {
                    "id": d.id,
                    "edition_id": d.edition_id,
                    "book_id": d.book_id,
                    "title": d.title,
                    "status": d.status,
                    "article_count": d.article_count,
                    "file_size_bytes": d.file_size_bytes,
                    "duration_ms": d.duration_ms,
                    "stage_timings": d.stage_timings,
                    "error_message": d.error_message,
                    "created_at": d.created_at.isoformat(),
                }
                for d in deliv_rows
            ]
        }

    if name == "retry_delivery":
        d = await session.get(Delivery, int(args["delivery_id"]))
        if d is None:
            return {"error": "delivery_not_found"}
        if d.book_id:
            return await run_deliver_book(
                d.book_id, target_email=d.target_email, trigger="mcp_retry"
            )
        if d.edition_id:
            return await run_build_and_deliver_edition(
                d.edition_id, trigger="mcp_retry", send_email=True
            )
        return {"error": "delivery_cannot_be_retried"}

    if name == "test_extract_url":
        target_url = str(args["url"]).strip()
        await assert_safe_public_url(target_url)
        net = await get_network_config(session)
        raw_html, final_url = await smart_fetch_text(
            target_url,
            use_proxy=str(args.get("use_proxy") or "auto"),
            outbound_proxy=net["outbound_proxy"],
            byparr_base_url=net["byparr_base_url"],
        )
        ex = extract_from_webpage(raw_html, final_url)
        return {
            "final_url": final_url,
            "title": ex.title,
            "author": ex.author,
            "language": ex.language,
            "word_count": ex.word_count,
            "lead_image_url": ex.lead_image_url,
            "preview": ex.plain_text[:800],
        }

    if name == "validate_book_epub":
        book = await session.get(Book, int(args["book_id"]))
        if book is None or not book.kindle_epub_path:
            return {"error": "book_not_found"}
        res = validate_epub(Path(book.kindle_epub_path))
        return {
            "book_id": book.id,
            "valid": res.valid,
            "errors": res.errors,
            "warnings": res.warnings,
            "chapter_count": res.chapter_count,
            "image_count": res.image_count,
            "size_bytes": res.size_bytes,
        }

    raise ValueError(f"Unknown MCP tool: {name}")


@router.api_route("/api/v1/mcp", methods=["POST", "GET", "DELETE"])
async def mcp_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> Response:
    if not _is_allowed_mcp_origin(request):
        return Response(
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32002,
                        "message": "Forbidden: disallowed Origin header",
                    },
                }
            ),
            status_code=403,
            media_type="application/json",
        )

    if request.method != "POST":
        return Response(
            status_code=405,
            headers={"Allow": "POST", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION},
        )

    tok = await _authenticate_mcp(session, authorization)
    if tok is None:
        return Response(
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32000,
                        "message": "Unauthorized: invalid or disabled MCP token",
                    },
                }
            ),
            status_code=401,
            media_type="application/json",
        )

    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            ),
            status_code=400,
            media_type="application/json",
        )

    req_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}

    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "inkpress", "version": __version__},
        }
        return Response(
            content=json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}),
            media_type="application/json",
        )

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "ping":
        return Response(
            content=json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}),
            media_type="application/json",
        )

    if method == "tools/list":
        visible = [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            }
            for t in TOOLS_SPEC
            if (not tok.readonly or t["readonly"])
        ]
        return Response(
            content=json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"tools": visible}}),
            media_type="application/json",
        )

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        tool_args = params.get("arguments") or {}
        meta = TOOL_META.get(tool_name)
        if meta is None:
            return Response(
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    }
                ),
                media_type="application/json",
            )
        if tok.readonly and not meta["readonly"]:
            return Response(
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32001,
                            "message": f"Forbidden: token is read-only; '{tool_name}' requires write access",
                        },
                    }
                ),
                status_code=403,
                media_type="application/json",
            )
        try:
            data = await _dispatch_tool(session, tool_name, tool_args)
            return Response(
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(data, ensure_ascii=False, indent=2),
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                media_type="application/json",
            )
        except Exception as exc:
            await session.rollback()
            return Response(
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": f"Tool error: {exc}"}],
                        },
                    },
                    ensure_ascii=False,
                ),
                media_type="application/json",
            )

    return Response(
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        ),
        media_type="application/json",
    )
