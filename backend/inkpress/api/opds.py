from __future__ import annotations

import asyncio
import base64
import binascii
import html as html_escape
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlencode
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from lxml import etree
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.api.auth import (
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
    _client_ip,
    _prune_attempts,
    _set_session_cookie,
)
from inkpress.api.books import ALLOWED_ASSET_MIME_BY_EXT, _read_epub_zip_asset
from inkpress.api.deps import AUTH_COOKIE_NAME
from inkpress.config import get_settings
from inkpress.db.base import get_db
from inkpress.db.models import Book, Edition, McpToken, User, utcnow
from inkpress.publishing.book_converter import list_epub_chapters, read_epub_chapter_html
from inkpress.security import create_access_token, decode_access_token, hash_token, verify_password
from inkpress.worker.tasks import (
    build_and_deliver_edition_task,
    deliver_book_task,
    run_build_and_deliver_edition_bg,
    run_deliver_book_bg,
)

router = APIRouter(tags=["opds", "kindle"])

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/terms/"
OPDS_NS = "http://opds-spec.org/2010/catalog"
OS_NS = "http://a9.com/-/spec/opensearch/1.1/"

OPDS_NAV_MIME = "application/atom+xml;profile=opds-catalog;kind=navigation;charset=utf-8"
OPDS_ACQ_MIME = "application/atom+xml;profile=opds-catalog;kind=acquisition;charset=utf-8"


@dataclass
class _EReaderPrincipal:
    username: str
    readonly: bool = False
    query_token: str = ""


def _with_token(path: str, token: str, extra_params: dict[str, str] | None = None) -> str:
    params: dict[str, str] = {}
    if extra_params:
        params.update(extra_params)
    if token:
        params["token"] = token
    if not params:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urlencode(params)}"


def _iso_utc(dt: datetime | None) -> str:
    if dt is None:
        dt = utcnow()
    aware = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_size_human(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 KB"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


async def _resolve_ereader_principal(
    request: Request,
    session: AsyncSession,
    cookie_token: str | None,
    query_token: str,
) -> _EReaderPrincipal | None:
    # 1. Cookie session JWT
    if cookie_token:
        payload = decode_access_token(cookie_token)
        if payload and payload.get("sub"):
            user = await session.get(User, int(payload["sub"]))
            if user is not None:
                return _EReaderPrincipal(username=user.username, readonly=False)

    # 2. Authorization header: Bearer (JWT or MCP token) or HTTP Basic (username:password)
    auth_hdr = (request.headers.get("authorization") or "").strip()
    if auth_hdr.lower().startswith("bearer "):
        raw_bearer = auth_hdr.split(" ", 1)[1].strip()
        if raw_bearer:
            jwt_payload = decode_access_token(raw_bearer)
            if jwt_payload and jwt_payload.get("sub"):
                user = await session.get(User, int(jwt_payload["sub"]))
                if user is not None:
                    return _EReaderPrincipal(username=user.username, readonly=False)
            tok_row = (
                await session.execute(
                    select(McpToken).where(
                        McpToken.token_hash == hash_token(raw_bearer),
                        McpToken.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if tok_row is not None:
                return _EReaderPrincipal(username=f"token:{tok_row.name}", readonly=tok_row.readonly)

    elif auth_hdr.lower().startswith("basic "):
        b64_part = auth_hdr.split(" ", 1)[1].strip()
        ip = _client_ip(request)
        now = time.monotonic()
        bucket = _prune_attempts(ip, now)
        if len(bucket) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many authentication attempts, try again later",
            )
        try:
            decoded = base64.b64decode(b64_part).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            decoded = ""
        if ":" in decoded:
            uname, passwd = decoded.split(":", 1)
            user = (
                await session.execute(select(User).where(User.username == uname.strip()))
            ).scalar_one_or_none()
            if user is not None and verify_password(passwd, user.password_hash):
                return _EReaderPrincipal(username=user.username, readonly=False)
            bucket.append(now)

    # 3. Query parameter token (`?token=ink_...`) for bookmarkable Kindle / OPDS URLs
    cleaned_qtok = query_token.strip()
    if cleaned_qtok:
        tok_row = (
            await session.execute(
                select(McpToken).where(
                    McpToken.token_hash == hash_token(cleaned_qtok),
                    McpToken.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if tok_row is not None:
            return _EReaderPrincipal(
                username=f"token:{tok_row.name}",
                readonly=tok_row.readonly,
                query_token=cleaned_qtok,
            )

    return None


async def _require_opds_principal(
    request: Request,
    session: AsyncSession = Depends(get_db),
    inkpress_session: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str = Query(default="", max_length=256),
) -> _EReaderPrincipal:
    principal = await _resolve_ereader_principal(request, session, inkpress_session, token)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OPDS authentication required",
            headers={"WWW-Authenticate": 'Basic realm="InkPress OPDS", charset="UTF-8"'},
        )
    return principal


def _build_opds_feed_root(
    *,
    feed_id: str,
    title: str,
    self_href: str,
    self_type: str,
    qtok: str,
    updated: datetime | None = None,
) -> etree._Element:
    nsmap = {None: ATOM_NS, "dc": DC_NS, "opds": OPDS_NS}
    feed = etree.Element(f"{{{ATOM_NS}}}feed", nsmap=nsmap)
    etree.SubElement(feed, f"{{{ATOM_NS}}}id").text = feed_id
    etree.SubElement(feed, f"{{{ATOM_NS}}}title").text = title
    etree.SubElement(feed, f"{{{ATOM_NS}}}updated").text = _iso_utc(updated)
    author_el = etree.SubElement(feed, f"{{{ATOM_NS}}}author")
    etree.SubElement(author_el, f"{{{ATOM_NS}}}name").text = "InkPress"

    etree.SubElement(
        feed,
        f"{{{ATOM_NS}}}link",
        rel="self",
        href=_with_token(self_href, qtok),
        type=self_type,
    )
    etree.SubElement(
        feed,
        f"{{{ATOM_NS}}}link",
        rel="start",
        href=_with_token("/api/v1/opds", qtok),
        type=OPDS_NAV_MIME,
    )
    etree.SubElement(
        feed,
        f"{{{ATOM_NS}}}link",
        rel="search",
        href=_with_token("/api/v1/opds/opensearch.xml", qtok),
        type="application/opensearchdescription+xml",
        title="搜索 InkPress 书库",
    )
    return feed


def _resolve_book_mobi_path(book: Book) -> Path | None:
    """Locate a companion KF8 `.mobi` file inside `library_data_dir` or alongside `kindle_epub_path`."""
    lib_root = get_settings().library_data_dir.resolve()
    epub_parent = (
        Path(book.kindle_epub_path).resolve().parent if book.kindle_epub_path else None
    )
    meta = book.metadata_json if isinstance(book.metadata_json, dict) else {}
    candidates: list[str] = []
    raw_mobi = str(meta.get("kindle_mobi_path") or "").strip()
    if raw_mobi:
        candidates.append(raw_mobi)
    if book.kindle_epub_path and book.kindle_epub_path.lower().endswith(".epub"):
        candidates.append(str(Path(book.kindle_epub_path).with_suffix(".mobi")))
    for cand in candidates:
        p = Path(cand)
        if not p.exists():
            continue
        resolved = p.resolve()
        if resolved.is_relative_to(lib_root) or (
            epub_parent is not None and resolved.parent == epub_parent
        ):
            return p
    return None


def _append_book_acq_entry(feed: etree._Element, book: Book, qtok: str) -> None:
    if not book.kindle_epub_path:
        return
    path = Path(book.kindle_epub_path)
    if not path.exists():
        return

    ext = path.suffix.lower() or ".epub"
    mime = "application/pdf" if ext == ".pdf" else "application/epub+zip"
    size_bytes = book.file_size_bytes or path.stat().st_size

    entry = etree.SubElement(feed, f"{{{ATOM_NS}}}entry")
    etree.SubElement(entry, f"{{{ATOM_NS}}}id").text = f"urn:inkpress:book:{book.id}"
    etree.SubElement(entry, f"{{{ATOM_NS}}}title").text = book.title
    etree.SubElement(entry, f"{{{ATOM_NS}}}updated").text = _iso_utc(book.created_at)

    author_el = etree.SubElement(entry, f"{{{ATOM_NS}}}author")
    etree.SubElement(author_el, f"{{{ATOM_NS}}}name").text = book.author or "InkPress"

    if book.language:
        etree.SubElement(entry, f"{{{DC_NS}}}language").text = book.language

    desc_parts = [p for p in (book.description, _format_size_human(size_bytes)) if p]
    summary_el = etree.SubElement(entry, f"{{{ATOM_NS}}}summary", type="text")
    summary_el.text = " · ".join(desc_parts)

    dl_href = _with_token(f"/api/v1/opds/download/{book.id}", qtok)
    etree.SubElement(
        entry,
        f"{{{ATOM_NS}}}link",
        rel="http://opds-spec.org/acquisition",
        href=dl_href,
        type=mime,
        length=str(size_bytes),
    )

    mobi_path = _resolve_book_mobi_path(book)
    if mobi_path is not None:
        mobi_href = _with_token(f"/api/v1/opds/download/{book.id}/inkpress_{book.id}.mobi", qtok)
        etree.SubElement(
            entry,
            f"{{{ATOM_NS}}}link",
            rel="http://opds-spec.org/acquisition",
            href=mobi_href,
            type="application/x-mobipocket-ebook",
            length=str(mobi_path.stat().st_size),
        )

    if book.cover_path and Path(book.cover_path).exists():
        cov_href = _with_token(f"/api/v1/opds/cover/{book.id}", qtok)
        etree.SubElement(
            entry,
            f"{{{ATOM_NS}}}link",
            rel="http://opds-spec.org/image",
            href=cov_href,
            type="image/jpeg",
        )
        etree.SubElement(
            entry,
            f"{{{ATOM_NS}}}link",
            rel="http://opds-spec.org/image/thumbnail",
            href=cov_href,
            type="image/jpeg",
        )


def _xml_response(root: etree._Element, media_type: str) -> Response:
    payload = etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True)
    return Response(content=payload, media_type=media_type)


@router.get("/api/v1/opds")
async def opds_root_catalog(
    principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """OPDS 1.2 Root Navigation Catalog for KOReader, Boox, PocketBook, and Foliate."""
    latest_dt = (
        await session.execute(
            select(Book.created_at)
            .where(Book.conversion_status == "ready")
            .order_by(desc(Book.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    qtok = principal.query_token
    feed = _build_opds_feed_root(
        feed_id="urn:inkpress:opds:root",
        title="InkPress 书架与每日晨报",
        self_href="/api/v1/opds",
        self_type=OPDS_NAV_MIME,
        qtok=qtok,
        updated=latest_dt,
    )

    sections = (
        (
            "urn:inkpress:opds:recent",
            "最新全部馆藏",
            "/api/v1/opds/recent",
            "按生成与入库时间倒序排列的最新刊物与电子书",
        ),
        (
            "urn:inkpress:opds:editions",
            "每日精选期刊",
            "/api/v1/opds/editions",
            "自动编排的每日晨报、图卦辑要与过往期刊归档",
        ),
        (
            "urn:inkpress:opds:books",
            "个人书架与网页剪藏",
            "/api/v1/opds/books",
            "上传转换的电子书、杂志与单篇剪藏",
        ),
    )
    for sec_id, sec_title, sec_href, sec_desc in sections:
        entry = etree.SubElement(feed, f"{{{ATOM_NS}}}entry")
        etree.SubElement(entry, f"{{{ATOM_NS}}}id").text = sec_id
        etree.SubElement(entry, f"{{{ATOM_NS}}}title").text = sec_title
        etree.SubElement(entry, f"{{{ATOM_NS}}}updated").text = _iso_utc(latest_dt)
        content_el = etree.SubElement(entry, f"{{{ATOM_NS}}}content", type="text")
        content_el.text = sec_desc
        etree.SubElement(
            entry,
            f"{{{ATOM_NS}}}link",
            rel="subsection",
            href=_with_token(sec_href, qtok),
            type=OPDS_ACQ_MIME,
        )

    return _xml_response(feed, OPDS_NAV_MIME)


@router.get("/api/v1/opds/opensearch.xml")
async def opds_opensearch(
    request: Request,
    principal: _EReaderPrincipal = Depends(_require_opds_principal),
) -> Response:
    base_url = str(request.base_url).rstrip("/")
    qtok = principal.query_token
    search_tpl = (
        f"{base_url}/api/v1/opds/search?q={{searchTerms}}&token={quote(qtok)}"
        if qtok
        else f"{base_url}/api/v1/opds/search?q={{searchTerms}}"
    )
    root = etree.Element(f"{{{OS_NS}}}OpenSearchDescription", nsmap={None: OS_NS})
    etree.SubElement(root, f"{{{OS_NS}}}ShortName").text = "InkPress"
    etree.SubElement(root, f"{{{OS_NS}}}Description").text = "Search InkPress OPDS Library"
    etree.SubElement(root, f"{{{OS_NS}}}InputEncoding").text = "UTF-8"
    etree.SubElement(root, f"{{{OS_NS}}}OutputEncoding").text = "UTF-8"
    etree.SubElement(
        root,
        f"{{{OS_NS}}}Url",
        type=OPDS_ACQ_MIME,
        template=search_tpl,
    )
    return _xml_response(root, "application/opensearchdescription+xml;charset=utf-8")


@router.get("/api/v1/opds/recent")
async def opds_recent_feed(
    limit: int = Query(default=50, ge=1, le=200),
    principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    books = (
        (
            await session.execute(
                select(Book)
                .where(Book.conversion_status == "ready")
                .order_by(desc(Book.created_at), desc(Book.id))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    feed = _build_opds_feed_root(
        feed_id="urn:inkpress:opds:recent",
        title="InkPress · 最新全部馆藏",
        self_href="/api/v1/opds/recent",
        self_type=OPDS_ACQ_MIME,
        qtok=principal.query_token,
        updated=books[0].created_at if books else None,
    )
    for b in books:
        _append_book_acq_entry(feed, b, principal.query_token)
    return _xml_response(feed, OPDS_ACQ_MIME)


@router.get("/api/v1/opds/editions")
async def opds_editions_feed(
    limit: int = Query(default=50, ge=1, le=200),
    principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    books = (
        (
            await session.execute(
                select(Book)
                .where(Book.conversion_status == "ready", Book.kind == "edition_issue")
                .order_by(desc(Book.created_at), desc(Book.id))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    feed = _build_opds_feed_root(
        feed_id="urn:inkpress:opds:editions",
        title="InkPress · 每日精选期刊",
        self_href="/api/v1/opds/editions",
        self_type=OPDS_ACQ_MIME,
        qtok=principal.query_token,
        updated=books[0].created_at if books else None,
    )
    for b in books:
        _append_book_acq_entry(feed, b, principal.query_token)
    return _xml_response(feed, OPDS_ACQ_MIME)


@router.get("/api/v1/opds/books")
async def opds_books_feed(
    limit: int = Query(default=50, ge=1, le=200),
    principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    books = (
        (
            await session.execute(
                select(Book)
                .where(
                    Book.conversion_status == "ready",
                    Book.kind.in_(("uploaded_book", "magazine", "web_clip")),
                )
                .order_by(desc(Book.created_at), desc(Book.id))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    feed = _build_opds_feed_root(
        feed_id="urn:inkpress:opds:books",
        title="InkPress · 个人书架与网页剪藏",
        self_href="/api/v1/opds/books",
        self_type=OPDS_ACQ_MIME,
        qtok=principal.query_token,
        updated=books[0].created_at if books else None,
    )
    for b in books:
        _append_book_acq_entry(feed, b, principal.query_token)
    return _xml_response(feed, OPDS_ACQ_MIME)


@router.get("/api/v1/opds/search")
async def opds_search_feed(
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> Response:
    kw = q.strip()
    stmt = select(Book).where(Book.conversion_status == "ready")
    if kw:
        escaped_kw = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pat = f"%{escaped_kw}%"
        stmt = stmt.where(
            or_(
                Book.title.ilike(like_pat, escape="\\"),
                Book.author.ilike(like_pat, escape="\\"),
                Book.description.ilike(like_pat, escape="\\"),
            )
        )
    books = (
        (await session.execute(stmt.order_by(desc(Book.created_at), desc(Book.id)).limit(limit)))
        .scalars()
        .all()
    )
    feed = _build_opds_feed_root(
        feed_id="urn:inkpress:opds:search",
        title=f"InkPress · 搜索: {kw}" if kw else "InkPress · 搜索结果",
        self_href=f"/api/v1/opds/search?q={quote(kw)}",
        self_type=OPDS_ACQ_MIME,
        qtok=principal.query_token,
        updated=books[0].created_at if books else None,
    )
    for b in books:
        _append_book_acq_entry(feed, b, principal.query_token)
    return _xml_response(feed, OPDS_ACQ_MIME)


def _serve_book_download(book: Book, *, want_mobi: bool = False) -> FileResponse:
    if want_mobi:
        mobi_path = _resolve_book_mobi_path(book)
        if mobi_path is None:
            raise HTTPException(status_code=404, detail="Kindle MOBI file not available")
        # Pure ASCII filename without RFC-5987 UTF-8 extension: required by Kindle Experimental Browser
        return FileResponse(
            mobi_path,
            media_type="application/x-mobipocket-ebook",
            headers={
                "Content-Disposition": f'attachment; filename="inkpress_{book.id}.mobi"'
            },
        )

    if not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book file not ready")
    path = Path(book.kindle_epub_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Book file missing on disk")
    ext = path.suffix or ".epub"
    media_type = "application/epub+zip" if ext.lower() == ".epub" else "application/pdf"
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", book.title).strip("_") or f"inkpress_{book.id}"
    utf8_name = quote(f"{book.title}{ext}")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_stem[:60]}{ext}"; filename*=UTF-8\'\'{utf8_name}'
            )
        },
    )


@router.get("/api/v1/opds/download/{book_id}")
async def opds_download_book(
    book_id: int,
    fmt: str = Query(default="", max_length=16),
    _principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book file not ready")
    return _serve_book_download(book, want_mobi=fmt.strip().lower() == "mobi")


@router.get("/api/v1/opds/download/{book_id}/{requested_name}")
async def opds_download_book_named(
    book_id: int,
    requested_name: str,
    _principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book file not ready")
    return _serve_book_download(book, want_mobi=requested_name.strip().lower().endswith(".mobi"))


@router.get("/api/v1/opds/cover/{book_id}")
async def opds_book_cover(
    book_id: int,
    _principal: _EReaderPrincipal = Depends(_require_opds_principal),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    book = await session.get(Book, book_id)
    if book is None or not book.cover_path or not Path(book.cover_path).exists():
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(book.cover_path, media_type="image/jpeg")


# ==============================================================================
# /kindle — Zero-JS High-Contrast HTML Direct-Download Page for E-Ink Browsers
# ==============================================================================

_KINDLE_EINK_CSS = """
body { font-family: serif; background: #fff; color: #000; margin: 0; padding: 14px; line-height: 1.5; max-width: 720px; margin-left: auto; margin-right: auto; }
h1 { font-size: 22px; margin: 0 0 4px 0; }
h2 { font-size: 17px; margin: 20px 0 8px 0; padding-bottom: 4px; border-bottom: 2px solid #000; }
p, div, li { font-size: 15px; }
a { color: #000; text-decoration: underline; }
.bar { border-bottom: 3px solid #000; padding-bottom: 10px; margin-bottom: 14px; }
.meta { font-size: 13px; color: #222; }
.box { border: 2px solid #000; padding: 12px; margin-bottom: 14px; }
.btn { display: inline-block; border: 2px solid #000; background: #000; color: #fff; text-decoration: none; font-weight: bold; font-size: 15px; padding: 10px 16px; margin: 6px 6px 2px 0; cursor: pointer; }
.btn-sec { display: inline-block; border: 2px solid #000; background: #fff; color: #000; text-decoration: none; font-weight: bold; font-size: 14px; padding: 8px 12px; margin: 6px 6px 2px 0; cursor: pointer; }
.row { padding: 10px 0; border-bottom: 1px solid #888; }
.inp { display: block; width: 94%; border: 2px solid #000; padding: 10px; font-size: 16px; margin: 6px 0 12px 0; background: #fff; color: #000; }
.msg { border: 2px dashed #000; padding: 10px; margin-bottom: 14px; font-weight: bold; }
.reader-body img { max-width: 100%; height: auto; display: block; margin: 10px auto; }
.reader-body blockquote { border-left: 3px solid #000; margin: 10px 0; padding-left: 10px; }
form { display: inline; margin: 0; }
""".strip()


def _render_kindle_login_html(error_msg: str = "") -> str:
    err_html = f'<div class="msg">{html_escape.escape(error_msg)}</div>' if error_msg else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>\n'
        "<title>InkPress · Kindle 登录</title>\n"
        f"<style>{_KINDLE_EINK_CSS}</style>\n"
        "</head>\n<body>\n"
        '<div class="bar"><h1>InkPress · 墨水屏直连书架</h1>'
        '<div class="meta">无脚本高对比极简版 · 适配 Kindle / Kobo / 文石内置浏览器</div></div>\n'
        f"{err_html}\n"
        '<div class="box">\n'
        '<form method="post" action="/kindle/login" style="display:block;">\n'
        "<label><strong>管理员账号</strong></label>\n"
        '<input class="inp" type="text" name="username" required="required" autocomplete="username"/>\n'
        "<label><strong>登录密码</strong></label>\n"
        '<input class="inp" type="password" name="password" required="required" autocomplete="current-password"/>\n'
        '<button class="btn" type="submit">登录进入直下页</button>\n'
        "</form>\n"
        "</div>\n"
        "</body>\n</html>"
    )


@router.post("/kindle/login", include_in_schema=False)
async def kindle_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_db),
) -> Response:
    ip = _client_ip(request)
    now = time.monotonic()
    bucket = _prune_attempts(ip, now)
    if len(bucket) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
        return HTMLResponse(
            _render_kindle_login_html("登录尝试过于频繁，请 5 分钟后再试。"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = (
        await session.execute(select(User).where(User.username == username.strip()))
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        bucket.append(now)
        return HTMLResponse(
            _render_kindle_login_html("账号或密码不正确，请重试。"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_access_token(user.id, user.username)
    redirect = RedirectResponse(url="/kindle", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(redirect, request, token)
    return redirect


@router.post("/kindle/logout", include_in_schema=False)
async def kindle_logout_submit() -> Response:
    redirect = RedirectResponse(url="/kindle", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return redirect


@router.post("/kindle/build/{edition_id}", include_in_schema=False)
async def kindle_trigger_preview_build(
    edition_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    inkpress_session: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str = Query(default="", max_length=256),
    session: AsyncSession = Depends(get_db),
) -> Response:
    principal = await _resolve_ereader_principal(request, session, inkpress_session, token)
    if principal is None:
        return HTMLResponse(_render_kindle_login_html("请先登录。"), status_code=401)
    if principal.readonly:
        return RedirectResponse(
            url=_with_token("/kindle", principal.query_token, {"msg": "readonly"}),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    ed = await session.get(Edition, edition_id)
    if ed is None:
        raise HTTPException(status_code=404, detail="Edition not found")

    try:
        await build_and_deliver_edition_task.kiq(
            ed.id, trigger="kindle_web", send_email=False, ignore_dedup=True
        )
    except Exception:
        background_tasks.add_task(
            run_build_and_deliver_edition_bg,
            ed.id,
            trigger="kindle_web",
            send_email=False,
            ignore_dedup=True,
        )
    return RedirectResponse(
        url=_with_token("/kindle", principal.query_token, {"msg": "queued"}),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/kindle/push/{book_id}", include_in_schema=False)
async def kindle_push_book_email(
    book_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    inkpress_session: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str = Query(default="", max_length=256),
    session: AsyncSession = Depends(get_db),
) -> Response:
    principal = await _resolve_ereader_principal(request, session, inkpress_session, token)
    if principal is None:
        return HTMLResponse(_render_kindle_login_html("请先登录。"), status_code=401)
    if principal.readonly:
        return RedirectResponse(
            url=_with_token("/kindle", principal.query_token, {"msg": "readonly"}),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    book = await session.get(Book, book_id)
    if book is None or book.conversion_status != "ready":
        raise HTTPException(status_code=404, detail="Book not ready")

    try:
        await deliver_book_task.kiq(book.id, trigger="kindle_web")
    except Exception:
        background_tasks.add_task(run_deliver_book_bg, book.id, trigger="kindle_web")
    return RedirectResponse(
        url=_with_token("/kindle", principal.query_token, {"msg": "pushed"}),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/kindle/asset/{book_id}/{asset_path:path}", include_in_schema=False)
async def kindle_reader_epub_asset(
    book_id: int,
    asset_path: str,
    request: Request,
    inkpress_session: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str = Query(default="", max_length=256),
    session: AsyncSession = Depends(get_db),
) -> Response:
    principal = await _resolve_ereader_principal(request, session, inkpress_session, token)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    book = await session.get(Book, book_id)
    if book is None or not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book not found")
    epub_path = Path(book.kindle_epub_path)
    if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
        raise HTTPException(status_code=404, detail="EPUB archive not available")

    decoded = unquote(asset_path).replace("\\", "/")
    normalized = posixpath.normpath(decoded).lstrip("/")
    if not normalized or normalized == "." or normalized.startswith(".."):
        raise HTTPException(status_code=400, detail="Invalid asset path")

    ext = Path(normalized).suffix.lower()
    media_type = ALLOWED_ASSET_MIME_BY_EXT.get(ext)
    if media_type is None:
        raise HTTPException(status_code=403, detail="Disallowed EPUB asset type")

    try:
        content = await asyncio.to_thread(_read_epub_zip_asset, epub_path, normalized)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Asset not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Corrupt EPUB archive") from exc

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.get("/kindle/read/{book_id}", response_class=HTMLResponse, include_in_schema=False)
async def kindle_online_reader_page(
    book_id: int,
    request: Request,
    ch: int = Query(default=0, ge=0),
    inkpress_session: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str = Query(default="", max_length=256),
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Zero-JS high-contrast paginated HTML reader for reading directly inside E-Ink browsers."""
    principal = await _resolve_ereader_principal(request, session, inkpress_session, token)
    if principal is None:
        return HTMLResponse(_render_kindle_login_html("请先登录。"), status_code=200)

    book = await session.get(Book, book_id)
    if book is None or not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book not found")
    epub_path = Path(book.kindle_epub_path)
    if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
        raise HTTPException(status_code=400, detail="Only EPUB books support inline reading")

    chapters = await asyncio.to_thread(list_epub_chapters, epub_path)
    if not chapters:
        raise HTTPException(status_code=404, detail="No readable chapters in EPUB")

    safe_idx = min(max(0, ch), len(chapters) - 1)
    qtok = principal.query_token
    q_suffix = f"?token={quote(qtok)}" if qtok else ""
    rendered = await asyncio.to_thread(
        read_epub_chapter_html,
        epub_path,
        safe_idx,
        book.id,
        asset_url_prefix=f"/kindle/asset/{book.id}",
        asset_query_suffix=q_suffix,
    )

    back_href = html_escape.escape(_with_token("/kindle", qtok), quote=True)
    nav_btns: list[str] = [f'<a class="btn-sec" href="{back_href}">← 返回书架</a>']
    if safe_idx > 0:
        prev_href = html_escape.escape(
            _with_token(f"/kindle/read/{book.id}", qtok, {"ch": str(safe_idx - 1)}),
            quote=True,
        )
        nav_btns.append(f'<a class="btn-sec" href="{prev_href}">上一章</a>')
    if safe_idx + 1 < len(chapters):
        next_href = html_escape.escape(
            _with_token(f"/kindle/read/{book.id}", qtok, {"ch": str(safe_idx + 1)}),
            quote=True,
        )
        nav_btns.append(f'<a class="btn" href="{next_href}">下一章 →</a>')
    nav_bar = " ".join(nav_btns)

    toc_items: list[str] = []
    for item in chapters:
        idx_val = int(str(item["index"]))
        ch_href = html_escape.escape(
            _with_token(f"/kindle/read/{book.id}", qtok, {"ch": str(idx_val)}),
            quote=True,
        )
        ch_label = html_escape.escape(str(item["title"]))
        marker = "▶ " if idx_val == safe_idx else ""
        toc_items.append(f'<li><a href="{ch_href}">{marker}{ch_label}</a></li>')

    html_doc = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>\n'
        f"<title>{html_escape.escape(book.title)} · 在线直读</title>\n"
        f"<style>{_KINDLE_EINK_CSS}</style>\n"
        "</head>\n<body>\n"
        '<div class="bar">'
        f"<h1>{html_escape.escape(book.title)}</h1>"
        f'<div class="meta">第 {safe_idx + 1} / {len(chapters)} 章</div>'
        f'<div style="margin-top:6px;">{nav_bar}</div>'
        "</div>\n"
        f'<div class="reader-body">{rendered["html"]}</div>\n'
        f'<div style="margin-top:18px;padding-top:10px;border-top:2px solid #000;">{nav_bar}</div>\n'
        "<h2>全书目录</h2>\n<ol>\n"
        + "\n".join(toc_items)
        + "\n</ol>\n</body>\n</html>"
    )
    return HTMLResponse(html_doc)


@router.get("/kindle", response_class=HTMLResponse, include_in_schema=False)
async def kindle_minimal_page(
    request: Request,
    msg: str = Query(default="", max_length=32),
    inkpress_session: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    token: str = Query(default="", max_length=256),
    session: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Lightweight zero-JS HTML download page tailored for Kindle Experimental Browser."""
    principal = await _resolve_ereader_principal(request, session, inkpress_session, token)
    if principal is None:
        return HTMLResponse(_render_kindle_login_html(), status_code=200)

    try:
        local_tz = ZoneInfo(get_settings().tz)
    except Exception:
        local_tz = ZoneInfo("Asia/Shanghai")
    now_str = datetime.now(local_tz).strftime("%Y-%m-%d %H:%M")
    qtok = principal.query_token

    editions = (await session.execute(select(Edition).order_by(Edition.id))).scalars().all()
    issue_books = (
        (
            await session.execute(
                select(Book)
                .where(Book.conversion_status == "ready", Book.kind == "edition_issue")
                .order_by(desc(Book.created_at), desc(Book.id))
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    other_books = (
        (
            await session.execute(
                select(Book)
                .where(
                    Book.conversion_status == "ready",
                    Book.kind.in_(("uploaded_book", "magazine", "web_clip")),
                )
                .order_by(desc(Book.created_at), desc(Book.id))
                .limit(15)
            )
        )
        .scalars()
        .all()
    )

    banner_html = ""
    if msg == "queued":
        banner_html = '<div class="msg">已加入后台编译队列，约 30～60 秒后点击「刷新页面」即可下载最新一期。</div>'
    elif msg == "pushed":
        banner_html = '<div class="msg">已加入邮件推送队列，稍后将通过 Send-to-Kindle 送达设备。</div>'
    elif msg == "readonly":
        banner_html = '<div class="msg">当前令牌为只读权限，仅支持下载与在线阅读，不支持触发编译或推送。</div>'

    def _fmt_dt(dt: datetime) -> str:
        aware = dt.astimezone(local_tz) if dt.tzinfo is not None else dt.replace(tzinfo=UTC).astimezone(local_tz)
        return aware.strftime("%m-%d %H:%M")

    hero_parts: list[str] = []
    if issue_books:
        latest = issue_books[0]
        latest_ext = Path(latest.kindle_epub_path).suffix.lower() or ".epub"
        epub_url = html_escape.escape(
            _with_token(f"/api/v1/opds/download/{latest.id}/inkpress_{latest.id}{latest_ext}", qtok),
            quote=True,
        )
        latest_mobi = _resolve_book_mobi_path(latest)
        hero_parts.append('<div class="box">')
        hero_parts.append('<div class="meta"><strong>【最新一期直下】</strong></div>')
        hero_parts.append(f'<div style="font-size:19px;font-weight:bold;margin:4px 0;">{html_escape.escape(latest.title)}</div>')
        hero_parts.append(
            f'<div class="meta">{html_escape.escape(latest.description)} · {_format_size_human(latest.file_size_bytes)} · {_fmt_dt(latest.created_at)}</div>'
        )
        if latest_mobi is not None:
            mobi_url = html_escape.escape(
                _with_token(f"/api/v1/opds/download/{latest.id}/inkpress_{latest.id}.mobi", qtok),
                quote=True,
            )
            hero_parts.append(f'<a class="btn" href="{mobi_url}">直下 Kindle .MOBI</a>')
            hero_parts.append(f'<a class="btn-sec" href="{epub_url}">立即下载最新 EPUB</a>')
        else:
            hero_parts.append(f'<a class="btn" href="{epub_url}">立即下载最新 EPUB</a>')
        if latest_ext == ".epub":
            read_url = html_escape.escape(_with_token(f"/kindle/read/{latest.id}", qtok), quote=True)
            hero_parts.append(f'<a class="btn-sec" href="{read_url}">浏览器直读</a>')
        if not principal.readonly:
            push_url = html_escape.escape(_with_token(f"/kindle/push/{latest.id}", qtok), quote=True)
            hero_parts.append(
                f'<form method="post" action="{push_url}">'
                '<button class="btn-sec" type="submit">推至 Kindle 邮箱</button>'
                "</form>"
            )
            for ed in editions[:2]:
                build_url = html_escape.escape(_with_token(f"/kindle/build/{ed.id}", qtok), quote=True)
                hero_parts.append(
                    f'<form method="post" action="{build_url}">'
                    f'<button class="btn-sec" type="submit">重新编译《{html_escape.escape(ed.name)}》</button>'
                    "</form>"
                )
        hero_parts.append("</div>")
    elif editions and not principal.readonly:
        ed0 = editions[0]
        build_url = html_escape.escape(_with_token(f"/kindle/build/{ed0.id}", qtok), quote=True)
        hero_parts.append(
            '<div class="box"><p>尚无已生成的期刊归档。</p>'
            f'<form method="post" action="{build_url}">'
            f'<button class="btn" type="submit">立即生成《{html_escape.escape(ed0.name)}》</button>'
            "</form></div>"
        )

    def _book_action_links(b: Book) -> str:
        ext = Path(b.kindle_epub_path).suffix.lower() or ".epub"
        epub_href = html_escape.escape(
            _with_token(f"/api/v1/opds/download/{b.id}/inkpress_{b.id}{ext}", qtok),
            quote=True,
        )
        links: list[str] = []
        if _resolve_book_mobi_path(b) is not None:
            mobi_href = html_escape.escape(
                _with_token(f"/api/v1/opds/download/{b.id}/inkpress_{b.id}.mobi", qtok),
                quote=True,
            )
            links.append(f'<a href="{mobi_href}">[.MOBI]</a>')
        links.append(f'<a href="{epub_href}">[{ext.upper()}]</a>')
        if ext == ".epub":
            rd_href = html_escape.escape(_with_token(f"/kindle/read/{b.id}", qtok), quote=True)
            links.append(f'<a href="{rd_href}">[在线直读]</a>')
        return " · ".join(links)

    issues_rows: list[str] = []
    for b in issue_books[1:]:
        b_mobi = _resolve_book_mobi_path(b)
        primary_dl = (
            f"/api/v1/opds/download/{b.id}/inkpress_{b.id}.mobi"
            if b_mobi is not None
            else f"/api/v1/opds/download/{b.id}/inkpress_{b.id}.epub"
        )
        dl_url = html_escape.escape(_with_token(primary_dl, qtok), quote=True)
        issues_rows.append(
            '<div class="row">'
            f'<div><strong><a href="{dl_url}">{html_escape.escape(b.title)}</a></strong></div>'
            f'<div class="meta">{html_escape.escape(b.description)} · {_format_size_human(b.file_size_bytes)} · {_fmt_dt(b.created_at)} · {_book_action_links(b)}</div>'
            "</div>"
        )

    books_rows: list[str] = []
    for b in other_books:
        b_mobi = _resolve_book_mobi_path(b)
        b_ext = Path(b.kindle_epub_path).suffix.lower() or ".epub"
        primary_dl = (
            f"/api/v1/opds/download/{b.id}/inkpress_{b.id}.mobi"
            if b_mobi is not None
            else f"/api/v1/opds/download/{b.id}/inkpress_{b.id}{b_ext}"
        )
        dl_url = html_escape.escape(_with_token(primary_dl, qtok), quote=True)
        author_part = f"{html_escape.escape(b.author)} · " if b.author else ""
        books_rows.append(
            '<div class="row">'
            f'<div><strong><a href="{dl_url}">{html_escape.escape(b.title)}</a></strong></div>'
            f'<div class="meta">{author_part}{_format_size_human(b.file_size_bytes)} · {_fmt_dt(b.created_at)} · {_book_action_links(b)}</div>'
            "</div>"
        )

    refresh_href = html_escape.escape(_with_token("/kindle", qtok), quote=True)
    opds_href = html_escape.escape(_with_token("/api/v1/opds", qtok), quote=True)

    html_doc = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>\n'
        "<title>InkPress · Kindle 直下页</title>\n"
        f"<style>{_KINDLE_EINK_CSS}</style>\n"
        "</head>\n<body>\n"
        '<div class="bar">'
        "<h1>InkPress · Kindle 直下页</h1>"
        f'<div class="meta">更新时间：{now_str} · <a href="{refresh_href}">刷新页面</a> · <a href="/">返回完整版网页</a></div>'
        "</div>\n"
        f"{banner_html}\n"
        + "\n".join(hero_parts)
        + "\n<h2>近期期刊归档</h2>\n"
        + ("\n".join(issues_rows) if issues_rows else '<p class="meta">暂无更多历史期刊。</p>')
        + "\n<h2>个人书架与网页剪藏</h2>\n"
        + ("\n".join(books_rows) if books_rows else '<p class="meta">书架暂无电子书。</p>')
        + '\n<div style="margin-top:24px;padding-top:12px;border-top:2px solid #000;" class="meta">'
        f'<div>KOReader / 文石 OPDS 1.2 订阅地址：<code>{opds_href}</code></div>'
        '<div style="margin-top:8px;">'
        '<form method="post" action="/kindle/logout"><button class="btn-sec" type="submit">退出登录</button></form>'
        "</div></div>\n"
        "</body>\n</html>"
    )
    return HTMLResponse(html_doc)
