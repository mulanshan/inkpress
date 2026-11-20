from __future__ import annotations

import asyncio
import posixpath
import re
import uuid
import zipfile
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
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.api.deps import get_current_user
from inkpress.api.schemas import BookDeliverRequest, BookScheduleRequest
from inkpress.config import get_settings
from inkpress.db.base import get_db
from inkpress.db.models import Book, User
from inkpress.publishing.book_converter import (
    SUPPORTED_BOOK_FORMATS,
    list_epub_chapters,
    read_epub_chapter_html,
)
from inkpress.worker.tasks import (
    convert_book_task,
    deliver_book_task,
    run_convert_book_bg,
    run_deliver_book,
    run_deliver_book_bg,
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


def _book_dict(book: Book) -> dict[str, Any]:
    meta = dict(book.metadata_json or {})
    # Avoid shipping large chapter-index caches in every library list item
    meta.pop("chapters_cache", None)
    meta.pop("chapters_cache_sig", None)
    return {
        "id": book.id,
        "kind": book.kind,
        "edition_id": book.edition_id,
        "title": book.title,
        "author": book.author,
        "description": book.description,
        "language": book.language,
        "original_format": book.original_format,
        "file_size_bytes": book.file_size_bytes,
        "conversion_status": book.conversion_status,
        "conversion_error": book.conversion_error,
        "metadata": meta,
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

    book.conversion_status = "converting"
    book.conversion_error = None
    meta = dict(book.metadata_json or {})
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
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    book = await session.get(Book, book_id)
    if book is None or not book.kindle_epub_path:
        raise HTTPException(status_code=404, detail="Book file not ready")
    path = Path(book.kindle_epub_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Book file missing on disk")
    ext = path.suffix or ".epub"
    media_type = "application/epub+zip" if ext.lower() == ".epub" else "application/pdf"
    return FileResponse(path, media_type=media_type, filename=f"{book.title}{ext}")


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
    for p_str in (book.original_path, book.kindle_epub_path, book.cover_path):
        if p_str:
            p = Path(p_str)
            if p.exists():
                p.unlink(missing_ok=True)
    await session.delete(book)
    await session.commit()
    return {"status": "deleted"}
