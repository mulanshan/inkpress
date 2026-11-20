from __future__ import annotations

import zipfile
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from inkpress.delivery.smtp import MAX_ATTACHMENT_BYTES, send_ebook_via_smtp
from inkpress.publishing.cover_generator import (
    FONT_CANDIDATES_SERIF,
    _load_font,
    _measure_width,
    _wrap_text,
    generate_editorial_cover,
)
from inkpress.publishing.epub_builder import (
    KINDLE_PAPER_CSS,
    EpubChapterInput,
    build_kindle_edition_epubs,
)
from inkpress.publishing.epub_validator import validate_epub
from inkpress.publishing.image_pipeline import download_and_optimize_images
from PIL import Image, ImageDraw


def _make_png_bytes(color: tuple[int, int, int] = (200, 40, 40), size: tuple[int, int] = (64, 64)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_build_zero_e999_epub_and_validate(tmp_path: Path) -> None:
    shanghai_tz = timezone(timedelta(hours=8))
    issue_dt = datetime(2026, 11, 20, 6, 30, tzinfo=shanghai_tz)
    chapters = [
        EpubChapterInput(
            title="深度观察：中文排版与电子墨水屏",
            source_name="少数派",
            category="科技",
            author="编辑部",
            published_at=issue_dt,
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

    long_edition_title = "木兰山深度观察与全球科技财经每日精选超长标题测试" * 3
    volumes_a = await build_kindle_edition_epubs(
        output_dir=tmp_path,
        edition_title=long_edition_title,
        subtitle="每日精选与深度阅读",
        issue_number=12,
        issue_date=issue_dt,
        chapters_input=chapters,
        briefing_html="<p>今日核心看点涵盖 AI 工程、电子纸生态与全球财经动态。</p>",
        include_qr_links=True,
        download_images=False,
    )
    volumes_b = await build_kindle_edition_epubs(
        output_dir=tmp_path,
        edition_title=long_edition_title,
        subtitle="每日精选与深度阅读",
        issue_number=12,
        issue_date=issue_dt,
        chapters_input=chapters,
        briefing_html="<p>今日核心看点涵盖 AI 工程、电子纸生态与全球财经动态。</p>",
        include_qr_links=False,
        download_images=False,
    )

    assert len(volumes_a) == 1
    vol = volumes_a[0]
    assert vol.path.exists()
    assert volumes_b[0].path.exists()
    # Same-minute builds must produce distinct filenames and stay well below 255 bytes
    assert vol.path != volumes_b[0].path
    assert len(vol.path.name.encode("utf-8")) < 200
    assert vol.article_count == 2
    assert len(vol.cover_bytes) > 1000

    res = validate_epub(vol.path)
    assert res.valid, f"Validation failed: {res.errors}"
    # cover.xhtml and nav.xhtml are excluded from chapter_count: briefing + 2 articles == 3
    assert res.chapter_count == 3

    # Bilingual translation CSS must not use italic (crisp CJK rendering)
    bilingual_css_block = KINDLE_PAPER_CSS.split("p.bilingual-tr", 1)[1].split("}", 1)[0]
    assert "italic" not in bilingual_css_block

    with zipfile.ZipFile(vol.path, "r") as zf:
        infos = zf.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert "OEBPS/toc.ncx" in zf.namelist()
        assert "OEBPS/nav.xhtml" in zf.namelist()
        opf_xml = zf.read("OEBPS/content.opf").decode("utf-8")
        # 2026-11-20 06:30+08:00 must convert to 2026-11-19T22:30:00Z in OPF
        assert "<dc:date>2026-11-19T22:30:00Z</dc:date>" in opf_xml
        assert 'property="dcterms:modified">2026-11-19T22:30:00Z</meta>' in opf_xml
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


def test_cover_wrap_text_two_lines_no_lone_ellipsis() -> None:
    img = Image.new("RGB", (800, 1200), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _load_font(FONT_CANDIDATES_SERIF, 48)
    max_width = 480

    # 1. Text that naturally wraps onto 2 full lines without overflowing line 2
    two_line_text = "每日精选晨报：全球科技观察与深度分析"
    # Ensure it wraps onto 2 lines under whatever system font is loaded
    while _measure_width(draw, two_line_text, font) <= max_width:
        two_line_text += "测试内容"
    while _measure_width(draw, two_line_text, font) > max_width * 1.7:
        two_line_text = two_line_text[:-1]

    wrapped = _wrap_text(draw, two_line_text, font, max_width, max_lines=2)
    assert len(wrapped) == 2
    assert wrapped[1] != "…"
    assert not wrapped[1].endswith("…")
    assert len(wrapped[1]) >= 2
    assert "".join(wrapped) == two_line_text

    # 2. Very long text exceeding 2 lines: line 2 must be filled + end with "…", NOT lone "…"
    overflow_text = "深度观察：人工智能基础设施、电子墨水屏排版规范与自托管出版工作流的未来十年全景演进趋势报告" * 2
    wrapped_overflow = _wrap_text(draw, overflow_text, font, max_width, max_lines=2)
    assert len(wrapped_overflow) == 2
    assert wrapped_overflow[1].endswith("…")
    assert wrapped_overflow[1] != "…"
    assert len(wrapped_overflow[1]) >= 4
    assert _measure_width(draw, wrapped_overflow[0], font) <= max_width
    assert _measure_width(draw, wrapped_overflow[1], font) <= max_width


async def test_strip_links_true_and_false(tmp_path: Path) -> None:
    chapters = [
        EpubChapterInput(
            title="超链接处理测试一",
            source_name="测试源",
            category="科技",
            author="作者",
            published_at=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
            canonical_url="https://example.com/posts/1",
            body_html=(
                '<p>点击查看 <a href="https://example.org/docs"><strong>官方文档</strong></a> '
                '以及 <a href="/relative/page">相对路径</a> 和 '
                '<a href="javascript:alert(1)">危险链接</a>。</p>'
            ),
        ),
        EpubChapterInput(
            title="超链接处理测试二",
            source_name="测试源",
            category="科技",
            author="作者",
            published_at=datetime(2026, 11, 20, 7, 0, tzinfo=UTC),
            canonical_url="https://example.com/posts/2",
            body_html="<p>第二篇正文。</p>",
        ),
    ]

    # Default strip_links=True: body links unwrapped, inner <strong> & text preserved, .chapter-nav kept
    vols_stripped = await build_kindle_edition_epubs(
        output_dir=tmp_path / "stripped",
        edition_title="无外链刊",
        subtitle="测试",
        issue_number=1,
        issue_date=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
        chapters_input=chapters,
        briefing_html='<p>头版引用 <a href="https://example.org/brief">头版外链</a>。</p>',
        download_images=False,
    )
    with zipfile.ZipFile(vols_stripped[0].path, "r") as zf:
        ch1 = zf.read("OEBPS/ch_001.xhtml").decode("utf-8")
        brief = zf.read("OEBPS/ch_000_briefing.xhtml").decode("utf-8")
        assert "<strong>官方文档</strong>" in ch1
        assert "相对路径" in ch1
        assert "https://example.org/docs" not in ch1
        assert "https://example.org/brief" not in brief
        assert "javascript:" not in ch1
        # Chapter navigation links MUST remain intact
        assert '<a href="nav.xhtml">返回目录</a>' in ch1
        assert '<a href="ch_002.xhtml">下一篇 →</a>' in ch1

    # strip_links=False: valid external & resolved relative links preserved; javascript: still unwrapped
    vols_kept = await build_kindle_edition_epubs(
        output_dir=tmp_path / "kept",
        edition_title="保留外链刊",
        subtitle="测试",
        issue_number=1,
        issue_date=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
        chapters_input=chapters,
        download_images=False,
        strip_links=False,
    )
    assert validate_epub(vols_kept[0].path).valid
    with zipfile.ZipFile(vols_kept[0].path, "r") as zf:
        ch1 = zf.read("OEBPS/ch_001.xhtml").decode("utf-8")
        assert 'href="https://example.org/docs"' in ch1
        assert 'href="https://example.com/relative/page"' in ch1
        assert "javascript:" not in ch1


async def test_multi_volume_epub_zero_broken_links(tmp_path: Path) -> None:
    # Chapters ~280 KB UTF-8 each: with max_mb_per_volume=1 (~624 KB budget),
    # Vol 1 gets 2 chapters and Vol 2 gets 1 chapter so we test both intra-volume
    # and cross-volume boundary .chapter-nav links!
    big_para = "<p>" + ("多卷分册测试正文内容，用于验证自动分卷与卷内导航完整性。" * 3600) + "</p>"
    chapters = [
        EpubChapterInput(
            title=f"长文章第 {i} 篇",
            source_name="长文周刊",
            category="深度",
            author="编辑部",
            published_at=datetime(2026, 11, 20, 6 + i, 0, tzinfo=UTC),
            canonical_url=f"https://example.com/long/{i}",
            body_html=big_para,
        )
        for i in range(1, 4)
    ]

    volumes = await build_kindle_edition_epubs(
        output_dir=tmp_path,
        edition_title="多卷测试刊",
        subtitle="自动分卷",
        issue_number=3,
        issue_date=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
        chapters_input=chapters,
        max_mb_per_volume=1,
        download_images=False,
    )

    assert len(volumes) >= 2
    for vol in volumes:
        val = validate_epub(vol.path)
        assert val.valid, f"Volume {vol.volume_index} failed validation: {val.errors}"
        with zipfile.ZipFile(vol.path, "r") as zf:
            ch_names = sorted(
                n for n in zf.namelist() if n.startswith("OEBPS/ch_") and n.endswith(".xhtml")
            )
            assert ch_names
            for i, ch_name in enumerate(ch_names[:-1]):
                ch_xml = zf.read(ch_name).decode("utf-8")
                next_base = ch_names[i + 1].split("/")[-1]
                assert f'<a href="{next_base}">下一篇 →</a>' in ch_xml
            last_ch_xml = zf.read(ch_names[-1]).decode("utf-8")
            assert "下一篇 →" not in last_ch_xml
            assert '<a href="nav.xhtml">返回目录</a>' in last_ch_xml


async def test_epub_validator_catches_broken_internal_links_and_manifest_errors(
    tmp_path: Path,
) -> None:
    # Build a valid EPUB first, then inject a broken cross-volume link and missing image
    vols = await build_kindle_edition_epubs(
        output_dir=tmp_path,
        edition_title="校验基准",
        subtitle="测试",
        issue_number=1,
        issue_date=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
        chapters_input=[
            EpubChapterInput(
                title="章节一",
                source_name="源",
                category="科技",
                author="作者",
                published_at=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
                canonical_url="https://example.com/1",
                body_html="<p>测试正文。</p>",
            )
        ],
        download_images=False,
    )
    base_epub = vols[0].path
    assert validate_epub(base_epub).valid

    broken_epub = tmp_path / "broken_links.epub"
    with zipfile.ZipFile(base_epub, "r") as src_zf, zipfile.ZipFile(broken_epub, "w") as dst_zf:
        dst_zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in src_zf.infolist():
            if info.filename == "mimetype":
                continue
            data = src_zf.read(info.filename)
            if info.filename == "OEBPS/ch_001.xhtml":
                text = data.decode("utf-8").replace(
                    "</body>",
                    '<p><a href="ch_999.xhtml">下一篇 →</a><img src="images/ghost.jpg" alt="x"/></p></body>',
                )
                data = text.encode("utf-8")
            elif info.filename == "OEBPS/content.opf":
                text = data.decode("utf-8").replace(
                    "</spine>",
                    '    <itemref idref="nonexistent-item"/>\n  </spine>',
                )
                data = text.encode("utf-8")
            dst_zf.writestr(info.filename, data, compress_type=zipfile.ZIP_DEFLATED)

    res = validate_epub(broken_epub)
    assert not res.valid
    joined = "\n".join(res.errors)
    assert "ch_999.xhtml" in joined
    assert "images/ghost.jpg" in joined
    assert "nonexistent-item" in joined


async def test_image_pipeline_per_url_referer_403_retry_and_per_chapter_cap(
    tmp_path: Path,
) -> None:
    png_bytes = _make_png_bytes((30, 140, 220))
    calls: list[tuple[str, str]] = []

    async def fake_fetch_bytes(
        url: str,
        *,
        outbound_proxy: str = "",
        referer: str = "",
        timeout: float = 15.0,
        **kwargs: Any,
    ) -> tuple[bytes, str]:
        calls.append((url, referer))
        req = httpx.Request("GET", url)
        # sspai CDN requires Referer
        if "cdnfile.sspai.com" in url and referer != "https://sspai.com/post/123":
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
        # strict-noref CDN rejects Referer (403) and succeeds when retried without Referer
        if "strict-noref.example.com" in url and referer:
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
        return png_bytes, "image/png"

    with patch("inkpress.publishing.image_pipeline.smart_fetch_bytes", side_effect=fake_fetch_bytes):
        res = await download_and_optimize_images(
            [
                ("https://cdnfile.sspai.com/pic1.png", "https://sspai.com/post/123"),
                ("https://strict-noref.example.com/pic2.png", "https://blog.example.com/post/1"),
            ]
        )
        assert "https://cdnfile.sspai.com/pic1.png" in res
        assert "https://strict-noref.example.com/pic2.png" in res
        # Verify strict-noref was tried WITH referer first, then retried WITHOUT referer
        noref_calls = [ref for u, ref in calls if "strict-noref.example.com" in u]
        assert noref_calls == ["https://blog.example.com/post/1", ""]

        # Verify per-chapter cap (20/article) in build_kindle_edition_epubs prevents starvation
        calls.clear()
        ch1_imgs = "".join(
            f'<img src="https://cdnfile.sspai.com/ch1_{i}.png" alt="i"/>' for i in range(30)
        )
        ch2_imgs = '<img src="https://cdnfile.sspai.com/ch2_0.png" alt="c2"/>'
        vols = await build_kindle_edition_epubs(
            output_dir=tmp_path,
            edition_title="配额测试",
            subtitle="图集",
            issue_number=1,
            issue_date=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
            chapters_input=[
                EpubChapterInput(
                    title="多图文章",
                    source_name="少数派",
                    category="科技",
                    author="作者",
                    published_at=datetime(2026, 11, 20, 6, 0, tzinfo=UTC),
                    canonical_url="https://sspai.com/post/123",
                    body_html=ch1_imgs,
                ),
                EpubChapterInput(
                    title="后续文章",
                    source_name="少数派",
                    category="科技",
                    author="作者",
                    published_at=datetime(2026, 11, 20, 7, 0, tzinfo=UTC),
                    canonical_url="https://sspai.com/post/123",
                    body_html=ch2_imgs,
                ),
            ],
            download_images=True,
        )
        val = validate_epub(vols[0].path)
        assert val.valid, val.errors
        # 20 from ch1 + 1 from ch2 + 1 cover = 22 images (ch1 capped at 20, ch2 not starved!)
        assert val.image_count == 22


async def test_smtp_limit_msgid_and_plain_port25(tmp_path: Path) -> None:
    assert MAX_ATTACHMENT_BYTES == 18 * 1024 * 1024

    big_file = tmp_path / "huge.epub"
    big_file.write_bytes(b"0" * (MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(ValueError, match="18 MB"):
        await send_ebook_via_smtp(
            smtp_config={"host": "relay.local", "port": 25, "from_email": "bot@example.com"},
            recipients=["kindle@kindle.com"],
            subject="Test",
            body_text="Body",
            attachments=[big_file],
        )

    small_file = tmp_path / "book.epub"
    small_file.write_bytes(b"epub-bytes")

    mock_send = AsyncMock()
    with patch("inkpress.delivery.smtp.aiosmtplib.send", mock_send):
        # Plain LAN relay on port 25 with display-name From header
        await send_ebook_via_smtp(
            smtp_config={
                "host": "relay.local",
                "port": 25,
                "use_ssl": False,
                "from_email": "InkPress Studio <bot@example.org>",
            },
            recipients=["reader@kindle.com"],
            subject="LAN Relay Test",
            body_text="Hello",
            attachments=[small_file],
        )
        sent_msg = mock_send.call_args.args[0]
        sent_kwargs = mock_send.call_args.kwargs
        assert sent_msg["Message-ID"].endswith("@example.org>")
        assert ">>" not in sent_msg["Message-ID"]
        assert sent_kwargs["use_tls"] is False
        assert sent_kwargs["start_tls"] is False

        # Port 587 with use_ssl=False should still enable STARTTLS
        mock_send.reset_mock()
        await send_ebook_via_smtp(
            smtp_config={
                "host": "smtp.example.com",
                "port": 587,
                "use_ssl": False,
                "from_email": "bot@example.org",
            },
            recipients=["reader@kindle.com"],
            subject="STARTTLS Test",
            body_text="Hello",
            attachments=[small_file],
        )
        assert mock_send.call_args.kwargs["use_tls"] is False
        assert mock_send.call_args.kwargs["start_tls"] is True
