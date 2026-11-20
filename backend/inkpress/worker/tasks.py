from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from inkpress.ai.client import AIClient
from inkpress.ai.editorial import enrich_articles_with_ai, generate_frontpage_briefing
from inkpress.config import get_settings
from inkpress.db.base import get_session_factory
from inkpress.db.models import (
    Article,
    ArticleDelivery,
    Book,
    Delivery,
    Edition,
    EditionSource,
    Source,
    utcnow,
)
from inkpress.delivery.smtp import parse_recipients, send_ebook_via_smtp
from inkpress.ingestion.service import sync_source
from inkpress.logging import get_logger
from inkpress.publishing.book_converter import convert_book_to_kindle_epub
from inkpress.publishing.epub_builder import EpubChapterInput, build_kindle_edition_epubs
from inkpress.settings_store import get_ai_config, get_network_config, get_smtp_config
from inkpress.worker.broker import enqueue_job, publish_event

logger = get_logger(__name__)


async def _make_ai_client(ai_cfg: dict[str, Any], outbound_proxy: str) -> AIClient | None:
    if not ai_cfg.get("enabled") or not ai_cfg.get("api_key"):
        return None
    return AIClient(
        base_url=str(ai_cfg["base_url"]),
        api_key=str(ai_cfg["api_key"]),
        model=str(ai_cfg["model"]),
        outbound_proxy=outbound_proxy,
        use_proxy=bool(ai_cfg.get("use_proxy", False)),
        max_concurrency=int(ai_cfg.get("max_concurrency", 3)),
        timeout_seconds=int(ai_cfg.get("timeout_seconds", 60)),
    )


async def run_sync_source(source_id: int) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return {"error": "source_not_found", "source_id": source_id}
        await publish_event("source.sync.started", {"source_id": source.id, "name": source.name})
        try:
            stats = await sync_source(session, source)
            await publish_event(
                "source.sync.completed",
                {"source_id": source.id, "name": source.name, **stats},
            )
            return {"source_id": source.id, **stats}
        except Exception as exc:
            await publish_event(
                "source.sync.failed",
                {"source_id": source.id, "name": source.name, "error": str(exc)},
            )
            raise


async def run_build_and_deliver_edition(
    edition_id: int,
    *,
    trigger: str = "manual",
    send_email: bool = True,
) -> dict[str, Any]:
    """End-to-end edition pipeline: sync sources -> dedup -> AI -> EPUB3 -> archive -> SMTP."""
    settings = get_settings()
    settings.ensure_storage_dirs()
    factory = get_session_factory()
    started = time.monotonic()
    stage_timings: dict[str, int] = {}

    async with factory() as session:
        edition = (
            await session.execute(
                select(Edition)
                .options(selectinload(Edition.source_links).selectinload(EditionSource.source))
                .where(Edition.id == edition_id)
            )
        ).scalar_one_or_none()
        if edition is None:
            return {"error": "edition_not_found", "edition_id": edition_id}

        smtp_cfg = await get_smtp_config(session)
        ai_cfg = await get_ai_config(session)
        net_cfg = await get_network_config(session)

        raw_targets = edition.target_emails.strip() or str(
            smtp_cfg.get("default_kindle_email") or ""
        )
        recipients = parse_recipients(raw_targets)

        delivery = Delivery(
            edition_id=edition.id,
            title=edition.name,
            trigger=trigger,
            target_email=", ".join(recipients),
            status="running",
        )
        session.add(delivery)
        await session.commit()
        await session.refresh(delivery)

        await publish_event(
            "delivery.stage",
            {
                "delivery_id": delivery.id,
                "edition_id": edition.id,
                "title": edition.name,
                "stage": "syncing",
            },
        )

        try:
            # 1. Sync all bound enabled sources
            t0 = time.monotonic()
            active_links = [
                link
                for link in edition.source_links
                if link.source is not None and link.source.enabled
            ]
            for link in active_links:
                try:
                    await sync_source(session, link.source)
                except Exception as exc:
                    logger.warning(
                        "edition_source_sync_error",
                        edition_id=edition.id,
                        source_id=link.source_id,
                        err=str(exc),
                    )
            stage_timings["sync_ms"] = int((time.monotonic() - t0) * 1000)

            # 2. Select fresh, undelivered articles within lookback window
            cutoff_pub = utcnow() - timedelta(hours=max(1, edition.lookback_hours))
            cutoff_dedup = utcnow() - timedelta(days=max(1, edition.dedup_window_days))

            already_sent_ids = set(
                (
                    await session.execute(
                        select(ArticleDelivery.article_id).where(
                            ArticleDelivery.edition_id == edition.id,
                            ArticleDelivery.delivered_at >= cutoff_dedup,
                        )
                    )
                )
                .scalars()
                .all()
            )

            selected_articles: list[Article] = []
            summarize_ids: set[int] = set()
            bilingual_ids: set[int] = set()
            seen_ids: set[int] = set()

            for link in active_links:
                src = link.source
                per_limit = link.max_items_override or src.max_items or 12
                rows = (
                    (
                        await session.execute(
                            select(Article)
                            .where(
                                Article.source_id == src.id,
                                Article.fetched_at >= cutoff_pub,
                            )
                            .order_by(desc(Article.published_at), desc(Article.id))
                            .limit(per_limit * 2)
                        )
                    )
                    .scalars()
                    .all()
                )
                taken = 0
                for art in rows:
                    if art.id in already_sent_ids or art.id in seen_ids:
                        continue
                    selected_articles.append(art)
                    seen_ids.add(art.id)
                    ai_opts = src.ai_options or {}
                    if ai_opts.get("summarize"):
                        summarize_ids.add(art.id)
                    if ai_opts.get("bilingual"):
                        bilingual_ids.add(art.id)
                    taken += 1
                    if taken >= per_limit or len(selected_articles) >= edition.max_articles:
                        break
                if len(selected_articles) >= edition.max_articles:
                    break

            if not selected_articles:
                delivery.status = "skipped"
                delivery.error_message = "本期时间窗口内暂无未推送的新文章"
                delivery.duration_ms = int((time.monotonic() - started) * 1000)
                delivery.stage_timings = stage_timings
                delivery.finished_at = utcnow()
                edition.last_run_at = utcnow()
                await session.commit()
                await publish_event(
                    "delivery.completed",
                    {"delivery_id": delivery.id, "status": "skipped", "title": edition.name},
                )
                return {"delivery_id": delivery.id, "status": "skipped", "article_count": 0}

            # 3. Optional AI enrichment & frontpage briefing
            t1 = time.monotonic()
            briefing_html = ""
            ai_client = await _make_ai_client(ai_cfg, net_cfg["outbound_proxy"])
            if ai_client is not None:
                await publish_event(
                    "delivery.stage",
                    {"delivery_id": delivery.id, "edition_id": edition.id, "stage": "ai_editorial"},
                )
                try:
                    await enrich_articles_with_ai(
                        ai_client,
                        selected_articles,
                        summarize_ids=summarize_ids,
                        bilingual_ids=bilingual_ids,
                    )
                    if edition.ai_briefing_enabled:
                        briefing_html = await generate_frontpage_briefing(
                            ai_client, edition.name, selected_articles
                        )
                except Exception as exc:
                    logger.warning("ai_editorial_degraded", edition_id=edition.id, err=str(exc))
                finally:
                    await ai_client.close()
                await session.commit()
            stage_timings["ai_ms"] = int((time.monotonic() - t1) * 1000)

            # 4. Build strict Kindle EPUB3 volume(s)
            await publish_event(
                "delivery.stage",
                {"delivery_id": delivery.id, "edition_id": edition.id, "stage": "building_epub"},
            )
            t2 = time.monotonic()
            try:
                tz = ZoneInfo(edition.timezone)
            except Exception:
                tz = ZoneInfo("Asia/Shanghai")
            now_local = datetime.now(tz)
            next_issue = (edition.issue_counter or 0) + 1

            chapters_input = [
                EpubChapterInput(
                    title=art.translated_title or art.title,
                    source_name=art.source_name,
                    category=art.category,
                    author=art.author,
                    published_at=art.published_at.astimezone(tz),
                    canonical_url=art.canonical_url,
                    body_html=art.clean_html,
                    ai_summary=art.ai_summary,
                    bilingual_html=art.bilingual_html,
                    language=art.language,
                )
                for art in selected_articles
            ]

            volumes = await build_kindle_edition_epubs(
                output_dir=settings.library_data_dir / "editions",
                edition_title=edition.name,
                subtitle=edition.subtitle,
                issue_number=next_issue,
                issue_date=now_local,
                chapters_input=chapters_input,
                briefing_html=briefing_html,
                cover_style=edition.cover_style,
                max_mb_per_volume=edition.max_mb_per_volume,
                grayscale_images=edition.grayscale_images,
                include_qr_links=edition.include_qr_links,
                outbound_proxy=net_cfg["outbound_proxy"],
            )
            stage_timings["epub_ms"] = int((time.monotonic() - t2) * 1000)

            # 5. Archive generated edition issue(s) into the Library (books table)
            saved_books: list[Book] = []
            for vol in volumes:
                cover_file = settings.library_data_dir / "covers" / f"{vol.path.stem}.jpg"
                cover_file.write_bytes(vol.cover_bytes)
                book = Book(
                    kind="edition_issue",
                    edition_id=edition.id,
                    title=vol.title,
                    author=edition.name,
                    description=f"第 {next_issue} 期 · 收录 {vol.article_count} 篇文章",
                    language="zh-CN",
                    original_format="epub",
                    original_path=str(vol.path),
                    kindle_epub_path=str(vol.path),
                    cover_path=str(cover_file),
                    file_size_bytes=vol.size_bytes,
                    conversion_status="ready",
                    metadata_json={
                        "issue_number": next_issue,
                        "volume_index": vol.volume_index,
                        "total_volumes": vol.total_volumes,
                        "article_count": vol.article_count,
                    },
                )
                session.add(book)
                saved_books.append(book)
            await session.flush()

            # 6. Deliver via SMTP if enabled & configured
            t3 = time.monotonic()
            if send_email:
                if not recipients:
                    raise ValueError("未配置 Kindle 收件邮箱，请在工作室设置或刊物中填写")
                await publish_event(
                    "delivery.stage",
                    {"delivery_id": delivery.id, "edition_id": edition.id, "stage": "sending_smtp"},
                )
                for vol in volumes:
                    await send_ebook_via_smtp(
                        smtp_config=smtp_cfg,
                        recipients=recipients,
                        subject=f"InkPress · {vol.title}",
                        body_text=f"Delivered by InkPress ({vol.article_count} articles).",
                        attachments=[vol.path],
                    )
                for book in saved_books:
                    book.pushed_count += 1
                    book.last_pushed_at = utcnow()
            stage_timings["smtp_ms"] = int((time.monotonic() - t3) * 1000)

            # 7. Record article delivery dedup ledger
            for art in selected_articles:
                session.add(
                    ArticleDelivery(
                        edition_id=edition.id,
                        article_id=art.id,
                        delivery_id=delivery.id,
                    )
                )

            edition.issue_counter = next_issue
            edition.last_run_at = utcnow()
            delivery.book_id = saved_books[0].id if saved_books else None
            delivery.title = volumes[0].title
            delivery.status = "sent"
            delivery.article_count = len(selected_articles)
            delivery.volume_count = len(volumes)
            delivery.file_size_bytes = sum(v.size_bytes for v in volumes)
            delivery.duration_ms = int((time.monotonic() - started) * 1000)
            delivery.stage_timings = stage_timings
            delivery.finished_at = utcnow()
            await session.commit()

            await publish_event(
                "delivery.completed",
                {
                    "delivery_id": delivery.id,
                    "edition_id": edition.id,
                    "status": "sent",
                    "title": delivery.title,
                    "article_count": delivery.article_count,
                    "size_bytes": delivery.file_size_bytes,
                },
            )
            return {
                "delivery_id": delivery.id,
                "status": "sent",
                "title": delivery.title,
                "article_count": delivery.article_count,
                "volume_count": delivery.volume_count,
                "size_bytes": delivery.file_size_bytes,
                "book_id": delivery.book_id,
            }
        except Exception as exc:
            delivery.status = "failed"
            delivery.error_message = str(exc)[:2000]
            delivery.duration_ms = int((time.monotonic() - started) * 1000)
            delivery.stage_timings = stage_timings
            delivery.finished_at = utcnow()
            await session.commit()
            await publish_event(
                "delivery.failed",
                {
                    "delivery_id": delivery.id,
                    "edition_id": edition.id,
                    "title": edition.name,
                    "error": str(exc),
                },
            )
            logger.exception(
                "edition_delivery_failed", edition_id=edition.id, delivery_id=delivery.id
            )
            raise


async def run_convert_book(book_id: int) -> dict[str, Any]:
    settings = get_settings()
    settings.ensure_storage_dirs()
    factory = get_session_factory()
    async with factory() as session:
        book = await session.get(Book, book_id)
        if book is None:
            return {"error": "book_not_found", "book_id": book_id}
        book.conversion_status = "converting"
        book.conversion_error = None
        await session.commit()
        await publish_event("book.convert.started", {"book_id": book.id, "title": book.title})
        try:
            res = await convert_book_to_kindle_epub(
                Path(book.original_path),
                output_dir=settings.library_data_dir / "epubs",
                cover_dir=settings.library_data_dir / "covers",
                title_override=book.title,
                author_override=book.author,
            )
            book.title = res.title[:500]
            book.author = res.author[:250]
            book.language = res.language[:16]
            book.kindle_epub_path = str(res.kindle_epub_path)
            book.cover_path = str(res.cover_path) if res.cover_path else None
            book.file_size_bytes = res.file_size_bytes
            book.conversion_status = "ready"
            book.metadata_json = {
                **(book.metadata_json or {}),
                "chapter_count": res.chapter_count,
                "conversion_notes": res.notes,
            }
            await session.commit()
            await publish_event(
                "book.convert.completed",
                {"book_id": book.id, "title": book.title, "size_bytes": book.file_size_bytes},
            )
            return {"book_id": book.id, "status": "ready", "notes": res.notes}
        except Exception as exc:
            book.conversion_status = "failed"
            book.conversion_error = str(exc)[:1500]
            await session.commit()
            await publish_event(
                "book.convert.failed",
                {"book_id": book.id, "title": book.title, "error": str(exc)},
            )
            raise


async def run_deliver_book(
    book_id: int,
    *,
    target_email: str = "",
    trigger: str = "manual",
) -> dict[str, Any]:
    factory = get_session_factory()
    started = time.monotonic()
    async with factory() as session:
        book = await session.get(Book, book_id)
        if book is None:
            return {"error": "book_not_found", "book_id": book_id}
        if book.conversion_status != "ready" or not book.kindle_epub_path:
            raise ValueError("电子书尚未完成格式转换，无法推送")

        smtp_cfg = await get_smtp_config(session)
        raw_targets = target_email.strip() or str(smtp_cfg.get("default_kindle_email") or "")
        recipients = parse_recipients(raw_targets)
        if not recipients:
            raise ValueError("未配置 Kindle 收件邮箱")

        file_path = Path(book.kindle_epub_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Kindle file missing: {file_path}")

        delivery = Delivery(
            book_id=book.id,
            title=book.title,
            trigger=trigger,
            target_email=", ".join(recipients),
            status="running",
            article_count=int((book.metadata_json or {}).get("chapter_count", 1)),
            volume_count=1,
            file_size_bytes=file_path.stat().st_size,
        )
        session.add(delivery)
        await session.commit()
        await session.refresh(delivery)

        try:
            await send_ebook_via_smtp(
                smtp_config=smtp_cfg,
                recipients=recipients,
                subject=f"InkPress · {book.title}",
                body_text=f"Book delivered from InkPress Library: {book.title}",
                attachments=[file_path],
            )
            book.pushed_count = (book.pushed_count or 0) + 1
            book.last_pushed_at = utcnow()
            if trigger == "scheduled":
                book.scheduled_push_at = None
            delivery.status = "sent"
            delivery.duration_ms = int((time.monotonic() - started) * 1000)
            delivery.finished_at = utcnow()
            await session.commit()
            await publish_event(
                "delivery.completed",
                {
                    "delivery_id": delivery.id,
                    "book_id": book.id,
                    "status": "sent",
                    "title": book.title,
                },
            )
            return {"delivery_id": delivery.id, "book_id": book.id, "status": "sent"}
        except Exception as exc:
            delivery.status = "failed"
            delivery.error_message = str(exc)[:2000]
            delivery.duration_ms = int((time.monotonic() - started) * 1000)
            delivery.finished_at = utcnow()
            await session.commit()
            await publish_event(
                "delivery.failed",
                {
                    "delivery_id": delivery.id,
                    "book_id": book.id,
                    "title": book.title,
                    "error": str(exc),
                },
            )
            raise


async def run_sync_source_bg(source_id: int) -> None:
    try:
        await run_sync_source(source_id)
    except Exception as exc:
        logger.warning("bg_sync_source_error", source_id=source_id, err=str(exc))


async def run_build_and_deliver_edition_bg(
    edition_id: int,
    *,
    trigger: str = "manual",
    send_email: bool = True,
) -> None:
    try:
        await run_build_and_deliver_edition(edition_id, trigger=trigger, send_email=send_email)
    except Exception as exc:
        logger.warning("bg_edition_error", edition_id=edition_id, err=str(exc))


async def run_convert_book_bg(book_id: int) -> None:
    try:
        await run_convert_book(book_id)
    except Exception as exc:
        logger.warning("bg_convert_book_error", book_id=book_id, err=str(exc))


async def run_deliver_book_bg(
    book_id: int,
    *,
    target_email: str = "",
    trigger: str = "manual",
) -> None:
    try:
        await run_deliver_book(book_id, target_email=target_email, trigger=trigger)
    except Exception as exc:
        logger.warning("bg_deliver_book_error", book_id=book_id, err=str(exc))


class _JobHandle:
    def __init__(self, job_type: str) -> None:
        self.job_type = job_type

    async def kiq(self, *args: Any, **kwargs: Any) -> str:
        mapped = dict(kwargs)
        if self.job_type == "sync_source" and args:
            mapped["source_id"] = args[0]
        elif self.job_type == "build_and_deliver_edition" and args:
            mapped["edition_id"] = args[0]
        elif (self.job_type == "convert_book" and args) or (
            self.job_type == "deliver_book" and args
        ):
            mapped["book_id"] = args[0]
        return await enqueue_job(self.job_type, mapped)


sync_source_task = _JobHandle("sync_source")
build_and_deliver_edition_task = _JobHandle("build_and_deliver_edition")
convert_book_task = _JobHandle("convert_book")
deliver_book_task = _JobHandle("deliver_book")
