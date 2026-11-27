from __future__ import annotations

import asyncio
import contextlib
import posixpath
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.api.deps import get_current_user
from inkpress.api.schemas import BookDeliverRequest, BookExportRequest, BookScheduleRequest
from inkpress.config import get_settings
from inkpress.db.base import get_db
from inkpress.db.models import Book, Delivery, User, utcnow
from inkpress.publishing.book_converter import (
    SUPPORTED_BOOK_FORMATS,
    build_companion_kindle_mobi,
    list_epub_chapters,
    read_epub_chapter_html,
)
from inkpress.worker.broker import has_live_worker
from inkpress.worker.tasks import (
    _locate_existing_book_mobi,
    convert_book_task,
    deliver_book_task,
    export_book_task,
    make_export_key,
    normalize_export_max_mb,
    run_convert_book_bg,
    run_deliver_book,
    run_deliver_book_bg,
    run_export_book,
    run_export_book_bg,
)

router = APIRouter(prefix="/api/v1/books", tags=["books"])

UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MB
MAX_BOOK_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_EPUB_ASSET_BYTES = 25 * 1024 * 1024  # 25 MB

ALLOWED_ASSET_MIME_BY_EXT: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


DOWNLOAD_MIME_BY_EXT: dict[str, str] = {
    ".epub": "application/epub+zip",
    ".pdf": "application/pdf",
    ".mobi": "application/x-mobipocket-ebook",
    ".azw3": "application/vnd.amazon.ebook",
    ".azw": "application/vnd.amazon.ebook",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".fb2": "application/x-fictionbook+xml",
    ".cbz": "application/vnd.comicbook+zip",
    ".rtf": "application/rtf",
}


def _sanitize_public_exports(raw_exports: Any) -> dict[str, dict[str, Any]]:
    public_exports: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_exports, dict):
        return public_exports
    for exp_key, exp_val in raw_exports.items():
        if not isinstance(exp_val, dict):
            continue
        p_str = str(exp_val.get("path") or "")
        st = str(exp_val.get("status") or "")
        if st == "ready" and (not p_str or not Path(p_str).exists()):
            continue
        public_exports[str(exp_key)] = {
            "key": str(exp_key),
            "target_format": str(exp_val.get("target_format") or ""),
            "max_mb": exp_val.get("max_mb"),
            "status": st,
            "size_bytes": int(exp_val.get("size_bytes") or 0),
            "notes": str(exp_val.get("notes") or ""),
            "error": exp_val.get("error"),
            "updated_at": exp_val.get("updated_at"),
        }
    return public_exports


def _book_dict(book: Book) -> dict[str, Any]:
    meta = dict(book.metadata_json or {})
    # Avoid shipping large chapter-index caches or internal filesystem paths in library items
    meta.pop("chapters_cache", None)
    meta.pop("chapters_cache_sig", None)
    raw_exports = meta.pop("exports", None)
    public_exports = _sanitize_public_exports(raw_exports)

    active_fmt = (
        Path(book.kindle_epub_path).suffix.lower().lstrip(".")
        if book.kindle_epub_path
        else ""
    ) or book.original_format.lower()
    orig_p = Path(book.original_path) if book.original_path else None
    has_orig = bool(orig_p is not None and orig_p.exists())
    orig_size = orig_p.stat().st_size if (orig_p is not None and has_orig) else book.file_size_bytes

    return {
        "id": book.id,
        "kind": book.kind,
        "edition_id": book.edition_id,
        "title": book.title,
        "author": book.author,
        "description": book.description,
        "language": book.language,
        "original_format": book.original_format,
        "active_format": active_fmt,
        "has_original": has_orig,
        "original_size_bytes": orig_size,
        "file_size_bytes": book.file_size_bytes,
        "conversion_status": book.conversion_status,
        "conversion_error": book.conversion_error,
        "metadata": meta,
        "exports": public_exports,
        "has_cover": bool(book.cover_path and Path(book.cover_path).exists()),
        "can_read_online": bool(
            book.kindle_epub_path and book.kindle_epub_path.lower().endswith(".epub")
        ),
        "scheduled_push_at": book.scheduled_push_at.isoformat() if book.scheduled_push_at else None,
        "pushed_count": book.pushed_count,
        "last_pushed_at": book.last_pushed_at.isoformat() if book.last_pushed_at else None,
        "created_at": book.created_at.isoformat(),
    }


def _safe_filename_stem(filename: str) -> str:
    raw_stem = Path(filename).stem
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", raw_stem).strip("._")
    return cleaned[:100] or "book"


def _safe_download_filename(title: str, ext: str, suffix_tag: str = "") -> str:
    clean_title = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "_", title).strip(" ._")[:110] or "book"
    norm_ext = ext if ext.startswith(".") else f".{ext}"
    return f"{clean_title}{suffix_tag}{norm_ext}"


def _read_epub_zip_asset(epub_path: Path, normalized_asset: str) -> bytes:
    with zipfile.ZipFile(epub_path, "r") as zf:
        try:
            info = zf.getinfo(normalized_asset)
        except KeyError as exc:
            raise FileNotFoundError(normalized_asset) from exc
        if info.file_size > MAX_EPUB_ASSET_BYTES:
            raise ValueError("EPUB asset exceeds 25 MB limit")
        return zf.read(info)


@router.get("")
async def list_books(
    kind: str | None = None,
    limit: int = 60,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Book).order_by(desc(Book.created_at), desc(Book.id)).limit(min(limit, 200))
    if kind:
        stmt = stmt.where(Book.kind == kind)
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [_book_dict(b) for b in rows]}


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_book(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    author: str = Form(default=""),
    kind: str = Form(default="uploaded_book"),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    filename = Path(file.filename or "untitled.epub").name
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_BOOK_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的电子书格式 '.{ext}'，支持：{', '.join(sorted(SUPPORTED_BOOK_FORMATS))}",
        )

    settings = get_settings()
    settings.ensure_storage_dirs()
    uid = uuid.uuid4().hex[:12]
    safe_stem = _safe_filename_stem(filename)
    original_path = settings.library_data_dir / "originals" / f"{safe_stem}_{uid}.{ext}"

    total_bytes = 0
    try:
        with original_path.open("wb") as out:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_BOOK_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="上传文件超过 100 MB 上限",
                    )
                out.write(chunk)
    except Exception:
        original_path.unlink(missing_ok=True)
        raise

    if total_bytes == 0:
        original_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传文件为空")

    custom_title = title.strip()
    display_title = custom_title or Path(filename).stem[:100] or safe_stem
    book = Book(
        kind=kind if kind in ("uploaded_book", "magazine") else "uploaded_book",
        title=display_title,
        author=author.strip(),
        original_format=ext,
        original_path=str(original_path),
        file_size_bytes=total_bytes,
        conversion_status="converting",
        metadata_json={"has_custom_title": bool(custom_title)},
    )
    session.add(book)
    await session.commit()
    await session.refresh(book)

    try:
        await convert_book_task.kiq(book.id)
    except Exception:
        background_tasks.add_task(run_convert_book_bg, book.id)

    return {"item": _book_dict(book)}


@router.post("/{book_id}/reconvert")
async def reconvert_book(
    book_id: int,
    background_tasks: BackgroundTasks,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    if not book.original_path or not Path(book.original_path).exists():
        raise HTTPException(status_code=400, detail="原始图书文件不存在，无法重新转码")

    lib_root = get_settings().library_data_dir.resolve()
    meta = dict(book.metadata_json or {})
    old_exports = meta.pop("exports", None)
    if isinstance(old_exports, dict):
        orig_resolved = Path(book.original_path).resolve()
        for exp_val in old_exports.values():
            if not isinstance(exp_val, dict):
                continue
            p_str = str(exp_val.get("path") or "").strip()
            if p_str:
                exp_p = Path(p_str)
                if (
                    exp_p.exists()
                    and exp_p.resolve() != orig_resolved
                    and exp_p.resolve().is_relative_to(lib_root)
                ):
                    exp_p.unlink(missing_ok=True)

    book.conversion_status = "converting"
    book.conversion_error = None
    meta.pop("chapters_cache", None)
    meta.pop("chapters_cache_sig", None)
    book.metadata_json = meta
    await session.commit()
    await session.refresh(book)

    try:
        await convert_book_task.kiq(book.id)
        queue_status = "queued"
    except Exception:
        background_tasks.add_task(run_convert_book_bg, book.id)
        queue_status = "queued_local"

    return {"status": queue_status, "item": _book_dict(book)}


@router.get("/{book_id}/cover")
async def get_book_cover(
    book_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    book = await session.get(Book, book_id)
    if book is None or not book.cover_path or not Path(book.cover_path).exists():
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(book.cover_path, media_type="image/jpeg")


@router.get("/{book_id}/download")
async def download_book(
    book_id: int,
    variant: str = Query(default="", max_length=24),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book file not ready")

    if variant.strip().lower() == "original":
        if not book.original_path:
            raise HTTPException(status_code=404, detail="原始上传文件不存在")
        orig_p = Path(book.original_path)
        if not orig_p.exists():
            raise HTTPException(status_code=404, detail="原始上传文件已不存在")
        orig_ext = orig_p.suffix.lower() or f".{book.original_format.lower()}"
        media_type = DOWNLOAD_MIME_BY_EXT.get(orig_ext, "application/octet-stream")
        return FileResponse(
            orig_p,
            media_type=media_type,
            filename=_safe_download_filename(book.title, orig_ext, "_原文件"),
        )

    if not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book file not ready")
    path = Path(book.kindle_epub_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Book file missing on disk")
    ext = (path.suffix or ".epub").lower()
    media_type = DOWNLOAD_MIME_BY_EXT.get(ext, "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        filename=_safe_download_filename(book.title, ext),
    )


def _is_recent_converting_export(entry: dict[str, Any], max_age_seconds: float = 600.0) -> bool:
    if entry.get("status") != "converting":
        return False
    raw_ts = str(entry.get("updated_at") or "").strip()
    if not raw_ts:
        return False
    try:
        parsed = datetime.fromisoformat(raw_ts)
        age = (utcnow() - parsed).total_seconds()
        return age < max_age_seconds
    except Exception:
        return False


@router.post("/{book_id}/export")
async def create_book_export(
    book_id: int,
    payload: BookExportRequest,
    background_tasks: BackgroundTasks,
    inline: bool = False,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    if book.conversion_status != "ready":
        raise HTTPException(status_code=400, detail="图书尚未转换就绪，请稍后再试")

    fmt = payload.target_format.strip().lower()
    norm_mb = normalize_export_max_mb(fmt, payload.max_mb)
    export_key = make_export_key(fmt, norm_mb)
    download_url = f"/api/v1/books/{book.id}/exports/{export_key}/download"
    settings = get_settings()
    settings.ensure_storage_dirs()
    lib_root = settings.library_data_dir.resolve()

    meta = dict(book.metadata_json or {})
    exports_map = dict(meta.get("exports") or {})
    existing_entry = exports_map.get(export_key)

    # 1. Instant cache hit when exact (format, max_mb) artifact already exists on disk
    if isinstance(existing_entry, dict) and existing_entry.get("status") == "ready":
        cached_p = Path(str(existing_entry.get("path") or ""))
        if cached_p.exists() and cached_p.stat().st_size > 0 and cached_p.resolve().is_relative_to(lib_root):
            if payload.set_as_default and fmt in ("epub", "pdf"):
                uid = uuid.uuid4().hex[:10]
                safe_stem = _safe_filename_stem(book.title)
                promoted = settings.library_data_dir / "epubs" / f"{safe_stem}_{uid}.{fmt}"
                promoted.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copy2, cached_p, promoted)
                mobi_out = await build_companion_kindle_mobi(promoted) if fmt == "epub" else None

                old_primary = Path(book.kindle_epub_path) if book.kindle_epub_path else None
                old_orig = Path(book.original_path).resolve() if book.original_path else None
                book.kindle_epub_path = str(promoted)
                book.file_size_bytes = promoted.stat().st_size
                meta.pop("chapters_cache", None)
                meta.pop("chapters_cache_sig", None)
                if mobi_out is not None:
                    meta["kindle_mobi_path"] = str(mobi_out)
                elif fmt == "pdf":
                    meta.pop("kindle_mobi_path", None)
                book.metadata_json = meta
                await session.commit()
                await session.refresh(book)

                if (
                    old_primary is not None
                    and old_primary.exists()
                    and old_primary.resolve() != promoted.resolve()
                    and (old_orig is None or old_primary.resolve() != old_orig)
                    and old_primary.resolve().is_relative_to((settings.library_data_dir / "epubs").resolve())
                ):
                    with contextlib.suppress(OSError):
                        old_primary.unlink(missing_ok=True)

            item_dict = _book_dict(book)
            return {
                "status": "ready",
                "export_key": export_key,
                "export": item_dict["exports"].get(export_key),
                "download_url": download_url,
                "item": item_dict,
            }

    # 2. Deduplicate in-flight conversion for the same export_key
    if not inline and isinstance(existing_entry, dict) and _is_recent_converting_export(existing_entry):
        item_dict = _book_dict(book)
        return {
            "status": "converting",
            "export_key": export_key,
            "export": item_dict["exports"].get(export_key),
            "download_url": download_url,
            "item": item_dict,
        }

    # 3. Validate Calibre availability upfront when exporting uncached MOBI
    if fmt == "mobi":
        existing_mobi = _locate_existing_book_mobi(book, lib_root)
        max_b = int(norm_mb * 1024 * 1024) if norm_mb is not None else None
        can_reuse_mobi = (
            existing_mobi is not None
            and existing_mobi.stat().st_size > 0
            and (max_b is None or existing_mobi.stat().st_size <= max_b)
        )
        if not can_reuse_mobi:
            local_calibre = shutil.which("ebook-convert") is not None
            worker_calibre = False if inline else await has_live_worker(require_calibre=True)
            if not local_calibre and not worker_calibre:
                raise HTTPException(
                    status_code=400,
                    detail="当前运行环境未启用 Calibre (ebook-convert) 组件，暂不支持生成 MOBI 格式；请选择转换为 EPUB 或轻量化 PDF。",
                )

    # 4. Inline execution (also fast-paths existing EPUB/MOBI copies)
    primary_p = Path(book.kindle_epub_path) if book.kindle_epub_path else Path("")
    is_instant_copy = (
        fmt == "epub"
        and primary_p.suffix.lower() == ".epub"
        and primary_p.exists()
        and (norm_mb is None or primary_p.stat().st_size <= int(norm_mb * 1024 * 1024))
    )
    if inline or is_instant_copy:
        try:
            await run_export_book(
                book.id,
                target_format=fmt,
                max_mb=norm_mb,
                set_as_default=payload.set_as_default,
            )
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.refresh(book)
        item_dict = _book_dict(book)
        return {
            "status": "ready",
            "export_key": export_key,
            "export": item_dict["exports"].get(export_key),
            "download_url": download_url,
            "item": item_dict,
        }

    # 5. Async worker execution (with automatic fallback to local BackgroundTasks when no worker is live)
    exports_map[export_key] = {
        "target_format": fmt,
        "max_mb": norm_mb,
        "status": "converting",
        "path": "",
        "size_bytes": 0,
        "notes": "",
        "error": None,
        "updated_at": utcnow().isoformat(),
    }
    meta["exports"] = exports_map
    book.metadata_json = meta
    await session.commit()
    await session.refresh(book)

    dispatched_to_worker = False
    if await has_live_worker(require_calibre=(fmt == "mobi")):
        try:
            await export_book_task.kiq(
                book.id,
                target_format=fmt,
                max_mb=norm_mb,
                set_as_default=payload.set_as_default,
            )
            dispatched_to_worker = True
        except Exception:
            dispatched_to_worker = False

    if not dispatched_to_worker:
        background_tasks.add_task(
            run_export_book_bg,
            book.id,
            target_format=fmt,
            max_mb=norm_mb,
            set_as_default=payload.set_as_default,
        )

    item_dict = _book_dict(book)
    return {
        "status": "converting",
        "export_key": export_key,
        "export": item_dict["exports"].get(export_key),
        "download_url": download_url,
        "item": item_dict,
    }


@router.get("/{book_id}/exports/{export_key}")
async def get_book_export_status(
    book_id: int,
    export_key: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not re.fullmatch(r"^[a-z0-9_]{2,40}$", export_key):
        raise HTTPException(status_code=400, detail="Invalid export key")
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    raw_exports = (book.metadata_json or {}).get("exports")
    raw_entry = raw_exports.get(export_key) if isinstance(raw_exports, dict) else None
    if not isinstance(raw_entry, dict):
        raise HTTPException(status_code=404, detail="Export job not found")

    st = str(raw_entry.get("status") or "")
    p_str = str(raw_entry.get("path") or "")
    if st == "ready" and (not p_str or not Path(p_str).exists()):
        st = "failed"
    item_dict = _book_dict(book)
    return {
        "status": st,
        "export_key": export_key,
        "export": {
            "key": export_key,
            "target_format": str(raw_entry.get("target_format") or ""),
            "max_mb": raw_entry.get("max_mb"),
            "status": st,
            "size_bytes": int(raw_entry.get("size_bytes") or 0),
            "notes": str(raw_entry.get("notes") or ""),
            "error": raw_entry.get("error"),
            "updated_at": raw_entry.get("updated_at"),
        },
        "download_url": f"/api/v1/books/{book.id}/exports/{export_key}/download",
        "item": item_dict,
    }


@router.get("/{book_id}/exports/{export_key}/download")
async def download_book_export(
    book_id: int,
    export_key: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    if not re.fullmatch(r"^[a-z0-9_]{2,40}$", export_key):
        raise HTTPException(status_code=400, detail="Invalid export key")
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    raw_exports = (book.metadata_json or {}).get("exports")
    entry = raw_exports.get(export_key) if isinstance(raw_exports, dict) else None
    if not isinstance(entry, dict) or entry.get("status") != "ready":
        raise HTTPException(status_code=404, detail="转换文件尚未就绪")

    lib_root = get_settings().library_data_dir.resolve()
    exp_path = Path(str(entry.get("path") or ""))
    if not exp_path.exists() or not exp_path.resolve().is_relative_to(lib_root):
        raise HTTPException(status_code=404, detail="转换文件已不存在，请重新转换")

    ext = (exp_path.suffix or f".{entry.get('target_format', 'epub')}").lower()
    media_type = DOWNLOAD_MIME_BY_EXT.get(ext, "application/octet-stream")
    max_mb = entry.get("max_mb")
    tag = ""
    if isinstance(max_mb, (int, float)) and ext in (".pdf", ".epub"):
        mb_str = str(int(max_mb)) if float(max_mb).is_integer() else str(max_mb)
        tag = f"_{mb_str}MB"
    return FileResponse(
        exp_path,
        media_type=media_type,
        filename=_safe_download_filename(book.title, ext, tag),
    )


@router.get("/{book_id}/chapters")
async def get_book_chapters(
    book_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    book = await session.get(Book, book_id)
    if book is None or not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book not found")
    epub_path = Path(book.kindle_epub_path)
    if not epub_path.exists():
        raise HTTPException(status_code=404, detail="Book file missing on disk")
    if epub_path.suffix.lower() != ".epub":
        raise HTTPException(status_code=400, detail="非 EPUB 格式暂不支持在线分章阅读")

    stat = epub_path.stat()
    stat_sig = f"{epub_path}:{stat.st_mtime_ns}:{stat.st_size}"
    meta = dict(book.metadata_json or {})
    cached = meta.get("chapters_cache")
    if isinstance(cached, list) and meta.get("chapters_cache_sig") == stat_sig:
        chapters = cached
    else:
        try:
            chapters = await asyncio.to_thread(list_epub_chapters, epub_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Book file missing on disk") from exc
        except (ValueError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=f"无效的 EPUB 文件: {exc}") from exc
        meta["chapters_cache"] = chapters
        meta["chapters_cache_sig"] = stat_sig
        book.metadata_json = meta
        await session.commit()

    return {"book": _book_dict(book), "chapters": chapters}


@router.get("/{book_id}/chapters/{chapter_index}")
async def get_book_chapter_content(
    book_id: int,
    chapter_index: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    book = await session.get(Book, book_id)
    if book is None or not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book not found")
    epub_path = Path(book.kindle_epub_path)
    if not epub_path.exists():
        raise HTTPException(status_code=404, detail="Book file missing on disk")
    if epub_path.suffix.lower() != ".epub":
        raise HTTPException(status_code=400, detail="非 EPUB 格式暂不支持在线分章阅读")
    try:
        return await asyncio.to_thread(read_epub_chapter_html, epub_path, chapter_index, book.id)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail="Chapter index out of range") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Book file missing on disk") from exc
    except (ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=f"无法读取 EPUB 章节: {exc}") from exc


@router.get("/{book_id}/asset/{asset_path:path}")
async def get_book_epub_asset(
    book_id: int,
    asset_path: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Response:
    book = await session.get(Book, book_id)
    if book is None or not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book not found")
    epub_path = Path(book.kindle_epub_path)
    if not epub_path.exists():
        raise HTTPException(status_code=404, detail="Book file missing")
    if epub_path.suffix.lower() != ".epub":
        raise HTTPException(status_code=400, detail="Book file is not an EPUB archive")

    decoded = unquote(asset_path).replace("\\", "/")
    normalized = posixpath.normpath(decoded).lstrip("/")
    if not normalized or normalized == "." or normalized.startswith(".."):
        raise HTTPException(status_code=400, detail="Invalid asset path")

    ext = Path(normalized).suffix.lower()
    media_type = ALLOWED_ASSET_MIME_BY_EXT.get(ext)
    if media_type is None:
        raise HTTPException(
            status_code=403,
            detail=f"Disallowed EPUB asset type '{ext or 'unknown'}'",
        )

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


@router.post("/{book_id}/deliver")
async def deliver_book_now(
    book_id: int,
    payload: BookDeliverRequest,
    background_tasks: BackgroundTasks,
    inline: bool = False,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    if book.conversion_status != "ready":
        raise HTTPException(status_code=400, detail="图书尚未转换完成")
    if inline:
        res = await run_deliver_book(book.id, target_email=payload.target_email, trigger="manual")
        return {"status": "completed", "result": res}
    try:
        await deliver_book_task.kiq(book.id, target_email=payload.target_email, trigger="manual")
        return {"status": "queued", "book_id": book.id}
    except Exception:
        background_tasks.add_task(
            run_deliver_book_bg, book.id, target_email=payload.target_email, trigger="manual"
        )
        return {"status": "queued_local", "book_id": book.id}


@router.patch("/{book_id}/schedule")
async def schedule_book_push(
    book_id: int,
    payload: BookScheduleRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    book.scheduled_push_at = payload.scheduled_push_at
    await session.commit()
    await session.refresh(book)
    return {"item": _book_dict(book)}


@router.delete("/{book_id}")
async def delete_book(
    book_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    book = await session.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    lib_root = get_settings().library_data_dir.resolve()
    epub_parent = (
        Path(book.kindle_epub_path).resolve().parent if book.kindle_epub_path else None
    )
    meta = book.metadata_json if isinstance(book.metadata_json, dict) else {}
    raw_extra = list(meta.get("volume_paths") or [])
    raw_exps = meta.get("exports")
    if isinstance(raw_exps, dict):
        for exp_val in raw_exps.values():
            if isinstance(exp_val, dict) and str(exp_val.get("path") or "").strip():
                raw_extra.append(str(exp_val["path"]))
    if meta.get("kindle_mobi_path"):
        raw_extra.append(meta["kindle_mobi_path"])
    if book.kindle_epub_path and book.kindle_epub_path.lower().endswith(".epub"):
        raw_extra.append(str(Path(book.kindle_epub_path).with_suffix(".mobi")))
    extra_vol_paths = [
        str(p)
        for p in raw_extra
        if str(p).strip()
        and (
            Path(str(p)).resolve().is_relative_to(lib_root)
            or (epub_parent is not None and Path(str(p)).resolve().parent == epub_parent)
        )
    ]
    for p_str in {book.original_path, book.kindle_epub_path, book.cover_path or "", *extra_vol_paths}:
        if p_str:
            p = Path(p_str)
            if p.exists():
                p.unlink(missing_ok=True)
    await session.execute(
        update(Delivery).where(Delivery.book_id == book.id).values(book_id=None)
    )
    await session.delete(book)
    await session.commit()
    return {"status": "deleted"}
