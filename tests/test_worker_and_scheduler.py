from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from inkpress.config import get_settings
from inkpress.db.base import Base, get_engine, get_session_factory, reset_db_globals
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
from inkpress.publishing.book_converter import ConvertedBookResult
from inkpress.worker.broker import (
    JOBS_GROUP_NAME,
    JOBS_STREAM_KEY,
    WORKER_HEARTBEAT_KEY,
    enqueue_job,
    publish_worker_heartbeat,
)
from inkpress.worker.runner import _process_stream_entry, recover_stale_running_deliveries
from inkpress.worker.scheduler import is_edition_due, run_retention_gc
from inkpress.worker.tasks import run_build_and_deliver_edition, run_convert_book
from sqlalchemy import func, select


@pytest.fixture
async def worker_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_file = tmp_path / "worker_test.db"
    lib_dir = tmp_path / "library"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("LIBRARY_DATA_DIR", str(lib_dir))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-at-least-32-bytes-long!!")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/15")
    monkeypatch.setenv("DEFAULT_KINDLE_EMAIL", "kindle@example.com")

    get_settings.cache_clear()
    reset_db_globals()

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield lib_dir

    await engine.dispose()
    reset_db_globals()
    get_settings.cache_clear()


async def test_send_email_false_does_not_consume_dedup_ledger_and_strips_links(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        src = Source(name="Tech", kind="rss", identifier="https://example.com/rss")
        session.add(src)
        await session.flush()

        art = Article(
            source_id=src.id,
            canonical_url="https://example.com/article-1",
            content_hash="hash-1",
            title="Strip Links Verification",
            source_name="Tech",
            category="科技",
            clean_html='<p>Read <a href="https://external.example/link">this link</a> carefully.</p>',
            plain_text="Read this link carefully.",
            published_at=utcnow(),
            fetched_at=utcnow(),
        )
        ed = Edition(
            name="Morning Test",
            strip_links=True,
            ai_briefing_enabled=False,
            target_emails="kindle@example.com",
        )
        session.add_all([art, ed])
        await session.flush()
        session.add(EditionSource(edition_id=ed.id, source_id=src.id, sort_order=0))
        await session.commit()
        ed_id = ed.id

    async def noop_sync(_session: Any, _source: Any) -> dict[str, int]:
        return {"fetched": 0, "inserted": 0}

    sent_attachments: list[Path] = []

    async def fake_smtp(**kw: Any) -> None:
        sent_attachments.extend(kw.get("attachments") or [])

    monkeypatch.setattr("inkpress.worker.tasks.sync_source", noop_sync)
    monkeypatch.setattr("inkpress.worker.tasks.send_ebook_via_smtp", fake_smtp)

    # 1. Preview build (send_email=False) MUST NOT write ArticleDelivery dedup ledger
    res_preview = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=False)
    assert res_preview["status"] == "sent"
    assert res_preview["article_count"] == 1
    assert len(sent_attachments) == 0

    async with factory() as session:
        dedup_count = int((await session.execute(select(func.count(ArticleDelivery.id)))).scalar_one())
        assert dedup_count == 0
        book = await session.get(Book, res_preview["book_id"])
        assert book is not None
        # Verify hyperlink was stripped inside generated EPUB body
        with zipfile.ZipFile(book.kindle_epub_path, "r") as zf:
            ch_names = [n for n in zf.namelist() if "ch_001" in n]
            assert ch_names
            ch_html = zf.read(ch_names[0]).decode("utf-8")
            assert "https://external.example/link" not in ch_html
            assert "this link" in ch_html

    # 2. Real delivery (send_email=True) still picks up the article & records dedup ledger
    res_real = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert res_real["status"] == "sent"
    assert len(sent_attachments) == 1

    async with factory() as session:
        dedup_count = int((await session.execute(select(func.count(ArticleDelivery.id)))).scalar_one())
        assert dedup_count == 1

    # 3. Subsequent send_email=True is skipped because article is now in dedup ledger
    res_skip = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert res_skip["status"] == "skipped"


async def test_failed_smtp_attaches_book_id_for_retry(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        src = Source(name="News", kind="rss", identifier="https://example.com/news")
        session.add(src)
        await session.flush()
        art = Article(
            source_id=src.id,
            canonical_url="https://example.com/article-2",
            content_hash="hash-2",
            title="Retry Book Reuse",
            source_name="News",
            category="综合",
            clean_html="<p>Body text</p>",
            plain_text="Body text",
            published_at=utcnow(),
            fetched_at=utcnow(),
        )
        ed = Edition(name="Retry Edition", ai_briefing_enabled=False, target_emails="k@example.com")
        session.add_all([art, ed])
        await session.flush()
        session.add(EditionSource(edition_id=ed.id, source_id=src.id, sort_order=0))
        await session.commit()
        ed_id = ed.id

    async def noop_sync(_session: Any, _source: Any) -> dict[str, int]:
        return {"fetched": 0, "inserted": 0}

    async def failing_smtp(**_kw: Any) -> None:
        raise RuntimeError("SMTP connection refused")

    monkeypatch.setattr("inkpress.worker.tasks.sync_source", noop_sync)
    monkeypatch.setattr("inkpress.worker.tasks.send_ebook_via_smtp", failing_smtp)

    with pytest.raises(RuntimeError, match="SMTP connection refused"):
        await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)

    async with factory() as session:
        deliv = (await session.execute(select(Delivery).where(Delivery.edition_id == ed_id))).scalar_one()
        assert deliv.status == "failed"
        assert deliv.book_id is not None  # Built EPUB attached before SMTP for zero-rebuild retry!
        dedup_count = int((await session.execute(select(func.count(ArticleDelivery.id)))).scalar_one())
        assert dedup_count == 0


async def test_run_convert_book_respects_has_custom_title(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orig_file = worker_db / "sample.txt"
    orig_file.parent.mkdir(parents=True, exist_ok=True)
    orig_file.write_text("Book content", encoding="utf-8")

    factory = get_session_factory()
    async with factory() as session:
        b_auto = Book(
            title="filename_fallback",
            original_format="txt",
            original_path=str(orig_file),
            conversion_status="converting",
            metadata_json={"has_custom_title": False},
        )
        b_custom = Book(
            title="My Explicit Title",
            original_format="txt",
            original_path=str(orig_file),
            conversion_status="converting",
            metadata_json={"has_custom_title": True},
        )
        session.add_all([b_auto, b_custom])
        await session.commit()
        auto_id, custom_id = b_auto.id, b_custom.id

    seen_overrides: list[str] = []

    async def fake_convert(
        _path: Path, *, title_override: str = "", **_kw: Any
    ) -> ConvertedBookResult:
        seen_overrides.append(title_override)
        out_epub = worker_db / "epubs" / "out.epub"
        out_epub.parent.mkdir(parents=True, exist_ok=True)
        out_epub.write_bytes(b"PK")
        return ConvertedBookResult(
            kindle_epub_path=out_epub,
            cover_path=None,
            title=title_override or "Real Metadata Title",
            author="Author",
            language="zh-CN",
            chapter_count=1,
            file_size_bytes=2,
            notes="ok",
        )

    monkeypatch.setattr("inkpress.worker.tasks.convert_book_to_kindle_epub", fake_convert)

    await run_convert_book(auto_id)
    await run_convert_book(custom_id)
    assert seen_overrides == ["", "My Explicit Title"]

    async with factory() as session:
        b1 = await session.get(Book, auto_id)
        b2 = await session.get(Book, custom_id)
        assert b1 is not None and b1.title == "Real Metadata Title"
        assert b2 is not None and b2.title == "My Explicit Title"


async def test_recover_stale_running_deliveries(worker_db: Path) -> None:
    factory = get_session_factory()
    now = utcnow()
    async with factory() as session:
        stale = Delivery(
            title="Stale Job",
            status="running",
            created_at=now - timedelta(minutes=30),
        )
        fresh = Delivery(
            title="Fresh Job",
            status="running",
            created_at=now - timedelta(minutes=2),
        )
        session.add_all([stale, fresh])
        await session.commit()
        stale_id, fresh_id = stale.id, fresh.id

    count = await recover_stale_running_deliveries(stale_minutes=15)
    assert count == 1

    async with factory() as session:
        d_stale = await session.get(Delivery, stale_id)
        d_fresh = await session.get(Delivery, fresh_id)
        assert d_stale is not None and d_stale.status == "failed"
        assert d_fresh is not None and d_fresh.status == "running"


def test_scheduler_misfire_grace_and_manual_collision() -> None:
    ed = Edition(
        name="Daily 06:00",
        cron_expr="0 6 * * *",
        timezone="Asia/Shanghai",
        enabled=True,
        last_run_at=datetime(2026, 11, 18, 6, 0, tzinfo=UTC),
    )

    # 14:00 CST (06:00 UTC) -> 8 hours after 06:00 CST -> outside 15-min grace window!
    now_afternoon_utc = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)
    assert is_edition_due(ed, now_afternoon_utc) is False

    # 06:05 CST (22:05 UTC previous day) -> 5 minutes after 06:00 CST -> DUE!
    now_due_utc = datetime(2026, 11, 20, 22, 5, tzinfo=UTC)
    assert is_edition_due(ed, now_due_utc) is True

    # Manual delivery ran within 30 min -> skipped!
    assert is_edition_due(ed, now_due_utc, has_recent_delivery=True) is False

    # Already ran at 06:01 CST -> NOT due again at 06:05 CST!
    ed.last_run_at = datetime(2026, 11, 20, 22, 1, tzinfo=UTC)
    assert is_edition_due(ed, now_due_utc) is False


async def test_retention_gc_keeps_starred_and_delivered_articles(worker_db: Path) -> None:
    factory = get_session_factory()
    now = utcnow()
    old_ts = now - timedelta(days=45)
    fresh_ts = now - timedelta(days=5)

    async with factory() as session:
        ed = Edition(name="Ed", retention_days=30)
        session.add(ed)
        await session.flush()

        a_delete = Article(
            canonical_url="https://example.com/old-unstarred-undelivered",
            content_hash="h1",
            title="Delete Me",
            is_starred=False,
            fetched_at=old_ts,
            published_at=old_ts,
        )
        a_starred = Article(
            canonical_url="https://example.com/old-starred",
            content_hash="h2",
            title="Keep Starred",
            is_starred=True,
            fetched_at=old_ts,
            published_at=old_ts,
        )
        a_delivered = Article(
            canonical_url="https://example.com/old-delivered",
            content_hash="h3",
            title="Keep Delivered",
            is_starred=False,
            fetched_at=old_ts,
            published_at=old_ts,
        )
        a_fresh = Article(
            canonical_url="https://example.com/fresh",
            content_hash="h4",
            title="Keep Fresh",
            is_starred=False,
            fetched_at=fresh_ts,
            published_at=fresh_ts,
        )
        session.add_all([a_delete, a_starred, a_delivered, a_fresh])
        await session.flush()
        session.add(
            ArticleDelivery(edition_id=ed.id, article_id=a_delivered.id, delivered_at=old_ts)
        )
        await session.commit()

    deleted = await run_retention_gc(now)
    assert deleted == 1

    async with factory() as session:
        remaining_titles = set(
            (await session.execute(select(Article.title))).scalars().all()
        )
        assert remaining_titles == {"Keep Starred", "Keep Delivered", "Keep Fresh"}


async def test_redis_streams_enqueue_process_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.added: list[tuple[str, dict[str, Any]]] = []
            self.acked: list[tuple[str, str, str]] = []
            self.deleted: list[tuple[str, str]] = []
            self.kv: dict[str, str] = {}

        async def xadd(self, key: str, fields: dict[str, Any], **_kw: Any) -> str:
            self.added.append((key, fields))
            return "1-0"

        async def xack(self, key: str, group: str, msg_id: str) -> int:
            self.acked.append((key, group, msg_id))
            return 1

        async def xdel(self, key: str, msg_id: str) -> int:
            self.deleted.append((key, msg_id))
            return 1

        async def set(self, key: str, val: str, ex: int | None = None) -> bool:
            self.kv[key] = val
            return True

        async def aclose(self) -> None:
            return None

    fake = FakeRedis()
    monkeypatch.setattr("inkpress.worker.broker.aioredis.from_url", lambda *_a, **_kw: fake)

    job_id = await enqueue_job("sync_source", {"source_id": 7})
    assert job_id
    assert len(fake.added) == 1
    assert fake.added[0][0] == JOBS_STREAM_KEY

    dispatched: list[dict[str, Any]] = []

    async def fake_dispatch(job: dict[str, Any]) -> dict[str, str]:
        dispatched.append(job)
        return {"ok": "yes"}

    monkeypatch.setattr("inkpress.worker.runner.dispatch_job", fake_dispatch)
    await _process_stream_entry(fake, "1-0", fake.added[0][1], worker_label="worker-test")  # type: ignore[arg-type]
    assert len(dispatched) == 1
    assert dispatched[0]["type"] == "sync_source"
    assert fake.acked == [(JOBS_STREAM_KEY, JOBS_GROUP_NAME, "1-0")]
    assert fake.deleted == [(JOBS_STREAM_KEY, "1-0")]

    hb = await publish_worker_heartbeat(fake)  # type: ignore[arg-type]
    assert WORKER_HEARTBEAT_KEY in fake.kv
    parsed_hb = json.loads(fake.kv[WORKER_HEARTBEAT_KEY])
    assert parsed_hb["ts"] == hb["ts"]
    assert "calibre_ebook_convert" in parsed_hb
    assert "pandoc" in parsed_hb
