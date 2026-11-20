from __future__ import annotations

import mimetypes
import uuid
import zipfile
from pathlib import Path
from typing import Any

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


def _book_dict(book: Book) -> dict[str, Any]:
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
        "metadata": book.metadata_json or {},
        "has_cover": bool(book.cover_path and Path(book.cover_path).exists()),
        "can_read_online": bool(
            book.kindle_epub_path and book.kindle_epub_path.lower().endswith(".epub")
        ),
        "scheduled_push_at": book.scheduled_push_at.isoformat() if book.scheduled_push_at else None,
        "pushed_count": book.pushed_count,
        "last_pushed_at": book.last_pushed_at.isoformat() if book.last_pushed_at else None,
        "created_at": book.created_at.isoformat(),
    }


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
    filename = file.filename or "untitled.epub"
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_BOOK_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的电子书格式 '.{ext}'，支持：{', '.join(sorted(SUPPORTED_BOOK_FORMATS))}",
        )

    settings = get_settings()
    settings.ensure_storage_dirs()
    uid = uuid.uuid4().hex[:12]
    safe_stem = Path(filename).stem[:100]
    original_path = settings.library_data_dir / "originals" / f"{safe_stem}_{uid}.{ext}"
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    original_path.write_bytes(data)

    book = Book(
        kind=kind if kind in ("uploaded_book", "magazine") else "uploaded_book",
        title=title.strip() or safe_stem,
        author=author.strip(),
        original_format=ext,
        original_path=str(original_path),
        file_size_bytes=len(data),
        conversion_status="converting",
    )
    session.add(book)
    await session.commit()
    await session.refresh(book)

    try:
        await convert_book_task.kiq(book.id)
    except Exception:
        background_tasks.add_task(run_convert_book_bg, book.id)

    return {"item": _book_dict(book)}


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
    media_type = "application/epub+zip" if ext == ".epub" else "application/pdf"
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
    chapters = list_epub_chapters(Path(book.kindle_epub_path))
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
    try:
        return read_epub_chapter_html(Path(book.kindle_epub_path), chapter_index, book.id)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail="Chapter index out of range") from exc


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
    clean_asset = asset_path.lstrip("/")
    with zipfile.ZipFile(epub_path, "r") as zf:
        if clean_asset not in zf.namelist():
            raise HTTPException(status_code=404, detail="Asset not found")
        content = zf.read(clean_asset)
    media_type, _ = mimetypes.guess_type(clean_asset)
    return Response(content=content, media_type=media_type or "application/octet-stream")


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
