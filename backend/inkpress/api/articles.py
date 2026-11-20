from __future__ import annotations

import html as html_escape
import uuid
from datetime import UTC, datetime
from typing import Any

import markdown as md_lib
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.ai.client import AIClient
from inkpress.ai.editorial import summarize_article, translate_article_bilingual
from inkpress.api.deps import get_current_user
from inkpress.api.schemas import ArticlePatch, ClipTextRequest, ClipUrlRequest
from inkpress.config import get_settings
from inkpress.db.base import get_db
from inkpress.db.models import Article, Book, Delivery, User, utcnow
from inkpress.db.search import search_articles_fulltext
from inkpress.delivery.smtp import parse_recipients, send_ebook_via_smtp
from inkpress.ingestion.extractor import (
    canonicalize_url,
    compute_content_hash,
    detect_language,
    estimate_word_count_and_time,
    extract_from_webpage,
    sanitize_article_html,
)
from inkpress.ingestion.fetcher import assert_safe_public_url, smart_fetch_text
from inkpress.publishing.epub_builder import EpubChapterInput, build_kindle_edition_epubs
from inkpress.settings_store import get_ai_config, get_network_config, get_smtp_config

router = APIRouter(prefix="/api/v1/articles", tags=["articles"])


def _article_summary_dict(art: Article) -> dict[str, Any]:
    excerpt = (art.ai_summary or art.plain_text[:220]).replace("\n", " ").strip()
    return {
        "id": art.id,
        "source_id": art.source_id,
        "source_name": art.source_name,
        "category": art.category,
        "canonical_url": art.canonical_url,
        "title": art.title,
        "translated_title": art.translated_title,
        "author": art.author,
        "language": art.language,
        "published_at": art.published_at.isoformat(),
        "fetched_at": art.fetched_at.isoformat(),
        "lead_image_url": art.lead_image_url,
        "excerpt": excerpt,
        "ai_summary": art.ai_summary,
        "has_bilingual": bool(art.bilingual_html),
        "word_count": art.word_count,
        "reading_time_min": art.reading_time_min,
        "is_read": art.is_read,
        "is_starred": art.is_starred,
    }


def _article_detail_dict(art: Article) -> dict[str, Any]:
    base = _article_summary_dict(art)
    base["clean_html"] = art.clean_html
    base["bilingual_html"] = art.bilingual_html
    base["plain_text"] = art.plain_text
    return base


async def _push_single_article_to_kindle(
    session: AsyncSession,
    article: Article,
    target_email: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    settings.ensure_storage_dirs()
    smtp_cfg = await get_smtp_config(session)
    net_cfg = await get_network_config(session)

    raw_targets = target_email.strip() or str(smtp_cfg.get("default_kindle_email") or "")
    recipients = parse_recipients(raw_targets)
    if not recipients:
        raise HTTPException(status_code=400, detail="请先在设置中填写 Kindle 收件邮箱")

    now = datetime.now(UTC)
    vols = await build_kindle_edition_epubs(
        output_dir=settings.library_data_dir / "epubs",
        edition_title=article.translated_title or article.title,
        subtitle=article.source_name or "Web Clip",
        issue_number=1,
        issue_date=now,
        chapters_input=[
            EpubChapterInput(
                title=article.translated_title or article.title,
                source_name=article.source_name or "剪藏",
                category=article.category,
                author=article.author,
                published_at=article.published_at,
                canonical_url=article.canonical_url,
                body_html=article.clean_html,
                ai_summary=article.ai_summary,
                bilingual_html=article.bilingual_html,
                language=article.language,
            )
        ],
        strip_links=True,
        outbound_proxy=net_cfg["outbound_proxy"],
    )
    primary_vol = vols[0]
    total_size = sum(v.size_bytes for v in vols)
    cover_file = settings.library_data_dir / "covers" / f"{primary_vol.path.stem}.jpg"
    cover_file.write_bytes(primary_vol.cover_bytes)

    book = Book(
        kind="web_clip",
        title=article.translated_title or article.title,
        author=article.author or article.source_name or "剪藏",
        description=article.canonical_url,
        language=article.language,
        original_format="epub",
        original_path=str(primary_vol.path),
        kindle_epub_path=str(primary_vol.path),
        cover_path=str(cover_file),
        file_size_bytes=total_size,
        conversion_status="ready",
        metadata_json={"volume_count": len(vols)},
    )
    session.add(book)
    await session.flush()

    delivery = Delivery(
        book_id=book.id,
        title=book.title,
        trigger="manual_clip",
        target_email=", ".join(recipients),
        status="running",
        article_count=1,
        volume_count=len(vols),
        file_size_bytes=total_size,
    )
    session.add(delivery)
    await session.commit()

    try:
        for vol in vols:
            subject = f"InkPress · {vol.title}" if len(vols) > 1 else f"InkPress · {book.title}"
            await send_ebook_via_smtp(
                smtp_config=smtp_cfg,
                recipients=recipients,
                subject=subject,
                body_text=f"Clipped and sent by InkPress: {article.canonical_url}",
                attachments=[vol.path],
            )
        book.pushed_count = 1
        book.last_pushed_at = utcnow()
        delivery.status = "sent"
        delivery.finished_at = utcnow()
        await session.commit()
    except Exception as exc:
        delivery.status = "failed"
        delivery.error_message = str(exc)[:2000]
        delivery.finished_at = utcnow()
        await session.commit()
        raise HTTPException(status_code=502, detail=f"SMTP 发送失败: {exc}") from exc

    return {
        "status": "sent",
        "delivery_id": delivery.id,
        "book_id": book.id,
        "volume_count": len(vols),
    }


@router.get("")
async def list_articles(
    q: str = "",
    category: str | None = None,
    source_id: int | None = None,
    starred: bool = False,
    unread_only: bool = False,
    limit: int = 40,
    offset: int = 0,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    articles, total = await search_articles_fulltext(
        session,
        q,
        category=category or None,
        source_id=source_id,
        starred_only=starred,
        unread_only=unread_only,
        limit=min(limit, 100),
        offset=max(offset, 0),
    )
    cat_rows = (
        await session.execute(
            select(Article.category, func.count(Article.id))
            .group_by(Article.category)
            .order_by(func.count(Article.id).desc())
        )
    ).all()
    return {
        "items": [_article_summary_dict(a) for a in articles],
        "categories": [{"name": c, "count": int(cnt)} for c, cnt in cat_rows],
        "total": total,
    }


@router.get("/{article_id}")
async def get_article(
    article_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    art = await session.get(Article, article_id)
    if art is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"item": _article_detail_dict(art)}


@router.patch("/{article_id}")
async def patch_article(
    article_id: int,
    payload: ArticlePatch,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    art = await session.get(Article, article_id)
    if art is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if payload.is_read is not None:
        art.is_read = payload.is_read
    if payload.is_starred is not None:
        art.is_starred = payload.is_starred
    await session.commit()
    await session.refresh(art)
    return {"item": _article_detail_dict(art)}


@router.post("/{article_id}/ai-enrich")
async def ai_enrich_article(
    article_id: int,
    mode: str = "summary",  # summary | bilingual
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    art = await session.get(Article, article_id)
    if art is None:
        raise HTTPException(status_code=404, detail="Article not found")
    ai_cfg = await get_ai_config(session)
    net_cfg = await get_network_config(session)
    if not ai_cfg.get("api_key"):
        raise HTTPException(status_code=400, detail="请先在工作室设置中配置 AI API Key")

    ai = AIClient(
        base_url=str(ai_cfg["base_url"]),
        api_key=str(ai_cfg["api_key"]),
        model=str(ai_cfg["model"]),
        outbound_proxy=net_cfg["outbound_proxy"],
        use_proxy=bool(ai_cfg.get("use_proxy", False)),
        max_concurrency=int(ai_cfg.get("max_concurrency", 3)),
        timeout_seconds=int(ai_cfg.get("timeout_seconds", 60)),
    )
    try:
        if mode == "bilingual":
            try:
                tr_title, bi_html = await translate_article_bilingual(ai, art)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"AI 翻译调用失败: {exc}") from exc
            if not tr_title and not bi_html:
                raise HTTPException(
                    status_code=422,
                    detail="AI 未生成有效双语译文（文章可能已是中文或正文内容不足）",
                )
            if tr_title:
                art.translated_title = tr_title[:500]
            if bi_html:
                art.bilingual_html = bi_html
        else:
            try:
                summary = await summarize_article(ai, art)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"AI 摘要调用失败: {exc}") from exc
            if not summary or not summary.strip():
                raise HTTPException(
                    status_code=422,
                    detail="AI 未生成有效摘要（文章内容可能过短）",
                )
            art.ai_summary = summary.strip()
        await session.commit()
        await session.refresh(art)
    finally:
        await ai.close()
    return {"item": _article_detail_dict(art)}


@router.post("/{article_id}/push")
async def push_article_now(
    article_id: int,
    target_email: str = "",
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    art = await session.get(Article, article_id)
    if art is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return await _push_single_article_to_kindle(session, art, target_email=target_email)


@router.post("/clip-url")
async def clip_url(
    payload: ClipUrlRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    target_url = payload.url.strip()
    try:
        await assert_safe_public_url(target_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"不允许抓取该地址: {exc}") from exc

    net = await get_network_config(session)
    try:
        raw_html, final_url = await smart_fetch_text(
            target_url,
            use_proxy=payload.use_proxy,
            outbound_proxy=net["outbound_proxy"],
            byparr_base_url=net["byparr_base_url"],
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"抓取网页失败: {exc}") from exc

    extracted = extract_from_webpage(raw_html, final_url, fallback_title=payload.title.strip())
    canon = canonicalize_url(final_url)
    existing = (
        await session.execute(select(Article).where(Article.canonical_url == canon))
    ).scalar_one_or_none()
    if existing is None:
        art = Article(
            canonical_url=canon,
            content_hash=compute_content_hash(extracted.title, extracted.plain_text),
            title=payload.title.strip() or extracted.title,
            author=extracted.author,
            source_name="网页剪藏",
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
    if payload.push_to_kindle:
        push_result = await _push_single_article_to_kindle(
            session, art, target_email=payload.target_email
        )
    return {"item": _article_detail_dict(art), "delivery": push_result}


@router.post("/clip-text")
async def clip_text(
    payload: ClipTextRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if payload.content_format == "markdown":
        raw_html = md_lib.markdown(payload.content, extensions=["tables", "fenced_code"])
    elif payload.content_format == "html":
        raw_html = payload.content
    else:
        raw_html = "\n".join(
            f"<p>{html_escape.escape(line.strip())}</p>"
            for line in payload.content.splitlines()
            if line.strip()
        )
    clean_html, plain_text, lead_img = sanitize_article_html(raw_html)
    words, mins = estimate_word_count_and_time(plain_text)
    art = Article(
        canonical_url=f"https://inkpress.local/clip/{uuid.uuid4().hex}",
        content_hash=compute_content_hash(payload.title, plain_text),
        title=payload.title.strip(),
        author=payload.author.strip() or "手动剪藏",
        source_name="文本剪藏",
        category="剪藏",
        language=detect_language(f"{payload.title} {plain_text}"),
        published_at=utcnow(),
        fetched_at=utcnow(),
        lead_image_url=lead_img,
        clean_html=clean_html,
        plain_text=plain_text,
        word_count=words,
        reading_time_min=mins,
        is_starred=True,
    )
    session.add(art)
    await session.commit()
    await session.refresh(art)

    push_result = None
    if payload.push_to_kindle:
        push_result = await _push_single_article_to_kindle(
            session, art, target_email=payload.target_email
        )
    return {"item": _article_detail_dict(art), "delivery": push_result}
