from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

from inkpress.publishing.cover_generator import generate_editorial_cover
from inkpress.publishing.epub_builder import EpubChapterInput, build_kindle_edition_epubs
from inkpress.publishing.epub_validator import validate_epub


async def test_build_zero_e999_epub_and_validate(tmp_path: Path) -> None:
    chapters = [
        EpubChapterInput(
            title="深度观察：中文排版与电子墨水屏",
            source_name="少数派",
            category="科技",
            author="编辑部",
            published_at=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
            canonical_url="https://example.com/article-1",
            body_html=(
                "<p>这是一段包含“中文引号”与长破折号——以及英文混合排版的测试正文。</p>"
                '<img src="" alt="broken-empty-src" />'
                "<blockquote><p>引用段落应正确渲染。</p></blockquote>"
            ),
            ai_summary="本文探讨电子纸排版规范与 Amazon Send to Kindle EPUB3 合规要点。",
        ),
        EpubChapterInput(
            title="Hacker News Daily Picks",
            source_name="Hacker News",
            category="科技",
            author="HN",
            published_at=datetime(2026, 11, 20, 7, 0, tzinfo=UTC),
            canonical_url="https://news.ycombinator.com/item?id=1",
            body_html="<p>Clean XHTML5 ensures Kindle renders typography cleanly.</p>",
            language="en",
        ),
    ]

    volumes = await build_kindle_edition_epubs(
        output_dir=tmp_path,
        edition_title="木兰山晨报",
        subtitle="每日精选与深度阅读",
        issue_number=12,
        issue_date=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
        chapters_input=chapters,
        briefing_html="<p>今日核心看点涵盖 AI 工程、电子纸生态与全球财经动态。</p>",
        include_qr_links=True,
        download_images=False,
    )

    assert len(volumes) == 1
    vol = volumes[0]
    assert vol.path.exists()
    assert vol.article_count == 2
    assert len(vol.cover_bytes) > 1000

    res = validate_epub(vol.path)
    assert res.valid, f"Validation failed: {res.errors}"
    assert res.chapter_count >= 4  # cover + nav + briefing + 2 articles

    with zipfile.ZipFile(vol.path, "r") as zf:
        infos = zf.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert "OEBPS/toc.ncx" in zf.namelist()
        assert "OEBPS/nav.xhtml" in zf.namelist()
        ch1_xml = zf.read("OEBPS/ch_001.xhtml").decode("utf-8")
        assert '<?xml version="1.0" encoding="utf-8"?>' in ch1_xml
        assert "“中文引号”" in ch1_xml
        assert 'src=""' not in ch1_xml


def test_cover_generator_styles() -> None:
    for style in ("editorial", "dark", "minimal"):
        data = generate_editorial_cover(
            title="木兰山晨报",
            subtitle="每日精选",
            issue_number=7,
            headlines=["头条一：全球科技趋势", "头条二：AI 基础设施演进"],
            style=style,
        )
        assert data[:2] == b"\xff\xd8"  # Valid JPEG header
        assert len(data) > 2000
