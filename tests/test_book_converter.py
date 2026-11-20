from __future__ import annotations

import zipfile
from pathlib import Path

from inkpress.publishing.book_converter import (
    convert_book_to_kindle_epub,
    list_epub_chapters,
    read_epub_chapter_html,
)
from inkpress.publishing.epub_validator import validate_epub


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


async def test_convert_markdown_to_kindle_epub(tmp_path: Path) -> None:
    src = tmp_path / "essay.md"
    src.write_text(
        "# 技术随笔\n\n这是一篇 **Markdown** 文章。\n\n- 要点 A\n- 要点 B\n", encoding="utf-8"
    )
    res = await convert_book_to_kindle_epub(
        src,
        output_dir=tmp_path / "epubs",
        cover_dir=tmp_path / "covers",
    )
    val = validate_epub(res.kindle_epub_path)
    assert val.valid, f"Converted MD EPUB invalid: {val.errors}"


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
