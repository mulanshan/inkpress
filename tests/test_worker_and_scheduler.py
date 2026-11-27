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
from inkpress.worker.runner import (
    DEAD_LETTER_STREAM_KEY,
    _process_stream_entry,
    _reclaim_once,
    recover_stale_running_deliveries,
)
from inkpress.worker.scheduler import is_edition_due, run_retention_gc
from inkpress.worker.tasks import (
    run_build_and_deliver_edition,
    run_convert_book,
    run_retry_delivery,
)
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

    # 1. Preview build (send_email=False) MUST NOT write ArticleDelivery, increment issue_counter, or set last_run_at
    res_preview = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=False)
    assert res_preview["status"] == "generated"
    assert res_preview["article_count"] == 1
    assert len(sent_attachments) == 0

    async with factory() as session:
        dedup_count = int((await session.execute(select(func.count(ArticleDelivery.id)))).scalar_one())
        assert dedup_count == 0
        ed_after_preview = await session.get(Edition, ed_id)
        assert ed_after_preview is not None
        assert ed_after_preview.issue_counter == 0
        assert ed_after_preview.last_run_at is None
        deliv_preview = await session.get(Delivery, res_preview["delivery_id"])
        assert deliv_preview is not None and deliv_preview.target_email == ""
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


async def test_three_tier_edition_pipeline_anchor_digest_deep_and_ignore_dedup(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = get_session_factory()
    now = utcnow()
    async with factory() as session:
        s_tugua = Source(name="喷嚏图卦", kind="rss", identifier="https://dapenti.com/rss")
        s_zhihu = Source(
            name="知乎热榜",
            kind="newsnow",
            identifier="zhihu",
            fetch_strategy="newsnow_digest",
        )
        s_sspai = Source(name="少数派", kind="rss", identifier="https://sspai.com/feed")
        session.add_all([s_tugua, s_zhihu, s_sspai])
        await session.flush()

        a_tugua = Article(
            source_id=s_tugua.id,
            canonical_url="https://dapenti.com/tugua-1127",
            content_hash="tg1",
            title="【喷嚏图卦20261127】今日世界切片",
            source_name="喷嚏图卦",
            category="综合",
            clean_html="<p>【1】美联储会议纪要公布</p>",
            plain_text="【1】美联储会议纪要公布",
            word_count=500,
            published_at=now,
            fetched_at=now,
        )
        a_digest = Article(
            source_id=s_zhihu.id,
            canonical_url="https://inkpress.local/newsnow/zhihu/2026-11-27",
            content_hash="zh1",
            title="知乎热榜 · 热点速览 (2026-11-27)",
            source_name="知乎热榜",
            category="综合",
            clean_html='<div class="digest-item"><h3>1. 美联储会议纪要释放什么信号？</h3><p>美联储纪要内容。</p></div>',
            plain_text="美联储会议纪要",
            word_count=300,
            extra_meta={
                "newsnow_id": "zhihu",
                "digest_items": [
                    {
                        "title": "美联储会议纪要释放什么信号？",
                        "summary": "与喷嚏图卦重复的话题应被排除。",
                        "info_tag": "500万",
                        "url": "https://zhihu.com/q/1",
                        "image_url": "",
                    },
                    {
                        "title": "中国首例无线脑机接口临床试验完成",
                        "summary": "脑机接口帮助渐冻症患者恢复语言交流能力，取得关键临床突破。",
                        "info_tag": "900万",
                        "url": "https://zhihu.com/q/2",
                        "image_url": "",
                    },
                ],
            },
            published_at=now,
            fetched_at=now,
        )
        deep_text = "这是一篇关于电子墨水屏工作流与深度阅读习惯的长篇评测文章。" * 80
        a_deep = Article(
            source_id=s_sspai.id,
            canonical_url="https://sspai.com/post/deep-1",
            content_hash="sp1",
            title="少数派深度：电子纸慢读体系构建指南",
            source_name="少数派",
            category="科技",
            clean_html=f"<p>{deep_text}</p>",
            plain_text=deep_text,
            word_count=2200,
            published_at=now,
            fetched_at=now,
        )
        ed = Edition(
            name="三段式精选晨报",
            ai_briefing_enabled=True,
            strip_links=True,
            target_emails="kindle@example.com",
        )
        session.add_all([a_tugua, a_digest, a_deep, ed])
        await session.flush()
        session.add_all(
            [
                EditionSource(edition_id=ed.id, source_id=s_tugua.id, sort_order=0),
                EditionSource(edition_id=ed.id, source_id=s_zhihu.id, sort_order=1),
                EditionSource(edition_id=ed.id, source_id=s_sspai.id, sort_order=2),
            ]
        )
        await session.commit()
        ed_id = ed.id

    async def noop_sync(_session: Any, _source: Any) -> dict[str, int]:
        return {"new": 0, "skipped": 0, "failed": 0}

    async def noop_smtp(**_kw: Any) -> None:
        return None

    monkeypatch.setattr("inkpress.worker.tasks.sync_source", noop_sync)
    monkeypatch.setattr("inkpress.worker.tasks.send_ebook_via_smtp", noop_smtp)

    res = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert res["status"] == "sent"
    # 3 chapters: ch_001 (喷嚏图卦) + ch_002 (今日全网图文辑要) + ch_003 (少数派深度), NO ch_000_briefing!
    assert res["article_count"] == 3

    async with factory() as session:
        book = await session.get(Book, res["book_id"])
        assert book is not None
        with zipfile.ZipFile(book.kindle_epub_path, "r") as zf:
            names = zf.namelist()
            assert "OEBPS/ch_000_briefing.xhtml" not in names
            ch1 = zf.read("OEBPS/ch_001.xhtml").decode("utf-8")
            ch2 = zf.read("OEBPS/ch_002.xhtml").decode("utf-8")
            ch3 = zf.read("OEBPS/ch_003.xhtml").decode("utf-8")
            assert "喷嚏图卦20261127" in ch1
            assert "今日全网图文辑要" in ch2
            assert "脑机接口" in ch2
            assert "美联储会议纪要" not in ch2  # Deduplicated against 喷嚏图卦!
            assert "少数派深度" in ch3

    # Preview rebuild with ignore_dedup=True works even after morning delivery recorded dedup ledger!
    res_preview = await run_build_and_deliver_edition(
        ed_id, trigger="manual", send_email=False, ignore_dedup=True
    )
    assert res_preview["status"] == "generated"
    assert res_preview["article_count"] == 3


async def test_multivolume_retry_closes_dedup_ledger_and_updates_same_delivery(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = get_session_factory()
    now = utcnow()
    big_html = "<p>" + ("多卷失败重试闭环测试正文内容。" * 6000) + "</p>"
    async with factory() as session:
        src = Source(name="LongReads", kind="rss", identifier="https://example.com/long")
        session.add(src)
        await session.flush()
        arts = [
            Article(
                source_id=src.id,
                canonical_url=f"https://example.com/long-{i}",
                content_hash=f"lh-{i}",
                title=f"长文卷 {i}",
                source_name="LongReads",
                category="深度",
                clean_html=big_html,
                plain_text="长文",
                word_count=2000,
                published_at=now,
                fetched_at=now,
            )
            for i in range(1, 4)
        ]
        ed = Edition(
            name="多卷重试刊",
            ai_briefing_enabled=False,
            max_mb_per_volume=1,
            target_emails="kindle@example.com",
        )
        session.add_all([*arts, ed])
        await session.flush()
        session.add(EditionSource(edition_id=ed.id, source_id=src.id, sort_order=0))
        await session.commit()
        ed_id = ed.id

    async def noop_sync(_session: Any, _source: Any) -> dict[str, int]:
        return {"new": 0, "skipped": 0, "failed": 0}

    sent_subjects: list[str] = []
    fail_on_vol2 = True

    async def flaky_smtp(**kw: Any) -> None:
        subj = str(kw.get("subject") or "")
        if fail_on_vol2 and "(2/" in subj:
            raise RuntimeError("SMTP transient failure on volume 2")
        sent_subjects.append(subj)

    monkeypatch.setattr("inkpress.worker.tasks.sync_source", noop_sync)
    monkeypatch.setattr("inkpress.worker.tasks.send_ebook_via_smtp", flaky_smtp)

    with pytest.raises(RuntimeError, match="volume 2"):
        await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)

    # Volume 1 was sent, Volume 2 failed -> Delivery failed, dedup ledger STILL 0
    assert len(sent_subjects) == 1
    assert "(1/2)" in sent_subjects[0]
    async with factory() as session:
        delivs = (await session.execute(select(Delivery))).scalars().all()
        assert len(delivs) == 1
        failed_deliv_id = delivs[0].id
        assert delivs[0].status == "failed"
        assert int((await session.execute(select(func.count(ArticleDelivery.id)))).scalar_one()) == 0

    # Now SMTP recovers -> run_retry_delivery sends ONLY remaining Volume 2, updates SAME Delivery row,
    # writes all 3 articles to ArticleDelivery, and increments issue_counter to 1!
    fail_on_vol2 = False
    retry_res = await run_retry_delivery(failed_deliv_id, trigger="retry")
    assert retry_res["status"] == "sent"
    assert retry_res["delivery_id"] == failed_deliv_id
    assert len(sent_subjects) == 2
    assert "(2/2)" in sent_subjects[1]

    async with factory() as session:
        all_delivs = (await session.execute(select(Delivery))).scalars().all()
        assert len(all_delivs) == 1  # No orphan duplicate Delivery row!
        assert all_delivs[0].status == "sent"
        assert int((await session.execute(select(func.count(ArticleDelivery.id)))).scalar_one()) == 3
        ed_done = await session.get(Edition, ed_id)
        assert ed_done is not None and ed_done.issue_counter == 1


async def test_worker_lease_renewal_dlq_eviction_and_edition_mutex(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    # 1. Lease renewal calls XCLAIM ... JUSTID while slow job executes, and DLQ evicts times_delivered >= 3
    class StreamFakeRedis:
        def __init__(self) -> None:
            self.xclaims: list[tuple[str, list[str], bool]] = []
            self.dlq_items: list[dict[str, Any]] = []
            self.acked: list[str] = []
            self.deleted: list[str] = []

        async def xclaim(
            self,
            name: str,
            groupname: str,
            consumername: str,
            min_idle_time: int,
            message_ids: list[str],
            justid: bool = False,
            **_kw: Any,
        ) -> list[str]:
            self.xclaims.append((consumername, list(message_ids), justid))
            return list(message_ids)

        async def xpending_range(self, *_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {"message_id": "9-0", "consumer": "dead-w", "time_since_delivered": 400_000, "times_delivered": 3}
            ]

        async def xrange(self, *_a: Any, **_kw: Any) -> list[tuple[str, dict[str, str]]]:
            return [("9-0", {"payload": '{"type":"sync_source","kwargs":{"source_id":1}}'})]

        async def xadd(self, key: str, fields: dict[str, Any], **_kw: Any) -> str:
            if key == DEAD_LETTER_STREAM_KEY:
                self.dlq_items.append(dict(fields))
            return "dlq-1"

        async def xautoclaim(self, *_a: Any, **_kw: Any) -> tuple[str, list[Any]]:
            return ("0-0", [])

        async def xack(self, _key: str, _group: str, msg_id: str) -> int:
            self.acked.append(msg_id)
            return 1

        async def xdel(self, _key: str, msg_id: str) -> int:
            self.deleted.append(msg_id)
            return 1

    fake_r = StreamFakeRedis()

    async def slow_dispatch(_job: dict[str, Any]) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setattr("inkpress.worker.runner.dispatch_job", slow_dispatch)
    await _process_stream_entry(
        fake_r,  # type: ignore[arg-type]
        "5-0",
        {"payload": '{"id":"j1","type":"sync_source","kwargs":{"source_id":1}}'},
        worker_label="w-lease",
        lease_renew_seconds=0.015,
    )
    assert len(fake_r.xclaims) >= 1
    assert fake_r.xclaims[0] == ("w-lease", ["5-0"], True)

    evicted = await _reclaim_once(fake_r, "reclaimer-1")  # type: ignore[arg-type]
    assert evicted == 1
    assert len(fake_r.dlq_items) == 1
    assert fake_r.dlq_items[0]["failed_msg_id"] == "9-0"
    assert "9-0" in fake_r.acked and "9-0" in fake_r.deleted


async def test_deep_read_reservation_roundup_routing_delta_hotlist_and_archive_gc(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = get_session_factory()
    now = utcnow()
    async with factory() as session:
        s_zb = Source(name="联合早报", kind="newsnow", identifier="zaobao", fetch_strategy="extract_web")
        s_cls = Source(name="财联社热门", kind="newsnow", identifier="cls-hot", fetch_strategy="extract_web")
        s_hot = Source(name="微博热搜", kind="newsnow", identifier="weibo", fetch_strategy="newsnow_digest")
        session.add_all([s_zb, s_cls, s_hot])
        await session.flush()

        # 5 short flashes newer than 1 deep editorial (rank 6) in 联合早报 (per_limit=3)
        zb_flashes = [
            Article(
                source_id=s_zb.id,
                canonical_url=f"https://zaobao.com/flash-{i}",
                content_hash=f"zbf-{i}",
                title=f"国际短讯第{i}条：某国外交部例行记者会简讯",
                source_name="联合早报",
                category="国际",
                clean_html="<p>短讯正文内容。</p>",
                plain_text="短讯正文内容。",
                word_count=300,
                published_at=now - timedelta(minutes=i),
                fetched_at=now,
            )
            for i in range(1, 6)
        ]
        zb_editorial_text = "这是一篇联合早报关于全球贸易格局重构的深度社论长文。" * 70
        zb_editorial = Article(
            source_id=s_zb.id,
            canonical_url="https://zaobao.com/editorial-deep",
            content_hash="zbe-1",
            title="社论：多边贸易秩序的韧性与区域新机遇",
            source_name="联合早报",
            category="国际",
            clean_html=f"<p>{zb_editorial_text}</p>",
            plain_text=zb_editorial_text,
            word_count=1750,
            published_at=now - timedelta(minutes=30),
            fetched_at=now,
        )
        # Long roundup from 财联社热门 that must go to digest pool, NOT steal deep-read slot
        cls_roundup_text = "公告一：某公司回购股份。公告二：某公司业绩预增。" * 80
        cls_roundup = Article(
            source_id=s_cls.id,
            canonical_url="https://cls.cn/roundup-1",
            content_hash="clsr-1",
            title="11月27日盘后公告集锦：多家上市公司披露回购计划",
            source_name="财联社热门",
            category="财经",
            clean_html=f"<p>{cls_roundup_text}</p>",
            plain_text=cls_roundup_text,
            word_count=1800,
            published_at=now,
            fetched_at=now,
        )
        hot_art = Article(
            source_id=s_hot.id,
            canonical_url="https://inkpress.local/newsnow/weibo/2026-11-27",
            content_hash="wb-morning",
            title="微博热搜 · 热点速览 (2026-11-27)",
            source_name="微博热搜",
            category="热点",
            clean_html='<div class="digest-item"><h3>1. 晨间话题一</h3></div>',
            plain_text="晨间话题一",
            word_count=200,
            extra_meta={
                "newsnow_id": "weibo",
                "items_sig": "sig-morning",
                "digest_items": [
                    {"title": "央行晨间开展逆回购操作投放流动性", "summary": "晨间操作详情内容介绍。", "url": "https://s.weibo.com/m1"},
                    {"title": "国家统计局公布十月工业利润数据", "summary": "十月规模以上工业企业利润增长。", "url": "https://s.weibo.com/m2"},
                ],
            },
            published_at=now,
            fetched_at=now,
        )
        ed = Edition(
            name="早晚双刊测试",
            ai_briefing_enabled=True,
            max_articles=15,
            retention_days=30,
            target_emails="kindle@example.com",
        )
        session.add_all([*zb_flashes, zb_editorial, cls_roundup, hot_art, ed])
        await session.flush()
        session.add_all(
            [
                EditionSource(edition_id=ed.id, source_id=s_zb.id, sort_order=0, max_items_override=3),
                EditionSource(edition_id=ed.id, source_id=s_cls.id, sort_order=1, max_items_override=2),
                EditionSource(edition_id=ed.id, source_id=s_hot.id, sort_order=2, max_items_override=5),
            ]
        )
        await session.commit()
        ed_id = ed.id
        hot_id = hot_art.id

    async def noop_sync(_session: Any, _source: Any) -> dict[str, int]:
        return {"new": 0, "skipped": 0, "failed": 0}

    async def noop_smtp(**_kw: Any) -> None:
        return None

    monkeypatch.setattr("inkpress.worker.tasks.sync_source", noop_sync)
    monkeypatch.setattr("inkpress.worker.tasks.send_ebook_via_smtp", noop_smtp)

    # Morning formal delivery: reserves 联合早报社论 as standalone deep chapter, routes 盘后公告集锦 to digest!
    res1 = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert res1["status"] == "sent"
    async with factory() as session:
        b1 = await session.get(Book, res1["book_id"])
        assert b1 is not None
        with zipfile.ZipFile(b1.kindle_epub_path, "r") as zf:
            nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
            assert "社论：多边贸易秩序的韧性与区域新机遇" in nav
            assert "盘后公告集锦" not in nav

    # Drain the remaining 3 undelivered zb_flashes from s_zb (since per_limit=3 took 2 flashes + 1 editorial)
    res_drain = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert res_drain["status"] == "sent"

    # Re-run when all articles are delivered and hotlist sig is unchanged -> skipped!
    res_unchanged = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert res_unchanged["status"] == "skipped"

    # Evening hotlist update on same Article row -> delta items picked up for evening formal push!
    async with factory() as session:
        db_hot = await session.get(Article, hot_id)
        assert db_hot is not None
        meta = dict(db_hot.extra_meta)
        meta["items_sig"] = "sig-evening"
        meta["digest_items"] = [
            {"title": "央行晨间开展逆回购操作投放流动性", "summary": "晨间旧闻应被过滤。", "url": "https://s.weibo.com/m1"},
            {"title": "晚间重磅：工信部发布算力互联互通行动计划", "summary": "工信部晚间正式印发全国算力网互联行动方案。", "url": "https://s.weibo.com/e1"},
            {"title": "晚间快讯：嫦娥八号国际合作载荷名单揭晓", "summary": "国家航天局公布嫦娥八号最新国际科研载荷名单。", "url": "https://s.weibo.com/e2"},
        ]
        db_hot.extra_meta = meta
        await session.commit()

    res_evening = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert res_evening["status"] == "sent"
    async with factory() as session:
        b_eve = await session.get(Book, res_evening["book_id"])
        assert b_eve is not None
        with zipfile.ZipFile(b_eve.kindle_epub_path, "r") as zf:
            ch1_eve = zf.read("OEBPS/ch_001.xhtml").decode("utf-8")
            assert "算力互联互通" in ch1_eve
            assert "嫦娥八号" in ch1_eve
            assert "晨间旧闻" not in ch1_eve

    # P1-10: Archive GC beyond 60 days cleans expired edition_issue Book & disk file, keeps uploaded_book
    old_epub = worker_db / "editions" / "expired_issue.epub"
    old_epub.parent.mkdir(parents=True, exist_ok=True)
    old_epub.write_bytes(b"old-epub")
    user_epub = worker_db / "epubs" / "user_novel.epub"
    user_epub.parent.mkdir(parents=True, exist_ok=True)
    user_epub.write_bytes(b"user-novel")

    async with factory() as session:
        session.add_all(
            [
                Book(
                    kind="edition_issue",
                    title="Expired Issue",
                    original_path=str(old_epub),
                    kindle_epub_path=str(old_epub),
                    created_at=now - timedelta(days=75),
                ),
                Book(
                    kind="uploaded_book",
                    title="User Forever Book",
                    original_path=str(user_epub),
                    kindle_epub_path=str(user_epub),
                    created_at=now - timedelta(days=200),
                ),
            ]
        )
        await session.commit()

    await run_retention_gc(now, archive_days=60)
    assert not old_epub.exists()
    assert user_epub.exists()


async def test_dedup_ledger_refreshes_delivered_at_and_dlq_marks_db_failed(
    worker_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = get_session_factory()
    now = utcnow()
    async with factory() as session:
        src = Source(name="Evergreen", kind="webpage", identifier="https://example.com/evergreen")
        ed = Edition(
            name="Dedup Refresh Ed",
            ai_briefing_enabled=False,
            dedup_window_days=14,
            retention_days=30,
            target_emails="kindle@example.com",
        )
        session.add_all([src, ed])
        await session.flush()
        session.add(EditionSource(edition_id=ed.id, source_id=src.id, sort_order=0))
        art = Article(
            source_id=src.id,
            canonical_url="https://example.com/evergreen",
            content_hash="eg-1",
            title="Evergreen Guide",
            source_name="Evergreen",
            category="科技",
            clean_html="<p>Evergreen article body content that stays updated.</p>",
            plain_text="Evergreen article body content that stays updated.",
            word_count=200,
            published_at=now,
            fetched_at=now,
        )
        session.add(art)
        await session.flush()
        # Delivered 20 days ago (> dedup_window_days=14, < archive_days=60)
        session.add(
            ArticleDelivery(
                edition_id=ed.id,
                article_id=art.id,
                delivered_at=now - timedelta(days=20),
            )
        )
        poison_deliv = Delivery(edition_id=ed.id, title="Poison Edition", status="pending")
        poison_book = Book(
            kind="uploaded_book",
            title="Poison PDF",
            original_path="/tmp/p.pdf",
            conversion_status="converting",
        )
        poison_export_book = Book(
            kind="uploaded_book",
            title="Poison Export Book",
            conversion_status="ready",
            metadata_json={
                "exports": {
                    "pdf_10mb": {
                        "target_format": "pdf",
                        "max_mb": 10.0,
                        "status": "converting",
                    }
                }
            },
        )
        session.add_all([poison_deliv, poison_book, poison_export_book])
        await session.commit()
        ed_id = ed.id
        poison_deliv_id = poison_deliv.id
        poison_book_id = poison_book.id
        poison_exp_book_id = poison_export_book.id

    async def noop_sync(_s: Any, _src: Any) -> dict[str, int]:
        return {"new": 0, "skipped": 0, "failed": 0}

    async def noop_smtp(**_kw: Any) -> None:
        return None

    monkeypatch.setattr("inkpress.worker.tasks.sync_source", noop_sync)
    monkeypatch.setattr("inkpress.worker.tasks.send_ebook_via_smtp", noop_smtp)

    # 1. Re-delivery after 14-day dedup window succeeds ONCE and refreshes delivered_at,
    #    so an immediate subsequent run skips instead of re-sending endlessly!
    r1 = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert r1["status"] == "sent"
    r2 = await run_build_and_deliver_edition(ed_id, trigger="manual", send_email=True)
    assert r2["status"] == "skipped"

    # 2. DLQ eviction transitions linked pending Delivery & converting Book to "failed" in DB!
    class DlqFakeRedis:
        async def xpending_range(self, *_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            return [
                {"message_id": "11-0", "consumer": "w1", "time_since_delivered": 400_000, "times_delivered": 3},
                {"message_id": "12-0", "consumer": "w1", "time_since_delivered": 400_000, "times_delivered": 3},
                {"message_id": "13-0", "consumer": "w1", "time_since_delivered": 400_000, "times_delivered": 3},
            ]

        async def xrange(self, _k: str, min: str, **_kw: Any) -> list[tuple[str, dict[str, str]]]:
            if min == "11-0":
                payload = json.dumps(
                    {"type": "build_and_deliver_edition", "kwargs": {"edition_id": ed_id, "delivery_id": poison_deliv_id}}
                )
            elif min == "12-0":
                payload = json.dumps({"type": "convert_book", "kwargs": {"book_id": poison_book_id}})
            else:
                payload = json.dumps(
                    {"type": "export_book", "kwargs": {"book_id": poison_exp_book_id, "target_format": "pdf", "max_mb": 10}}
                )
            return [(min, {"payload": payload})]

        async def xadd(self, *_a: Any, **_kw: Any) -> str:
            return "dlq-x"

        async def xautoclaim(self, *_a: Any, **_kw: Any) -> tuple[str, list[Any]]:
            return ("0-0", [])

        async def xack(self, *_a: Any, **_kw: Any) -> int:
            return 1

        async def xdel(self, *_a: Any, **_kw: Any) -> int:
            return 1

    evicted = await _reclaim_once(DlqFakeRedis(), "reclaimer-db")  # type: ignore[arg-type]
    assert evicted == 3
    async with factory() as session:
        d_after = await session.get(Delivery, poison_deliv_id)
        b_after = await session.get(Book, poison_book_id)
        exp_b_after = await session.get(Book, poison_exp_book_id)
        assert d_after is not None and d_after.status == "failed" and "DLQ" in (d_after.error_message or "")
        assert b_after is not None and b_after.conversion_status == "failed" and "DLQ" in (b_after.conversion_error or "")
        assert exp_b_after is not None
        exp_state = (exp_b_after.metadata_json or {})["exports"]["pdf_10mb"]
        assert exp_state["status"] == "failed" and "DLQ" in (exp_state.get("error") or "")
