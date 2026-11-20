from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress import __version__
from inkpress.db.base import get_db
from inkpress.db.models import Article, Book, Delivery, Edition, McpToken, Source, utcnow
from inkpress.db.search import search_articles_fulltext
from inkpress.ingestion.extractor import extract_from_webpage
from inkpress.ingestion.fetcher import smart_fetch_text
from inkpress.ingestion.newsnow_catalog import NEWSNOW_CATALOG
from inkpress.publishing.epub_validator import validate_epub
from inkpress.security import hash_token
from inkpress.settings_store import get_network_config
from inkpress.worker.tasks import run_build_and_deliver_edition, run_deliver_book, run_sync_source

router = APIRouter(tags=["mcp"])

MCP_PROTOCOL_VERSION = "2025-06-18"

TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "get_system_status",
        "description": "Inspect InkPress service status, counts of sources, articles, editions, books, and recent failures.",
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
        "description": "Add a new NewsNow, RSS, RSSHub (rsshub://...), or webpage source.",
        "readonly": False,
        "inputSchema": {
            "type": "object",
            "required": ["name", "kind", "identifier"],
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["newsnow", "rss", "rsshub", "webpage"]},
                "identifier": {"type": "string"},
                "category": {"type": "string"},
                "fetch_strategy": {
                    "type": "string",
                    "enum": ["fulltext_rss", "extract_web", "newsnow_digest", "byparr_browser"],
                },
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
        "name": "list_editions",
        "description": "List scheduled Kindle editions and their cron schedules.",
        "readonly": True,
        "inputSchema": {"type": "object", "properties": {}},
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
        "name": "test_extract_url",
        "description": "Diagnostic AI-Ops tool: fetch and extract a URL without saving, reporting word count and preview.",
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
        tok.last_used_at = utcnow()
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
                    "enabled": s.enabled,
                    "consecutive_failures": s.consecutive_failures,
                    "last_error": s.last_error,
                }
                for s in src_rows
            ]
        }

    if name == "add_source":
        src = Source(
            name=str(args["name"]).strip(),
            kind=str(args["kind"]),
            identifier=str(args["identifier"]).strip(),
            category=str(args.get("category") or "综合"),
            fetch_strategy=str(args.get("fetch_strategy") or "extract_web"),
        )
        session.add(src)
        await session.commit()
        await session.refresh(src)
        return {"id": src.id, "name": src.name, "status": "created"}

    if name == "sync_source_now":
        return await run_sync_source(int(args["source_id"]))

    if name == "search_articles":
        arts = await search_articles_fulltext(
            session,
            str(args.get("query") or ""),
            category=args.get("category"),
            limit=min(int(args.get("limit") or 15), 50),
        )
        return {
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
            ]
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
                    "issue_counter": e.issue_counter,
                    "last_run_at": e.last_run_at.isoformat() if e.last_run_at else None,
                }
                for e in ed_rows
            ]
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

    if name == "test_extract_url":
        net = await get_network_config(session)
        raw_html, final_url = await smart_fetch_text(
            str(args["url"]),
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
