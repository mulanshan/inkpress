from __future__ import annotations

import asyncio
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import docx
import pymupdf
import pytest
from inkpress.publishing.book_converter import (
    SUPPORTED_BOOK_FORMATS,
    _render_scanned_page_1bit,
    _run_calibre_convert,
    compress_pdf_file,
    convert_book_to_kindle_epub,
    convert_epub_to_pdf,
    convert_pdf_to_epub_with_budget,
    extract_book_to_txt,
    list_epub_chapters,
    normalize_epub_for_kindle,
    read_epub_chapter_html,
    recompress_epub_to_budget,
)
from inkpress.publishing.epub_validator import MISSING_NCX_ERROR, validate_epub
from PIL import Image


def _make_png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (80, 80)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _create_epub3_without_ncx(path: Path) -> None:
    """Simulate Calibre 8.5.0 / Pandoc EPUB3 output: valid EPUB3 except missing toc.ncx."""
    container_xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        b"  <rootfiles>\n"
        b'    <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>\n'
        b"  </rootfiles>\n"
        b"</container>\n"
    )
    content_opf = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
        b'  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        b'    <dc:identifier id="uid">urn:uuid:calibre-epub3-demo</dc:identifier>\n'
        b"    <dc:title>Calibre EPUB3 Book</dc:title>\n"
        b"    <dc:creator>Author A</dc:creator>\n"
        b"    <dc:language>zh-CN</dc:language>\n"
        b"  </metadata>\n"
        b"  <manifest>\n"
        b'    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        b'    <item id="css" href="styles/custom.css" media-type="text/css"/>\n'
        b'    <item id="ch1" href="Text/discovery.xhtml" media-type="application/xhtml+xml"/>\n'
        b'    <item id="ch2" href="Text/chapter_two.xhtml" media-type="application/xhtml+xml"/>\n'
        b"  </manifest>\n"
        b"  <spine>\n"
        b'    <itemref idref="ch1"/>\n'
        b'    <itemref idref="ch2"/>\n'
        b"  </spine>\n"
        b"</package>\n"
    )
    nav_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head><meta charset="utf-8"/><title>目录</title></head>\n'
        '<body><nav epub:type="toc"><ol>\n'
        '  <li><a href="Text/discovery.xhtml#top">发现之旅</a></li>\n'
        '  <li><a href="Text/chapter_two.xhtml">第二章 深入原理</a></li>\n'
        "</ol></nav></body></html>\n"
    ).encode()
    # Intentionally set Pandoc-like <title>ch001.xhtml</title> while <body> has <h1>发现之旅</h1>
    ch1_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><meta charset="utf-8"/><title>ch001.xhtml</title>'
        '<link rel="stylesheet" href="../styles/custom.css"/></head>\n'
        '<body><h1 id="top">发现之旅</h1><p>保留原始 CSS 与章节结构。</p></body></html>\n'
    ).encode()
    ch2_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><meta charset="utf-8"/><title>ch002.xhtml</title></head>\n'
        "<body><h2>第二章 深入原理</h2><p>次节内容。</p></body></html>\n"
    ).encode()

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/content.opf", content_opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/styles/custom.css", b"body { color: #222; }", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/Text/discovery.xhtml", ch1_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("EPUB/Text/chapter_two.xhtml", ch2_xhtml, compress_type=zipfile.ZIP_DEFLATED)


async def test_convert_txt_with_chinese_chapters(tmp_path: Path) -> None:
    src = tmp_path / "sample_novel.txt"
    src.write_text(
        "第一章 初遇\n\n这是第一章的正文内容，包含多个自然段。\n\n第二章 远行\n\n这是第二章的正文内容。",
        encoding="utf-8",
    )
    res = await convert_book_to_kindle_epub(
        src,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
        title_override="测试长篇小说",
        author_override="测试作者",
    )
    assert res.kindle_epub_path.exists()
    assert res.chapter_count == 2
    assert res.cover_path is not None and res.cover_path.exists()

    val = validate_epub(res.kindle_epub_path)
    assert val.valid, f"Converted TXT EPUB invalid: {val.errors}"

    chapters = list_epub_chapters(res.kindle_epub_path)
    assert len(chapters) == 2
    ch0 = read_epub_chapter_html(res.kindle_epub_path, 0, book_id=1)
    assert "第一章" in str(ch0["html"])


async def test_convert_markdown_multi_chapter_splitting(tmp_path: Path) -> None:
    src = tmp_path / "essay.md"
    src.write_text(
        "# 技术随笔\n\n导语段落。\n\n"
        "## 第一节 架构\n\n这是一篇 **Markdown** 文章。\n\n"
        "```python\n# not a heading inside code fence\nprint('ok')\n```\n\n"
        "## 第二节 实践\n\n- 要点 A\n- 要点 B\n",
        encoding="utf-8",
    )
    res = await convert_book_to_kindle_epub(
        src,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
    )
    val = validate_epub(res.kindle_epub_path)
    assert val.valid, f"Converted MD EPUB invalid: {val.errors}"
    assert res.chapter_count == 3
    chapters = list_epub_chapters(res.kindle_epub_path)
    assert [c["title"] for c in chapters] == ["技术随笔", "第一节 架构", "第二节 实践"]


async def test_repair_malformed_epub(tmp_path: Path) -> None:
    broken = tmp_path / "broken.epub"
    # Intentionally break mimetype ordering, missing UTF-8 header, empty <img>, missing toc.ncx
    with zipfile.ZipFile(broken, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/container.xml", "<container/>")
        zf.writestr(
            "OEBPS/ch1.html",
            '<html><body><h1>坏文件测试</h1><p>正文内容</p><img src=""/></body></html>',
        )
        zf.writestr("mimetype", "application/epub+zip")

    pre = validate_epub(broken)
    assert not pre.valid

    repaired = await convert_book_to_kindle_epub(
        broken,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
        title_override="修复后的电子书",
    )
    post = validate_epub(repaired.kindle_epub_path)
    assert post.valid, f"Repaired EPUB still invalid: {post.errors}"
    assert "Repaired EPUB" in repaired.notes


async def test_inplace_toc_ncx_injection_preserves_epub3_structure(tmp_path: Path) -> None:
    src = tmp_path / "calibre_out.epub"
    _create_epub3_without_ncx(src)

    pre = validate_epub(src)
    assert pre.errors == [MISSING_NCX_ERROR]

    res = await convert_book_to_kindle_epub(
        src,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
    )
    assert "in-place" in res.notes.lower()
    assert res.title == "Calibre EPUB3 Book"
    assert res.author == "Author A"
    assert res.chapter_count == 2

    post = validate_epub(res.kindle_epub_path)
    assert post.valid, f"Patched EPUB failed validation: {post.errors}"

    with zipfile.ZipFile(res.kindle_epub_path, "r") as zf:
        names = set(zf.namelist())
        # Original custom CSS and original chapter path MUST be preserved untouched!
        assert "EPUB/styles/custom.css" in names
        assert "EPUB/Text/discovery.xhtml" in names
        assert "EPUB/toc.ncx" in names
        ncx_text = zf.read("EPUB/toc.ncx").decode("utf-8")
        assert "发现之旅" in ncx_text
        assert 'src="Text/discovery.xhtml#top"' in ncx_text

    # list_epub_chapters must NOT drop discovery.xhtml and must prefer <h1> over <title>ch001.xhtml
    chapters = list_epub_chapters(res.kindle_epub_path)
    assert len(chapters) == 2
    assert chapters[0]["filename"] == "EPUB/Text/discovery.xhtml"
    assert chapters[0]["title"] == "发现之旅"
    assert chapters[1]["title"] == "第二章 深入原理"


async def test_read_epub_chapter_html_xss_stripping_links_and_non_epub(tmp_path: Path) -> None:
    epub_path = tmp_path / "xss_reader.epub"
    img_bytes = _make_png_bytes((20, 120, 200))

    container_xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        b'  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        b"</container>\n"
    )
    opf = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
        b'  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        b'    <dc:identifier id="uid">urn:uuid:xss-test</dc:identifier>\n'
        b"    <dc:title>XSS Reader Test</dc:title><dc:language>zh-CN</dc:language>\n"
        b"  </metadata>\n"
        b"  <manifest>\n"
        b'    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        b'    <item id="c1" href="Text/ch_001.xhtml" media-type="application/xhtml+xml"/>\n'
        b'    <item id="c2" href="Text/ch_002.xhtml" media-type="application/xhtml+xml"/>\n'
        b'    <item id="p1" href="images/my%20pic.png" media-type="image/png"/>\n'
        b"  </manifest>\n"
        b'  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>\n'
        b"</package>\n"
    )
    ch1 = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/></head><body>'
        "<h1>安全测试章节</h1>"
        '<script>alert("stored-xss")</script>'
        '<img src="../images/my%20pic.png" alt="配图" onerror="alert(1)"/>'
        '<a href="javascript:alert(2)">恶意链接</a>'
        '<a href="ch_002.xhtml">跳转到第二章</a>'
        '<div class="chapter-nav"><a href="../nav.xhtml">返回目录</a></div>'
        "</body></html>"
    ).encode()
    ch2 = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8"/></head>'
        "<body><h1>第二章</h1><p>OK</p></body></html>"
    ).encode()

    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", b"<html/>")
        zf.writestr("OEBPS/Text/ch_001.xhtml", ch1)
        zf.writestr("OEBPS/Text/ch_002.xhtml", ch2)
        zf.writestr("OEBPS/images/my pic.png", img_bytes)

    res = read_epub_chapter_html(epub_path, 0, book_id=42)
    html_out = str(res["html"])
    assert "<script" not in html_out.lower()
    assert "stored-xss" not in html_out
    assert "onerror" not in html_out.lower()
    assert "javascript:" not in html_out.lower()
    assert "chapter-nav" not in html_out
    assert "/api/v1/books/42/asset/OEBPS/images/my%20pic.png" in html_out
    assert 'data-chapter-index="1"' in html_out

    # Non-existent, non-EPUB (.pdf), and corrupted files must raise ValueError cleanly
    with pytest.raises(ValueError):
        read_epub_chapter_html(tmp_path / "missing.epub", 0, book_id=1)
    fake_pdf = tmp_path / "doc.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError):
        read_epub_chapter_html(fake_pdf, 0, book_id=1)
    corrupt_epub = tmp_path / "corrupt.epub"
    corrupt_epub.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        read_epub_chapter_html(corrupt_epub, 0, book_id=1)


def test_rebuild_prevents_data_src_hijack_duplicate_h1_collision_and_epub2_cover(
    tmp_path: Path,
) -> None:
    src = tmp_path / "malformed_complex.epub"
    red_png = _make_png_bytes((220, 20, 20), (80, 80))
    blue_png = _make_png_bytes((20, 40, 220), (80, 80))
    cover_png = _make_png_bytes((50, 180, 90), (200, 300))

    # Intentionally omit mimetype-first and toc.ncx so normalize_epub_for_kindle runs rebuild
    container_xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        b'  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        b"</container>\n"
    )
    opf = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">\n'
        b'  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        b'    <dc:identifier id="uid">urn:uuid:epub2-rebuild</dc:identifier>\n'
        b"    <dc:title>EPUB2 Rebuild Test</dc:title><dc:language>zh-CN</dc:language>\n"
        b'    <meta name="cover" content="cov-img"/>\n'
        b"  </metadata>\n"
        b"  <manifest>\n"
        b'    <item id="cov-img" href="images/front.png" media-type="image/png"/>\n'
        b'    <item id="ch1" href="ch1/discovery.html" media-type="application/xhtml+xml"/>\n'
        b'    <item id="ch2" href="ch2/gbk_page.html" media-type="application/xhtml+xml"/>\n'
        b'    <item id="img1" href="ch1/fig.png" media-type="image/png"/>\n'
        b'    <item id="img2" href="ch2/fig.png" media-type="image/png"/>\n'
        b"  </manifest>\n"
        b"  <spine>\n"
        b'    <itemref idref="ch1"/>\n'
        b'    <itemref idref="ch2"/>\n'
        b"  </spine>\n"
        b"</package>\n"
    )
    # Chapter 1: already starts with <h1> + has image with external data-src hijack attempt
    ch1_html = (
        "<html><head><title>ch001.html</title></head><body>"
        "<h1>已有主标题</h1>"
        '<p>正文段落 <img src="fig.png" data-src="https://evil.example/hijack.jpg" '
        'data-original="https://evil.example/orig.jpg" alt="red"/></p>'
        "</body></html>"
    ).encode()
    # Chapter 2: GB18030 encoded + same basename fig.png (blue) in ch2/
    ch2_html = (
        '<html><head><meta charset="gb18030"/></head><body>'
        "<h1>简体中文编码测试</h1>"
        '<p>这是 GB18030 编码章节正文。<img src="fig.png" alt="blue"/></p>'
        "</body></html>"
    ).encode("gb18030")

    with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/images/front.png", cover_png)
        zf.writestr("OEBPS/ch1/discovery.html", ch1_html)
        zf.writestr("OEBPS/ch2/gbk_page.html", ch2_html)
        zf.writestr("OEBPS/ch1/fig.png", red_png)
        zf.writestr("OEBPS/ch2/fig.png", blue_png)

    dst = tmp_path / "rebuilt.epub"
    cover_out = tmp_path / "cover.jpg"
    res = normalize_epub_for_kindle(src, dst, cover_out)

    val = validate_epub(dst)
    assert val.valid, f"Rebuilt EPUB failed validation: {val.errors}"
    assert res.chapter_count == 2
    assert res.cover_path == cover_out and cover_out.exists()

    # Check EPUB2 cover was actually extracted (green image ~50,180,90)
    with Image.open(cover_out) as cov_img:
        r, g, b = cov_img.convert("RGB").getpixel((cov_img.width // 2, cov_img.height // 2))
        assert g > r and g > b

    with zipfile.ZipFile(dst, "r") as zf:
        ch1_out = zf.read("OEBPS/ch_0001.xhtml").decode("utf-8")
        ch2_out = zf.read("OEBPS/ch_0002.xhtml").decode("utf-8")
        # 1. No data-src hijack! Local rebuilt image preserved!
        assert "evil.example" not in ch1_out
        assert 'src="images/rebuilt_001_01.jpg"' in ch1_out
        # 2. No duplicate <h1>!
        assert ch1_out.lower().count("<h1") == 1
        assert ch2_out.lower().count("<h1") == 1
        # 3. GB18030 decoded cleanly!
        assert "简体中文编码测试" in ch2_out
        assert "这是 GB18030 编码章节正文" in ch2_out
        # 4. Same-basename fig.png images did NOT collide (red in ch1, blue in ch2)
        img1 = Image.open(BytesIO(zf.read("OEBPS/images/rebuilt_001_01.jpg"))).convert("RGB")
        img2 = Image.open(BytesIO(zf.read("OEBPS/images/rebuilt_002_01.jpg"))).convert("RGB")
        r1, _g1, b1 = img1.getpixel((10, 10))
        r2, _g2, b2 = img2.getpixel((10, 10))
        assert r1 > 180 and b1 < 80
        assert b2 > 180 and r2 < 80


async def test_pdf_encrypted_rejected_and_reflow_with_toc_image_and_cover(tmp_path: Path) -> None:
    # 1. Encrypted PDF must raise ValueError with friendly message
    enc_pdf = tmp_path / "locked.pdf"
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Secret content")
    doc.save(
        str(enc_pdf),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="secret",
        owner_pw="owner",
    )
    doc.close()

    with pytest.raises(ValueError, match="密码"):
        await convert_book_to_kindle_epub(
            enc_pdf,
            output_dir=tmp_path / "epubs",
            cover_dir=tmp_path / "covers",
        )

    # 2. Multi-page text PDF with repeating headers/footers, wrapped lines, embedded image, & TOC
    text_pdf = tmp_path / "journal.pdf"
    img_bytes = _make_png_bytes((240, 160, 20), (100, 100))
    doc = pymupdf.open()
    doc.set_metadata({"title": "Reflow Journal", "author": "Dr. Test"})

    for page_num in range(1, 4):
        page = doc.new_page(width=595, height=842)
        if page_num == 1:
            # Paint a solid dark-blue banner on page 1 so we verify page-1 thumbnail cover is preserved
            page.draw_rect(pymupdf.Rect(0, 0, 595, 180), color=(0.1, 0.2, 0.7), fill=(0.1, 0.2, 0.7))
        # Repeating header in top 10% band (y <= 84)
        page.insert_text((72, 40), "InkPress Technical Journal Header", fontsize=10)
        # Wrapped multi-line paragraph in body
        body_rect = pymupdf.Rect(72, 200, 520, 420)
        page.insert_textbox(
            body_rect,
            (
                f"Page {page_num} discusses modern digital publishing pipelines and how e-ink "
                "devices reflow long technical paragraphs smoothly across screens without breaking "
                "individual visual lines into separate paragraph elements."
            ),
            fontsize=12,
        )
        if page_num == 2:
            page.insert_image(pymupdf.Rect(72, 440, 220, 588), stream=img_bytes)
        # Repeating footer in bottom 10% band (y >= 758)
        page.insert_text((260, 810), f"Page {page_num}", fontsize=10)

    doc.set_toc([[1, "Part One: Foundations", 1], [1, "Part Two: Practice", 2]])
    doc.save(str(text_pdf))
    doc.close()

    res = await convert_book_to_kindle_epub(
        text_pdf,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
    )
    assert res.kindle_epub_path.suffix == ".epub"
    assert res.title == "Reflow Journal"
    assert res.author == "Dr. Test"
    assert res.chapter_count == 2
    assert res.cover_path is not None and res.cover_path.exists()

    val = validate_epub(res.kindle_epub_path)
    assert val.valid, f"Reflowed PDF EPUB invalid: {val.errors}"
    assert val.image_count >= 2  # cover + embedded image from page 2

    # Verify page-1 rendered cover thumbnail was preserved (top pixel is blue, NOT cream editorial bg)
    with Image.open(res.cover_path) as cov:
        cr, cg, cb = cov.convert("RGB").getpixel((cov.width // 2, 20))
        assert cb > cr + 40 and cb > cg + 20

    with zipfile.ZipFile(res.kindle_epub_path, "r") as zf:
        ch1 = zf.read("OEBPS/ch_0001.xhtml").decode("utf-8")
        ch2 = zf.read("OEBPS/ch_0002.xhtml").decode("utf-8")
        # Repeating headers/footers stripped
        assert "InkPress Technical Journal Header" not in ch1
        assert "InkPress Technical Journal Header" not in ch2
        # Wrapped lines merged into a real paragraph containing full sentence
        assert "modern digital publishing pipelines and how e-ink" in ch1
        assert "without breaking individual visual lines" in ch1
        # Embedded image referenced in chapter 2
        assert "images/pdf_img_" in ch2

    # 3. Scanned / sparse PDF (3 pages with tiny text) must preserve native PDF
    scanned_pdf = tmp_path / "scanned.pdf"
    sdoc = pymupdf.open()
    for _ in range(3):
        sp = sdoc.new_page()
        sp.insert_text((72, 100), "Hi")
    sdoc.save(str(scanned_pdf))
    sdoc.close()

    scan_res = await convert_book_to_kindle_epub(
        scanned_pdf,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
    )
    assert scan_res.kindle_epub_path.suffix == ".pdf"


async def test_cbr_rejected_calibre_args_timeout_and_docx_conversion(tmp_path: Path) -> None:
    assert "cbr" not in SUPPORTED_BOOK_FORMATS
    cbr_file = tmp_path / "comic.cbr"
    cbr_file.write_bytes(b"Rar!")
    with pytest.raises(ValueError, match="Unsupported book format"):
        await convert_book_to_kindle_epub(
            cbr_file,
            output_dir=tmp_path / "epubs",
            cover_dir=tmp_path / "covers",
        )

    # Verify _run_calibre_convert omits --embed-all-fonts=false and kills process on timeout
    recorded_cmds: list[tuple[Any, ...]] = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock(return_value=-9)

    async def fake_exec(*cmd: Any, **kwargs: Any) -> Any:
        recorded_cmds.append(cmd)
        # Create dummy dst file when not timing out
        Path(cmd[2]).write_bytes(b"ok")
        return fake_proc

    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await _run_calibre_convert(tmp_path / "in.mobi", tmp_path / "out.epub")
    assert "--embed-all-fonts=false" not in recorded_cmds[0]
    assert "--epub-version=3" in recorded_cmds[0]

    # Timeout path must kill and reap child process
    async def hang_comm() -> tuple[bytes, bytes]:
        await asyncio.sleep(5)
        return b"", b""

    async def fake_wait_for(coro: Any, timeout: float) -> Any:
        coro.close()
        raise TimeoutError

    fake_proc.communicate = hang_comm
    with (
        patch("asyncio.create_subprocess_exec", return_value=fake_proc),
        patch("asyncio.wait_for", side_effect=fake_wait_for),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        await _run_calibre_convert(tmp_path / "in.mobi", tmp_path / "out2.epub")
    fake_proc.kill.assert_called_once()
    fake_proc.wait.assert_awaited_once()

    # DOCX conversion fallback (python-docx) produces valid EPUB3
    docx_file = tmp_path / "notes.docx"
    d = docx.Document()
    d.add_heading("第一章 概要", level=1)
    d.add_paragraph("这是 DOCX 文档正文内容。")
    d.save(str(docx_file))

    with patch("inkpress.publishing.book_converter._has_binary", return_value=False):
        docx_res = await convert_book_to_kindle_epub(
            docx_file,
            output_dir=tmp_path / "epubs",
            cover_dir=tmp_path / "covers",
        )
    assert validate_epub(docx_res.kindle_epub_path).valid

    # DOCX via Pandoc path uses pandoc -> normalize_epub_for_kindle (in-place NCX)
    async def fake_pandoc(src_p: Path, dst_p: Path, title: str, author: str = "") -> None:
        _create_epub3_without_ncx(dst_p)

    with (
        patch("inkpress.publishing.book_converter._has_binary", return_value=True),
        patch("inkpress.publishing.book_converter._run_pandoc_docx_to_epub", side_effect=fake_pandoc),
    ):
        pandoc_res = await convert_book_to_kindle_epub(
            docx_file,
            output_dir=tmp_path / "epubs",
            cover_dir=tmp_path / "covers",
        )
    assert "Pandoc" in pandoc_res.notes
    assert "in-place" in pandoc_res.notes.lower()
    assert validate_epub(pandoc_res.kindle_epub_path).valid


def _make_noisy_scan_png(seed: int, size: tuple[int, int] = (900, 1260)) -> bytes:
    """Create a realistic noisy scanned magazine page PNG (uncompressed ~1MB, JPEG ~250KB)."""
    img = Image.new("L", size, 238)
    px = img.load()
    assert px is not None
    w, h = size
    # Simulate paper grain + horizontal lines of dark strokes
    for y in range(40, h - 40, 22):
        for x in range(50, w - 50):
            if ((x * 13 + y * 7 + seed) % 19) < 8:
                px[x, y] = 25
                if y + 1 < h:
                    px[x, y + 1] = 35
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            px[x, y] = max(0, min(255, int(px[x, y]) - ((x * 31 + y * 17 + seed) % 28)))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def test_scanned_and_text_pdf_compression_and_scanned_pdf_to_epub(tmp_path: Path) -> None:
    # 1. Multi-page noisy scanned PDF (with RGB cover + 30 pages across 2 malformed-level bookmarks)
    scanned_pdf = tmp_path / "magazine_scan.pdf"
    sdoc = pymupdf.open()
    cov_page = sdoc.new_page(width=540, height=745)
    cov_page.draw_rect(pymupdf.Rect(0, 0, 540, 745), color=(0.8, 0.2, 0.1), fill=(0.8, 0.2, 0.1))
    page_jpg_a = _make_noisy_scan_png(1)
    page_jpg_b = _make_noisy_scan_png(2)
    for idx in range(29):
        pg = sdoc.new_page(width=540, height=745)
        pg.insert_image(pg.rect, stream=page_jpg_a if idx % 2 == 0 else page_jpg_b)
    sdoc.set_metadata({"title": "1991合订本", "author": "编辑部"})
    sdoc.set_toc([[1, "第一期", 1], [1, "第二期", 28]])
    sdoc.save(str(scanned_pdf))
    sdoc.close()

    orig_size = scanned_pdf.stat().st_size
    assert orig_size > 400_000

    # Compress scanned PDF strictly under 1.0 MB while preserving page count & TOC bookmarks
    compact_pdf = tmp_path / "compact.pdf"
    comp_res = compress_pdf_file(scanned_pdf, compact_pdf, max_mb=1.0)
    assert compact_pdf.exists()
    assert comp_res.file_size_bytes <= 1 * 1024 * 1024
    assert comp_res.file_size_bytes < orig_size
    with pymupdf.open(str(compact_pdf)) as out_doc:
        assert len(out_doc) == 30
        toc = out_doc.get_toc()
        assert len(toc) == 2
        assert toc[0][1] == "第一期"

    # Convert 30-page 0-text scanned PDF into Kindle-valid EPUB3 (with automatic <=25p TOC chunking)
    scanned_epub = tmp_path / "scan_out.epub"
    scan_cov = tmp_path / "scan_cov.jpg"
    epub_res = convert_pdf_to_epub_with_budget(
        scanned_pdf,
        scanned_epub,
        scan_cov,
        max_mb=1.0,
    )
    assert scanned_epub.exists()
    assert epub_res.file_size_bytes <= 1 * 1024 * 1024
    val = validate_epub(scanned_epub)
    assert val.valid, f"Scanned PDF -> EPUB failed validation: {val.errors}"
    # 27 pages in bookmark 1 (>25 -> split into 2 chapters) + 3 pages in bookmark 2 = 3 chapters
    chapters = list_epub_chapters(scanned_epub)
    assert len(chapters) == 3
    assert "第一期 (1/2)" in str(chapters[0]["title"])
    ch0_html = str(read_epub_chapter_html(scanned_epub, 0, book_id=9)["html"])
    assert "/api/v1/books/9/asset/OEBPS/images/page_0001.png" in ch0_html

    # 0-text scanned PDF -> TXT must raise friendly ValueError instead of writing empty file
    with pytest.raises(ValueError, match="文字层"):
        extract_book_to_txt(scanned_pdf, tmp_path / "should_fail.txt")

    # 2. Vector text PDF with bloated embedded photo -> preserves selectable text while shrinking
    bloated_text_pdf = tmp_path / "bloated_text.pdf"
    big_rgb = Image.effect_noise((1600, 1200), 80.0).convert("RGB")
    big_buf = BytesIO()
    big_rgb.save(big_buf, format="PNG")
    tdoc = pymupdf.open()
    for pno in range(1, 4):
        tp = tdoc.new_page(width=595, height=842)
        tp.insert_textbox(
            pymupdf.Rect(60, 60, 535, 320),
            (
                f"Chapter {pno}: Selectable vector text must remain searchable after compression. "
                * 8
            ),
            fontsize=11,
        )
        if pno == 1:
            tp.insert_image(pymupdf.Rect(60, 340, 500, 680), stream=big_buf.getvalue())
    tdoc.save(str(bloated_text_pdf))
    tdoc.close()
    assert bloated_text_pdf.stat().st_size > 1_000_000

    shrunk_text_pdf = tmp_path / "shrunk_text.pdf"
    t_comp = compress_pdf_file(bloated_text_pdf, shrunk_text_pdf, max_mb=1.0)
    assert t_comp.file_size_bytes <= 1 * 1024 * 1024
    assert t_comp.file_size_bytes < bloated_text_pdf.stat().st_size // 2
    with pymupdf.open(str(shrunk_text_pdf)) as check_tdoc:
        assert "Selectable vector text must remain searchable" in check_tdoc[0].get_text()


async def test_epub_to_pdf_recompress_txt_and_huge_mediabox_guard(tmp_path: Path) -> None:
    # 1. Build a rich CJK EPUB with embedded illustration, then export to PDF and TXT
    noise_jpg_buf = BytesIO()
    Image.effect_noise((1200, 900), 65.0).convert("RGB").save(
        noise_jpg_buf, format="JPEG", quality=95
    )
    src_md = tmp_path / "rich.md"
    src_md.write_text(
        "# 第一章 导论\n\n这是一段用于测试 EPUB 转 PDF 与纯文本导出的中文段落内容。\n\n"
        "## 第二章 深入分析\n\n包含第二章节的详细论述与排版验证。\n",
        encoding="utf-8",
    )
    epub_built = await convert_book_to_kindle_epub(
        src_md,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
        title_override="排版转换测试书",
        author_override="墨印作者",
    )

    out_pdf = tmp_path / "from_epub.pdf"
    pdf_art = convert_epub_to_pdf(
        epub_built.kindle_epub_path,
        out_pdf,
        title="排版转换测试书",
        author="墨印作者",
        max_mb=5.0,
    )
    assert out_pdf.exists() and pdf_art.file_size_bytes > 0
    assert pdf_art.file_size_bytes <= 5 * 1024 * 1024
    with pymupdf.open(str(out_pdf)) as pdoc:
        full_txt = "\n".join(pdoc[i].get_text() for i in range(len(pdoc)))
        assert "导论" in full_txt
        assert len(pdoc.get_toc()) >= 2

    out_txt = tmp_path / "from_epub.txt"
    txt_art = extract_book_to_txt(
        epub_built.kindle_epub_path, out_txt, title="排版转换测试书", author="墨印作者"
    )
    assert out_txt.exists() and txt_art.file_size_bytes > 0
    txt_content = out_txt.read_text(encoding="utf-8")
    assert "排版转换测试书" in txt_content
    assert "作者：墨印作者" in txt_content
    assert "深入分析" in txt_content

    # 2. Recompress an oversized EPUB with heavy JPEG asset down under budget
    heavy_epub = tmp_path / "heavy.epub"
    with (
        zipfile.ZipFile(epub_built.kindle_epub_path, "r") as zin,
        zipfile.ZipFile(heavy_epub, "w") as zout,
    ):
        zout.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in zin.infolist():
            if info.filename == "mimetype":
                continue
            if info.filename == "OEBPS/images/cover.jpg":
                zout.writestr(info.filename, noise_jpg_buf.getvalue(), compress_type=zipfile.ZIP_DEFLATED)
            else:
                zout.writestr(info.filename, zin.read(info.filename), compress_type=zipfile.ZIP_DEFLATED)

    orig_heavy = heavy_epub.stat().st_size
    shrunk_epub = tmp_path / "shrunk.epub"
    recomp = recompress_epub_to_budget(heavy_epub, shrunk_epub, max_bytes=300_000)
    assert recomp.file_size_bytes <= 300_000
    assert recomp.file_size_bytes < orig_heavy
    assert validate_epub(shrunk_epub).valid

    # 3. Bogus 12000x16000 pt MediaBox page must clamp cleanly without OOM
    giant_doc = pymupdf.open()
    gp = giant_doc.new_page(width=12000, height=16000)
    gp.draw_rect(pymupdf.Rect(1000, 1000, 4000, 4000), color=(0, 0, 0), fill=(0, 0, 0))
    bw_img = _render_scanned_page_1bit(
        giant_doc[0], target_long_edge_px=1000, threshold=145, median_blend=0.3
    )
    assert max(bw_img.size) <= 1005
    giant_doc.close()

    # 4. Corrupt PDF raises ValueError cleanly
    corrupt_pdf = tmp_path / "bad.pdf"
    corrupt_pdf.write_bytes(b"not-a-pdf")
    with pytest.raises(ValueError, match="PDF"):
        compress_pdf_file(corrupt_pdf, tmp_path / "out_bad.pdf", max_mb=5.0)
