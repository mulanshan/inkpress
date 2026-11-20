from __future__ import annotations

import asyncio
import html as html_escape
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import docx
import markdown as md_lib
import pymupdf
from bs4 import BeautifulSoup
from lxml import etree

from inkpress.ingestion.extractor import detect_language, sanitize_article_html
from inkpress.logging import get_logger
from inkpress.publishing.cover_generator import generate_editorial_cover
from inkpress.publishing.epub_builder import (
    _CompiledChapter,
    _wrap_xhtml_document,
    _write_strict_epub_zip,
    _xml_safe_fragment,
)
from inkpress.publishing.epub_validator import validate_epub
from inkpress.publishing.image_pipeline import ProcessedImage, optimize_image_bytes

logger = get_logger(__name__)

SUPPORTED_BOOK_FORMATS = {
    "epub",
    "mobi",
    "azw3",
    "azw",
    "txt",
    "md",
    "markdown",
    "html",
    "htm",
    "docx",
    "pdf",
    "fb2",
    "cbz",
    "cbr",
    "rtf",
}

CHAPTER_HEADING_RE = re.compile(
    r"^(第[零一二三四五六七八九十百千万0-9]+[章节回卷部篇集].*|Chapter\s+\d+.*|CHAPTER\s+\d+.*)$",
    re.MULTILINE,
)


@dataclass
class ConvertedBookResult:
    title: str
    author: str
    language: str
    kindle_epub_path: Path
    cover_path: Path | None
    file_size_bytes: int
    chapter_count: int
    notes: str = ""


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


async def _run_calibre_convert(src: Path, dst: Path, extra_args: list[str] | None = None) -> None:
    cmd = [
        "ebook-convert",
        str(src),
        str(dst),
        "--epub-version=3",
        "--embed-all-fonts=false",
        *(extra_args or []),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    if proc.returncode != 0 or not dst.exists():
        err = (stderr or stdout).decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ebook-convert failed (rc={proc.returncode}): {err}")


def _extract_epub_metadata_and_cover(
    epub_path: Path, cover_out: Path
) -> tuple[str, str, str, Path | None]:
    title = epub_path.stem
    author = ""
    language = "zh-CN"
    saved_cover: Path | None = None

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = zf.namelist()
            opf_name = next((n for n in names if n.endswith(".opf")), None)
            if opf_name:
                opf_root = etree.fromstring(zf.read(opf_name))
                dc_ns = {"dc": "http://purl.org/dc/elements/1.1/"}
                t = opf_root.find(".//dc:title", namespaces=dc_ns)
                if t is not None and (t.text or "").strip():
                    title = t.text.strip()
                a = opf_root.find(".//dc:creator", namespaces=dc_ns)
                if a is not None and (a.text or "").strip():
                    author = a.text.strip()
                lang = opf_root.find(".//dc:language", namespaces=dc_ns)
                if lang is not None and (lang.text or "").strip():
                    language = lang.text.strip()

                # Try to locate cover image
                cover_href = None
                for item in opf_root.xpath(".//*[local-name()='item']"):
                    props = (item.get("properties") or "").lower()
                    iid = (item.get("id") or "").lower()
                    href = item.get("href") or ""
                    if "cover-image" in props or (
                        "cover" in iid and href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                    ):
                        cover_href = href
                        break
                if cover_href:
                    opf_dir = str(Path(opf_name).parent)
                    candidate = (
                        f"{opf_dir}/{cover_href}" if opf_dir not in ("", ".") else cover_href
                    )
                    candidate = candidate.lstrip("./")
                    if candidate in names:
                        opt = optimize_image_bytes(zf.read(candidate), max_width=800)
                        if opt:
                            cover_out.parent.mkdir(parents=True, exist_ok=True)
                            cover_out.write_bytes(opt)
                            saved_cover = cover_out
    except Exception as exc:
        logger.debug("epub_metadata_extract_warn", err=str(exc))

    return title, author, language, saved_cover


def normalize_epub_for_kindle(src: Path, dst: Path, cover_out: Path) -> ConvertedBookResult:
    """Repair common Send-to-Kindle E999 triggers in an existing EPUB (UTF-8, empty img, mimetype)."""
    title, author, language, saved_cover = _extract_epub_metadata_and_cover(src, cover_out)
    validation = validate_epub(src)
    if validation.valid and src.stat().st_size <= 22 * 1024 * 1024:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if saved_cover is None:
            cover_bytes = generate_editorial_cover(
                title=title, subtitle=author or "Kindle Library", style="editorial"
            )
            cover_out.parent.mkdir(parents=True, exist_ok=True)
            cover_out.write_bytes(cover_bytes)
            saved_cover = cover_out
        return ConvertedBookResult(
            title=title,
            author=author,
            language=language,
            kindle_epub_path=dst,
            cover_path=saved_cover,
            file_size_bytes=dst.stat().st_size,
            chapter_count=validation.chapter_count,
            notes="EPUB already Kindle-compliant",
        )

    # Rebuild chapters cleanly into a strict Kindle-safe EPUB3
    chapters: list[_CompiledChapter] = []
    with zipfile.ZipFile(src, "r") as zf:
        html_entries = sorted(
            n
            for n in zf.namelist()
            if n.lower().endswith((".xhtml", ".html", ".htm"))
            and "nav" not in Path(n).name.lower()
            and "cover" not in Path(n).name.lower()
        )
        # Collect images inside archive
        archive_images: dict[str, bytes] = {}
        for name in zf.namelist():
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                archive_images[Path(name).name] = zf.read(name)

        for idx, entry_name in enumerate(html_entries, start=1):
            raw_bytes = zf.read(entry_name)
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            soup = BeautifulSoup(raw_text, "lxml")
            heading = soup.find(["h1", "h2", "h3", "title"])
            ch_title = (heading.get_text(" ", strip=True) if heading else f"第 {idx} 节")[:160]
            ch_imgs: list[ProcessedImage] = []
            for img in soup.find_all("img"):
                img_src = str(img.get("src") or "").strip()
                base_name = Path(img_src).name
                if base_name in archive_images:
                    opt = optimize_image_bytes(archive_images[base_name], max_width=1000)
                    if opt:
                        out_name = f"images/rebuilt_{idx:03d}_{len(ch_imgs) + 1:02d}.jpg"
                        img["src"] = out_name
                        ch_imgs.append(
                            ProcessedImage(
                                original_url=img_src,
                                filename=out_name,
                                media_type="image/jpeg",
                                data=opt,
                            )
                        )
                        continue
                img.decompose()
            body = soup.body if soup.body else soup
            clean_inner, _, _ = sanitize_article_html(body.decode_contents())
            safe_inner = _xml_safe_fragment(clean_inner)
            anchor_id = f"sec-{idx:04d}"
            full_inner = f'<h1 id="{anchor_id}">{html_escape.escape(ch_title)}</h1>\n{safe_inner}'
            chapters.append(
                _CompiledChapter(
                    filename=f"ch_{idx:04d}.xhtml",
                    item_id=f"ch-{idx:04d}",
                    anchor_id=anchor_id,
                    title=ch_title,
                    category="书库",
                    xhtml_bytes=_wrap_xhtml_document(
                        title=ch_title, body_inner=full_inner, language=language
                    ),
                    images=ch_imgs,
                )
            )

    if not chapters:
        raise RuntimeError("No readable HTML chapters found in EPUB")

    if saved_cover is not None:
        cover_bytes = saved_cover.read_bytes()
    else:
        cover_bytes = generate_editorial_cover(
            title=title, subtitle=author or "Kindle Library", style="editorial"
        )
        cover_out.parent.mkdir(parents=True, exist_ok=True)
        cover_out.write_bytes(cover_bytes)
        saved_cover = cover_out

    _write_strict_epub_zip(
        dst,
        book_id=f"urn:uuid:{uuid.uuid4()}",
        title=title,
        author=author or "Unknown",
        language=language,
        issue_date=datetime.now(UTC),
        cover_bytes=cover_bytes,
        chapters=chapters,
    )
    return ConvertedBookResult(
        title=title,
        author=author,
        language=language,
        kindle_epub_path=dst,
        cover_path=saved_cover,
        file_size_bytes=dst.stat().st_size,
        chapter_count=len(chapters),
        notes=f"Repaired EPUB ({len(validation.errors)} issue(s) fixed)",
    )


def _split_text_to_chapters(text: str, default_title: str) -> list[tuple[str, str]]:
    matches = list(CHAPTER_HEADING_RE.finditer(text))
    if len(matches) >= 2:
        sections: list[tuple[str, str]] = []
        if matches[0].start() > 100:
            sections.append(("前言", text[: matches[0].start()].strip()))
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            ch_title = m.group(1).strip()[:120]
            ch_body = text[start:end].strip()
            if ch_body:
                sections.append((ch_title, ch_body))
        if sections:
            return sections

    # Fallback: chunk long plain text into ~12,000 char chapters for crisp Kindle pagination
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paragraphs:
        return [(default_title, text.strip())]
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    buf_len = 0
    for p in paragraphs:
        buf.append(p)
        buf_len += len(p)
        if buf_len >= 12000:
            chunks.append((f"{default_title} ({len(chunks) + 1})", "\n\n".join(buf)))
            buf = []
            buf_len = 0
    if buf:
        label = default_title if not chunks else f"{default_title} ({len(chunks) + 1})"
        chunks.append((label, "\n\n".join(buf)))
    return chunks


def _build_epub_from_sections(
    *,
    sections: list[tuple[str, str]],  # (chapter_title, html_fragment)
    title: str,
    author: str,
    language: str,
    dst: Path,
    cover_out: Path,
    notes: str = "",
) -> ConvertedBookResult:
    compiled: list[_CompiledChapter] = []
    for idx, (ch_title, ch_html) in enumerate(sections, start=1):
        anchor_id = f"sec-{idx:04d}"
        safe_inner = _xml_safe_fragment(ch_html)
        full_inner = f'<h1 id="{anchor_id}" class="article-title">{html_escape.escape(ch_title)}</h1>\n{safe_inner}'
        compiled.append(
            _CompiledChapter(
                filename=f"ch_{idx:04d}.xhtml",
                item_id=f"ch-{idx:04d}",
                anchor_id=anchor_id,
                title=ch_title,
                category="书库",
                xhtml_bytes=_wrap_xhtml_document(
                    title=ch_title, body_inner=full_inner, language=language
                ),
            )
        )

    cover_bytes = generate_editorial_cover(
        title=title,
        subtitle=author or "Personal Library",
        headlines=[s[0] for s in sections[:4]],
        style="editorial",
    )
    cover_out.parent.mkdir(parents=True, exist_ok=True)
    cover_out.write_bytes(cover_bytes)

    _write_strict_epub_zip(
        dst,
        book_id=f"urn:uuid:{uuid.uuid4()}",
        title=title,
        author=author or "Unknown",
        language=language,
        issue_date=datetime.now(UTC),
        cover_bytes=cover_bytes,
        chapters=compiled,
    )
    return ConvertedBookResult(
        title=title,
        author=author,
        language=language,
        kindle_epub_path=dst,
        cover_path=cover_out,
        file_size_bytes=dst.stat().st_size,
        chapter_count=len(compiled),
        notes=notes,
    )


async def convert_book_to_kindle_epub(
    src: Path,
    *,
    output_dir: Path,
    cover_dir: Path,
    title_override: str = "",
    author_override: str = "",
) -> ConvertedBookResult:
    """Convert any supported book/magazine format into a verified Kindle-ready EPUB3."""
    ext = src.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_BOOK_FORMATS:
        raise ValueError(f"Unsupported book format: .{ext}")

    uid = uuid.uuid4().hex[:12]
    dst = output_dir / f"{src.stem}_{uid}.epub"
    cover_out = cover_dir / f"book_{uid}.jpg"
    title = title_override.strip() or src.stem
    author = author_override.strip()

    if ext == "epub":
        res = await asyncio.to_thread(normalize_epub_for_kindle, src, dst, cover_out)
        if title_override.strip():
            res.title = title_override.strip()
        if author_override.strip():
            res.author = author_override.strip()
        return res

    if ext == "txt":
        raw_bytes = src.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
            try:
                text = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw_bytes.decode("utf-8", errors="replace")
        lang = detect_language(text)
        raw_sections = _split_text_to_chapters(text, title)
        html_sections = [
            (
                ch_t,
                "\n".join(
                    f"<p>{html_escape.escape(p.strip())}</p>"
                    for p in ch_body.splitlines()
                    if p.strip()
                ),
            )
            for ch_t, ch_body in raw_sections
        ]
        return await asyncio.to_thread(
            _build_epub_from_sections,
            sections=html_sections,
            title=title,
            author=author,
            language=lang,
            dst=dst,
            cover_out=cover_out,
            notes="Converted from TXT with automatic chapter detection",
        )

    if ext in ("md", "markdown"):
        text = src.read_text(encoding="utf-8", errors="replace")
        lang = detect_language(text)
        rendered = md_lib.markdown(text, extensions=["tables", "fenced_code", "toc"])
        clean_html, _, _ = sanitize_article_html(rendered)
        return await asyncio.to_thread(
            _build_epub_from_sections,
            sections=[(title, clean_html)],
            title=title,
            author=author,
            language=lang,
            dst=dst,
            cover_out=cover_out,
            notes="Converted from Markdown",
        )

    if ext in ("html", "htm"):
        raw = src.read_text(encoding="utf-8", errors="replace")
        clean_html, plain_text, _ = sanitize_article_html(raw)
        lang = detect_language(plain_text)
        return await asyncio.to_thread(
            _build_epub_from_sections,
            sections=[(title, clean_html)],
            title=title,
            author=author,
            language=lang,
            dst=dst,
            cover_out=cover_out,
            notes="Converted from HTML",
        )

    if ext == "docx":
        document = docx.Document(str(src))
        sections: list[tuple[str, str]] = []
        cur_title = title
        cur_paras: list[str] = []
        full_sample = []
        for para in document.paragraphs:
            t = para.text.strip()
            if not t:
                continue
            full_sample.append(t)
            style_name = (para.style.name if para.style else "").lower()
            if "heading 1" in style_name or "heading 2" in style_name:
                if cur_paras:
                    sections.append((cur_title, "\n".join(cur_paras)))
                    cur_paras = []
                cur_title = t[:120]
            else:
                cur_paras.append(f"<p>{html_escape.escape(t)}</p>")
        if cur_paras:
            sections.append((cur_title, "\n".join(cur_paras)))
        if not sections:
            sections = [(title, "<p>（文档为空）</p>")]
        lang = detect_language("\n".join(full_sample[:40]))
        return await asyncio.to_thread(
            _build_epub_from_sections,
            sections=sections,
            title=title,
            author=author,
            language=lang,
            dst=dst,
            cover_out=cover_out,
            notes="Converted from DOCX",
        )

    if ext == "pdf":

        def _convert_pdf() -> ConvertedBookResult:
            pdf_doc = pymupdf.open(str(src))
            try:
                meta = pdf_doc.metadata or {}
                pdf_title = title_override.strip() or (meta.get("title") or "").strip() or title
                pdf_author = author_override.strip() or (meta.get("author") or "").strip() or author
                page_texts: list[str] = []
                for page_idx in range(len(pdf_doc)):
                    page_texts.append(str(pdf_doc[page_idx].get_text("text")).strip())
                combined = "\n\n".join(t for t in page_texts if t)

                # Save first page rendered thumbnail as cover
                if len(pdf_doc) > 0:
                    pix = pdf_doc[0].get_pixmap(dpi=130)
                    opt = optimize_image_bytes(pix.tobytes("png"), max_width=800)
                    if opt:
                        cover_out.parent.mkdir(parents=True, exist_ok=True)
                        cover_out.write_bytes(opt)

                if len(combined) < 200:
                    # Scanned / image-heavy magazine PDF: preserve original PDF for native Kindle PDF push
                    pdf_dst = output_dir / f"{src.stem}_{uid}.pdf"
                    pdf_dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, pdf_dst)
                    return ConvertedBookResult(
                        title=pdf_title,
                        author=pdf_author,
                        language="zh-CN",
                        kindle_epub_path=pdf_dst,
                        cover_path=cover_out if cover_out.exists() else None,
                        file_size_bytes=pdf_dst.stat().st_size,
                        chapter_count=len(pdf_doc),
                        notes="Scanned/layout PDF preserved directly (Amazon Send to Kindle supports native PDF)",
                    )

                lang = detect_language(combined)
                raw_sections = _split_text_to_chapters(combined, pdf_title)
                html_sections = [
                    (
                        ch_t,
                        "\n".join(
                            f"<p>{html_escape.escape(p.strip())}</p>"
                            for p in re.split(r"\n+", ch_b)
                            if p.strip()
                        ),
                    )
                    for ch_t, ch_b in raw_sections
                ]
                return _build_epub_from_sections(
                    sections=html_sections,
                    title=pdf_title,
                    author=pdf_author,
                    language=lang,
                    dst=dst,
                    cover_out=cover_out,
                    notes=f"Reflowed text PDF ({len(pdf_doc)} pages) to Kindle EPUB3",
                )
            finally:
                pdf_doc.close()

        return await asyncio.to_thread(_convert_pdf)

    # MOBI / AZW3 / FB2 / CBZ / CBR / RTF via Calibre ebook-convert
    if _has_binary("ebook-convert"):
        raw_epub = output_dir / f"calibre_tmp_{uid}.epub"
        try:
            await _run_calibre_convert(src, raw_epub)
            res = await asyncio.to_thread(normalize_epub_for_kindle, raw_epub, dst, cover_out)
            if title_override.strip():
                res.title = title_override.strip()
            if author_override.strip():
                res.author = author_override.strip()
            res.notes = f"Converted from .{ext} via Calibre ebook-convert + Kindle normalization"
            return res
        finally:
            if raw_epub.exists():
                raw_epub.unlink(missing_ok=True)

    raise RuntimeError(
        f"Format .{ext} requires Calibre CLI (ebook-convert) in the worker container."
    )


def list_epub_chapters(epub_path: Path) -> list[dict[str, object]]:
    """Extract ordered chapter list from an EPUB for the built-in web reader."""
    if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
        return []
    chapters: list[dict[str, object]] = []
    with zipfile.ZipFile(epub_path, "r") as zf:
        names = zf.namelist()
        opf_name = next((n for n in names if n.endswith(".opf")), None)
        ordered_files: list[str] = []
        if opf_name:
            opf_dir = str(Path(opf_name).parent)
            opf_root = etree.fromstring(zf.read(opf_name))
            manifest: dict[str, str] = {}
            for item in opf_root.xpath(".//*[local-name()='item']"):
                iid = item.get("id")
                href = item.get("href")
                if iid and href:
                    full = f"{opf_dir}/{href}" if opf_dir not in ("", ".") else href
                    manifest[iid] = full.lstrip("./")
            for ref in opf_root.xpath(".//*[local-name()='itemref']"):
                idref = ref.get("idref")
                if idref and idref in manifest:
                    path = manifest[idref]
                    if (
                        path in names
                        and "cover" not in Path(path).name.lower()
                        and "nav" not in Path(path).name.lower()
                    ):
                        ordered_files.append(path)
        if not ordered_files:
            ordered_files = sorted(
                n
                for n in names
                if n.lower().endswith((".xhtml", ".html"))
                and "cover" not in Path(n).name.lower()
                and "nav" not in Path(n).name.lower()
            )
        for idx, fname in enumerate(ordered_files):
            raw = zf.read(fname).decode("utf-8", errors="replace")
            soup = BeautifulSoup(raw, "lxml")
            h = soup.find(["h1", "h2", "title"])
            title = (h.get_text(" ", strip=True) if h else f"Chapter {idx + 1}")[:180]
            chapters.append({"index": idx, "filename": fname, "title": title})
    return chapters


def read_epub_chapter_html(epub_path: Path, chapter_index: int, book_id: int) -> dict[str, object]:
    """Return sanitized HTML of a specific EPUB chapter with rewritten asset URLs."""
    chapters = list_epub_chapters(epub_path)
    if chapter_index < 0 or chapter_index >= len(chapters):
        raise IndexError("Chapter index out of range")
    fname = str(chapters[chapter_index]["filename"])
    ch_dir = str(Path(fname).parent)

    with zipfile.ZipFile(epub_path, "r") as zf:
        raw = zf.read(fname).decode("utf-8", errors="replace")
        soup = BeautifulSoup(raw, "lxml")
        for img in soup.find_all("img"):
            src = str(img.get("src") or "").strip()
            if src and not src.startswith(("http://", "https://", "data:")):
                resolved = (f"{ch_dir}/{src}" if ch_dir not in ("", ".") else src).lstrip("./")
                img["src"] = f"/api/v1/books/{book_id}/asset/{resolved}"
        body = soup.body if soup.body else soup
        html_out = body.decode_contents()
    return {
        "index": chapter_index,
        "total": len(chapters),
        "title": chapters[chapter_index]["title"],
        "html": html_out,
    }
