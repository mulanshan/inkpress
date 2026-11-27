from __future__ import annotations

import asyncio
import html as html_escape
import posixpath
import re
import shutil
import statistics
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import docx
import markdown as md_lib
import nh3
import pymupdf
from bs4 import BeautifulSoup, Tag
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
from inkpress.publishing.epub_validator import MISSING_NCX_ERROR, validate_epub
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
    "rtf",
}

CHAPTER_HEADING_RE = re.compile(
    r"^(第[零一二三四五六七八九十百千万0-9]+[章节回卷部篇集].*|Chapter\s+\d+.*|CHAPTER\s+\d+.*)$",
    re.MULTILINE,
)

_CHARSET_META_RE = re.compile(
    r'(?:charset|encoding)\s*=\s*["\']?([A-Za-z0-9._-]+)', re.IGNORECASE
)
_FILENAME_TITLE_RE = re.compile(r"^[A-Za-z0-9_./-]+\.(?:xhtml|html|htm)$", re.IGNORECASE)
_PAGE_NUMBER_RE = re.compile(
    r"^(?:[-—–·\s]*\d+[-—–·\s]*|(?:page|p\.?|第)\s*\d+\s*(?:of\s*\d+|/\s*\d+|页)?)$",
    re.IGNORECASE,
)
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_SENTENCE_END_CHARS = ("。", "！", "？", ".", "!", "?", "”", "」", "》", "）", ")")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_MAX_INPLACE_EPUB_BYTES = 18 * 1024 * 1024
_MAX_PDF_IMAGES = 120

READER_ALLOWED_TAGS: set[str] = {
    "p",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "code",
    "ul",
    "ol",
    "li",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "del",
    "a",
    "img",
    "figure",
    "figcaption",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "hr",
    "div",
    "span",
    "sup",
    "sub",
    "section",
    "article",
    "header",
    "footer",
    "dl",
    "dt",
    "dd",
    "caption",
    "small",
    "mark",
    "abbr",
    "cite",
    "q",
}

READER_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "class", "data-chapter-index"},
    "img": {"src", "alt", "title", "class"},
    "td": {"colspan", "rowspan", "class"},
    "th": {"colspan", "rowspan", "class"},
    "div": {"class"},
    "span": {"class"},
    "p": {"class"},
    "blockquote": {"class"},
    "h1": {"class"},
    "h2": {"class"},
    "h3": {"class"},
    "h4": {"class"},
    "h5": {"class"},
    "h6": {"class"},
    "pre": {"class"},
    "code": {"class"},
    "ul": {"class"},
    "ol": {"class"},
    "li": {"class"},
    "figure": {"class"},
    "figcaption": {"class"},
    "table": {"class"},
    "section": {"class"},
    "article": {"class"},
}


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


@dataclass
class _ManifestItem:
    item_id: str
    href: str
    resolved_path: str
    media_type: str
    properties: str


@dataclass
class _OpfInfo:
    opf_path: str
    opf_root: etree._Element
    title: str
    author: str
    language: str
    book_id: str
    manifest: dict[str, _ManifestItem] = field(default_factory=dict)
    spine_paths: list[str] = field(default_factory=list)
    non_chapter_paths: set[str] = field(default_factory=set)
    nav_path: str | None = None
    cover_image_path: str | None = None


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _safe_xml_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def _resolve_epub_path(base_member: str, href: str) -> str:
    clean_href = href.split("#", 1)[0].split("?", 1)[0]
    clean_href = unquote(clean_href).strip()
    if not clean_href:
        return posixpath.normpath(base_member).lstrip("/")
    base_dir = posixpath.dirname(base_member)
    joined = posixpath.join(base_dir, clean_href) if base_dir else clean_href
    return posixpath.normpath(joined).lstrip("/")


def _decode_html_bytes(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    head_ascii = raw_bytes[:1024].decode("ascii", errors="ignore")
    declared_match = _CHARSET_META_RE.search(head_ascii)
    candidates: list[str] = []
    if declared_match:
        enc = declared_match.group(1).strip().lower()
        if enc in ("gb2312", "gbk"):
            enc = "gb18030"
        candidates.append(enc)
    candidates.extend(["gb18030", "big5", "latin-1"])

    for enc in candidates:
        try:
            return raw_bytes.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_xhtml_soup(raw_text: str) -> BeautifulSoup:
    stripped = raw_text.lstrip()
    if stripped.startswith("<?xml") or "http://www.w3.org/1999/xhtml" in stripped[:400]:
        try:
            soup = BeautifulSoup(raw_text, "xml")
            if soup.body is not None:
                return soup
        except Exception:
            pass
    cleaned = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", raw_text, flags=re.IGNORECASE)
    return BeautifulSoup(cleaned, "lxml")


def _extract_chapter_title(soup: BeautifulSoup, fallback: str) -> str:
    """Prefer visible headings inside <body> over <head><title> (avoids ch001.xhtml titles)."""
    body = soup.body if soup.body else soup
    for tag_name in ("h1", "h2", "h3"):
        for heading in body.find_all(tag_name):
            text = heading.get_text(" ", strip=True)
            if text:
                return text
    title_tag = soup.find("title")
    if title_tag is not None:
        title_text = title_tag.get_text(" ", strip=True)
        if title_text and not _FILENAME_TITLE_RE.match(title_text):
            return title_text
    return fallback


def _starts_with_h1(xhtml_fragment: str) -> bool:
    stripped = xhtml_fragment.lstrip()
    if re.match(r"^<h1(\s|>|/>)", stripped, flags=re.IGNORECASE):
        return True
    try:
        frag_soup = BeautifulSoup(stripped, "lxml")
        root = frag_soup.body if frag_soup.body else frag_soup
        for child in root.children:
            if not isinstance(child, Tag):
                if str(child).strip():
                    return False
                continue
            if child.name == "h1":
                return True
            if child.name in ("div", "section", "header", "article"):
                first_tag = child.find(True)
                if isinstance(first_tag, Tag) and first_tag.name == "h1":
                    prefix = ""
                    for elem in child.children:
                        if elem is first_tag:
                            break
                        prefix += elem.get_text() if isinstance(elem, Tag) else str(elem)
                    if not prefix.strip():
                        return True
            return False
    except Exception:
        return False
    return False


def _ensure_leading_h1_or_prepend(
    safe_inner: str,
    ch_title: str,
    anchor_id: str,
    *,
    add_article_title_class: bool = False,
) -> tuple[str, str]:
    """Avoid prepending a duplicate <h1> when chapter HTML already begins with <h1>."""
    if _starts_with_h1(safe_inner):
        match = re.search(r"<h1(\b[^>]*)>", safe_inner, flags=re.IGNORECASE)
        if match:
            attrs = match.group(1)
            id_match = re.search(r'\bid\s*=\s*"([^"]+)"', attrs)
            if id_match:
                return safe_inner, id_match.group(1)
            patched = (
                safe_inner[: match.start()]
                + f'<h1 id="{anchor_id}"{attrs}>'
                + safe_inner[match.end() :]
            )
            return patched, anchor_id
        return safe_inner, anchor_id
    cls_attr = ' class="article-title"' if add_article_title_class else ""
    return f'<h1 id="{anchor_id}"{cls_attr}>{html_escape.escape(ch_title)}</h1>\n{safe_inner}', anchor_id


def _locate_opf_path(zf: zipfile.ZipFile) -> str | None:
    names = zf.namelist()
    name_set = set(names)
    parser = _safe_xml_parser()
    if "META-INF/container.xml" in name_set:
        try:
            root = etree.fromstring(zf.read("META-INF/container.xml"), parser=parser)
            rootfile = root.find(".//{*}rootfile")
            if rootfile is not None:
                raw_path = unquote((rootfile.get("full-path") or "").strip()).lstrip("/")
                norm_path = posixpath.normpath(raw_path) if raw_path else ""
                if norm_path and norm_path in name_set:
                    return norm_path
        except Exception:
            pass
    return next((n for n in names if n.lower().endswith(".opf")), None)


def _parse_opf_structure(zf: zipfile.ZipFile) -> _OpfInfo | None:
    opf_path = _locate_opf_path(zf)
    if opf_path is None:
        return None
    names = set(zf.namelist())
    parser = _safe_xml_parser()
    try:
        opf_root = etree.fromstring(zf.read(opf_path), parser=parser)
    except Exception:
        return None

    dc_ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    t_el = opf_root.find(".//dc:title", namespaces=dc_ns)
    a_el = opf_root.find(".//dc:creator", namespaces=dc_ns)
    lang_el = opf_root.find(".//dc:language", namespaces=dc_ns)
    id_el = opf_root.find(".//dc:identifier", namespaces=dc_ns)

    title = (t_el.text or "").strip() if t_el is not None else ""
    author = (a_el.text or "").strip() if a_el is not None else ""
    language = (lang_el.text or "").strip() if lang_el is not None else "zh-CN"
    book_id = (id_el.text or "").strip() if id_el is not None else f"urn:uuid:{uuid.uuid4()}"

    manifest: dict[str, _ManifestItem] = {}
    non_chapter_paths: set[str] = set()
    nav_path: str | None = None
    cover_image_path: str | None = None

    for item in opf_root.xpath(".//*[local-name()='manifest']/*[local-name()='item']"):
        iid = (item.get("id") or "").strip()
        href = (item.get("href") or "").strip()
        mtype = (item.get("media-type") or "").strip().lower()
        props = (item.get("properties") or "").strip().lower()
        if not iid or not href:
            continue
        resolved = _resolve_epub_path(opf_path, href)
        m_item = _ManifestItem(
            item_id=iid,
            href=href,
            resolved_path=resolved,
            media_type=mtype,
            properties=props,
        )
        manifest[iid] = m_item

        prop_tokens = set(props.split())
        if "nav" in prop_tokens:
            nav_path = resolved
            non_chapter_paths.add(resolved)
        if iid.lower() in ("nav", "toc", "cover-page", "coverpage", "cover") and (
            mtype in ("application/xhtml+xml", "text/html")
            or resolved.lower().endswith((".xhtml", ".html", ".htm"))
        ):
            non_chapter_paths.add(resolved)
        if "cover-image" in prop_tokens and resolved in names:
            cover_image_path = resolved

    # EPUB2 <meta name="cover" content="cover-image-id"/>
    if cover_image_path is None:
        for meta in opf_root.xpath(".//*[local-name()='metadata']/*[local-name()='meta']"):
            if (meta.get("name") or "").strip().lower() == "cover":
                content = (meta.get("content") or "").strip()
                if content in manifest and manifest[content].resolved_path in names:
                    cover_image_path = manifest[content].resolved_path
                    break
                if content:
                    direct_path = _resolve_epub_path(opf_path, content)
                    if direct_path in names:
                        cover_image_path = direct_path
                        break

    # Fallback: manifest image item with cover id
    if cover_image_path is None:
        for m_item in manifest.values():
            if (
                m_item.item_id.lower() in ("cover", "cover-image", "cover_image", "cover-img")
                and (
                    m_item.media_type.startswith("image/")
                    or m_item.resolved_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                )
                and m_item.resolved_path in names
            ):
                cover_image_path = m_item.resolved_path
                break

    # Identify cover/toc XHTML pages via OPF <guide>
    for ref in opf_root.xpath(".//*[local-name()='guide']/*[local-name()='reference']"):
        rtype = (ref.get("type") or "").strip().lower()
        rhref = (ref.get("href") or "").strip()
        if rtype in ("cover", "toc") and rhref:
            non_chapter_paths.add(_resolve_epub_path(opf_path, rhref))

    # Spine reading order
    spine_paths: list[str] = []
    seen_spine: set[str] = set()
    for ref in opf_root.xpath(".//*[local-name()='spine']/*[local-name()='itemref']"):
        idref = (ref.get("idref") or "").strip()
        if idref and idref in manifest:
            res_path = manifest[idref].resolved_path
            if res_path in names and res_path not in seen_spine:
                seen_spine.add(res_path)
                spine_paths.append(res_path)

    if nav_path is None:
        for m_item in manifest.values():
            if posixpath.basename(m_item.resolved_path).lower() in ("nav.xhtml", "nav.html"):
                nav_path = m_item.resolved_path
                non_chapter_paths.add(nav_path)
                break

    return _OpfInfo(
        opf_path=opf_path,
        opf_root=opf_root,
        title=title,
        author=author,
        language=language or "zh-CN",
        book_id=book_id,
        manifest=manifest,
        spine_paths=spine_paths,
        non_chapter_paths=non_chapter_paths,
        nav_path=nav_path,
        cover_image_path=cover_image_path,
    )


async def _run_calibre_convert(src: Path, dst: Path, extra_args: list[str] | None = None) -> None:
    cmd = [
        "ebook-convert",
        str(src),
        str(dst),
        *(["--epub-version=3"] if dst.suffix.lower() == ".epub" else []),
        *(extra_args or []),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"ebook-convert timed out after 300s for {src.name}") from None
    if proc.returncode != 0 or not dst.exists():
        err = (stderr or stdout).decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ebook-convert failed (rc={proc.returncode}): {err}")


async def build_companion_kindle_mobi(epub_path: Path, *, timeout: float = 120.0) -> Path | None:
    """Build a dual KF8+MOBI7 `.mobi` companion alongside an `.epub` when Calibre CLI is available.

    Kindle Experimental Browser (firmware >= 5.16.4) blocks `.epub` and `.azw3` direct HTTP
    downloads and only accepts `.mobi`/`.azw`/`.prc`/`.txt` extensions.
    """
    if (
        not epub_path.exists()
        or epub_path.suffix.lower() != ".epub"
        or not _has_binary("ebook-convert")
    ):
        return None
    mobi_path = epub_path.with_suffix(".mobi")
    cmd = [
        "ebook-convert",
        str(epub_path),
        str(mobi_path),
        "--mobi-file-type=both",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            mobi_path.unlink(missing_ok=True)
            logger.warning("companion_mobi_timeout", epub=epub_path.name)
            return None
        if proc.returncode == 0 and mobi_path.exists() and mobi_path.stat().st_size > 0:
            return mobi_path
        mobi_path.unlink(missing_ok=True)
    except Exception as exc:
        mobi_path.unlink(missing_ok=True)
        logger.warning("companion_mobi_failed", epub=epub_path.name, err=str(exc))
    return None


async def _run_pandoc_docx_to_epub(src: Path, dst: Path, title: str, author: str = "") -> None:
    cmd = [
        "pandoc",
        str(src),
        "-o",
        str(dst),
        "--to=epub3",
        "--metadata",
        f"title={title}",
    ]
    if author:
        cmd.extend(["--metadata", f"author={author}"])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"pandoc timed out after 180s for {src.name}") from None
    if proc.returncode != 0 or not dst.exists():
        err = (stderr or stdout).decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"pandoc failed (rc={proc.returncode}): {err}")


def _extract_epub_metadata_and_cover(
    epub_path: Path, cover_out: Path
) -> tuple[str, str, str, Path | None]:
    title = epub_path.stem
    author = ""
    language = "zh-CN"
    saved_cover: Path | None = None

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            opf_info = _parse_opf_structure(zf)
            if opf_info is not None:
                if opf_info.title:
                    title = opf_info.title
                if opf_info.author:
                    author = opf_info.author
                if opf_info.language:
                    language = opf_info.language
                if opf_info.cover_image_path:
                    opt = optimize_image_bytes(zf.read(opf_info.cover_image_path), max_width=800)
                    if opt:
                        cover_out.parent.mkdir(parents=True, exist_ok=True)
                        cover_out.write_bytes(opt)
                        saved_cover = cover_out
    except Exception as exc:
        logger.debug("epub_metadata_extract_warn", err=str(exc))

    return title, author, language, saved_cover


def _inject_toc_ncx_inplace(src: Path, dst: Path) -> bool:
    """Inject a spec-compliant EPUB2 toc.ncx into an otherwise valid EPUB3 without rebuilding."""
    parser = _safe_xml_parser()
    try:
        with zipfile.ZipFile(src, "r") as zf:
            opf_info = _parse_opf_structure(zf)
            if opf_info is None:
                return False

            names = set(zf.namelist())
            opf_dir = posixpath.dirname(opf_info.opf_path)
            ncx_href = "toc.ncx"
            ncx_zip_path = posixpath.join(opf_dir, ncx_href) if opf_dir else ncx_href

            # Collect TOC entries from EPUB3 nav.xhtml first, then fall back to spine
            nav_entries: list[tuple[str, str]] = []
            if opf_info.nav_path and opf_info.nav_path in names:
                nav_raw = _decode_html_bytes(zf.read(opf_info.nav_path))
                nav_soup = _parse_xhtml_soup(nav_raw)
                toc_nav = nav_soup.find(
                    "nav", attrs={"epub:type": lambda v: bool(v and "toc" in str(v).lower())}
                ) or nav_soup.find("nav")
                if isinstance(toc_nav, Tag):
                    for a_tag in toc_nav.find_all("a", href=True):
                        label = a_tag.get_text(" ", strip=True)
                        raw_href = str(a_tag.get("href") or "").strip()
                        if not label or not raw_href or _SCHEME_RE.match(raw_href):
                            continue
                        frag = f"#{raw_href.split('#', 1)[1]}" if "#" in raw_href else ""
                        resolved = _resolve_epub_path(opf_info.nav_path, raw_href)
                        if resolved in names:
                            rel_src = (
                                posixpath.relpath(resolved, opf_dir) if opf_dir else resolved
                            )
                            nav_entries.append((label[:180], f"{rel_src}{frag}"))

            if not nav_entries:
                for idx, ch_path in enumerate(opf_info.spine_paths, start=1):
                    if ch_path in opf_info.non_chapter_paths:
                        continue
                    base_lower = posixpath.basename(ch_path).lower()
                    if base_lower in ("cover.xhtml", "cover.html", "nav.xhtml", "nav.html"):
                        continue
                    raw_text = _decode_html_bytes(zf.read(ch_path))
                    soup = _parse_xhtml_soup(raw_text)
                    ch_title = _extract_chapter_title(soup, fallback=f"第 {idx} 节")[:180]
                    rel_src = posixpath.relpath(ch_path, opf_dir) if opf_dir else ch_path
                    nav_entries.append((ch_title, rel_src))

            if not nav_entries:
                return False

            ncx_points = "\n".join(
                f'    <navPoint id="navPoint-{idx}" playOrder="{idx}">\n'
                f"      <navLabel><text>{html_escape.escape(label)}</text></navLabel>\n"
                f'      <content src="{html_escape.escape(href, quote=True)}"/>\n'
                "    </navPoint>"
                for idx, (label, href) in enumerate(nav_entries, start=1)
            )
            toc_ncx = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
                "  <head>\n"
                f'    <meta name="dtb:uid" content="{html_escape.escape(opf_info.book_id)}"/>\n'
                '    <meta name="dtb:depth" content="1"/>\n'
                '    <meta name="dtb:totalPageCount" content="0"/>\n'
                '    <meta name="dtb:maxPageNumber" content="0"/>\n'
                "  </head>\n"
                f"  <docTitle><text>{html_escape.escape(opf_info.title or src.stem)}</text></docTitle>\n"
                "  <navMap>\n"
                f"{ncx_points}\n"
                "  </navMap>\n"
                "</ncx>\n"
            ).encode()
            etree.fromstring(toc_ncx, parser=parser)

            # Patch OPF <manifest> and <spine toc="...">
            opf_root = opf_info.opf_root
            opf_ns = etree.QName(opf_root).namespace
            manifest_el = opf_root.find(".//{*}manifest")
            spine_el = opf_root.find(".//{*}spine")
            if manifest_el is None or spine_el is None:
                return False

            ncx_id = "ncx" if "ncx" not in opf_info.manifest else f"ncx-{uuid.uuid4().hex[:6]}"
            item_tag = f"{{{opf_ns}}}item" if opf_ns else "item"
            ncx_item = etree.SubElement(manifest_el, item_tag)
            ncx_item.set("id", ncx_id)
            ncx_item.set("href", ncx_href)
            ncx_item.set("media-type", "application/x-dtbncx+xml")
            spine_el.set("toc", ncx_id)

            patched_opf = etree.tostring(
                opf_root, encoding="utf-8", xml_declaration=True, pretty_print=True
            )
            etree.fromstring(patched_opf, parser=parser)

            dst.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dst, "w") as out_zf:
                out_zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
                for info in zf.infolist():
                    if info.filename == "mimetype":
                        continue
                    if info.filename == opf_info.opf_path:
                        out_zf.writestr(
                            opf_info.opf_path, patched_opf, compress_type=zipfile.ZIP_DEFLATED
                        )
                    elif info.filename == ncx_zip_path:
                        continue
                    else:
                        out_zf.writestr(
                            info.filename, zf.read(info.filename), compress_type=zipfile.ZIP_DEFLATED
                        )
                out_zf.writestr(ncx_zip_path, toc_ncx, compress_type=zipfile.ZIP_DEFLATED)

        post = validate_epub(dst)
        if post.valid:
            return True
        logger.debug("inplace_ncx_validation_failed", errors=post.errors)
        dst.unlink(missing_ok=True)
        return False
    except Exception as exc:
        logger.debug("inplace_ncx_inject_failed", err=str(exc))
        dst.unlink(missing_ok=True)
        return False


def normalize_epub_for_kindle(src: Path, dst: Path, cover_out: Path) -> ConvertedBookResult:
    """Repair common Send-to-Kindle E999 triggers in an existing EPUB (NCX, UTF-8, img, mimetype)."""
    title, author, language, saved_cover = _extract_epub_metadata_and_cover(src, cover_out)
    validation = validate_epub(src)
    src_size = src.stat().st_size if src.exists() else 0

    if validation.valid and src_size <= _MAX_INPLACE_EPUB_BYTES:
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

    # Fast non-destructive path: if ONLY missing toc.ncx and under 18 MB, inject in-place!
    if (
        validation.errors == [MISSING_NCX_ERROR]
        and src_size <= _MAX_INPLACE_EPUB_BYTES
        and _inject_toc_ncx_inplace(src, dst)
    ):
        post_val = validate_epub(dst)
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
            chapter_count=post_val.chapter_count,
            notes="Injected EPUB2 toc.ncx in-place for Kindle compatibility",
        )

    # Rebuild chapters cleanly into a strict Kindle-safe EPUB3
    chapters: list[_CompiledChapter] = []
    with zipfile.ZipFile(src, "r") as zf:
        opf_info = _parse_opf_structure(zf)
        html_entries: list[str] = []
        if opf_info is not None and opf_info.spine_paths:
            for sp in opf_info.spine_paths:
                if sp in opf_info.non_chapter_paths:
                    continue
                base_lower = posixpath.basename(sp).lower()
                if base_lower in ("cover.xhtml", "cover.html", "nav.xhtml", "nav.html"):
                    continue
                if sp.lower().endswith((".xhtml", ".html", ".htm")):
                    html_entries.append(sp)

        if not html_entries:
            non_ch = opf_info.non_chapter_paths if opf_info else set()
            html_entries = sorted(
                n
                for n in zf.namelist()
                if n.lower().endswith((".xhtml", ".html", ".htm"))
                and n not in non_ch
                and posixpath.basename(n).lower()
                not in ("cover.xhtml", "cover.html", "nav.xhtml", "nav.html")
            )

        # Collect archive images keyed by normalized path (prevents basename collisions)
        archive_images: dict[str, bytes] = {}
        basename_counts: Counter[str] = Counter()
        archive_by_basename: dict[str, bytes] = {}
        for name in zf.namelist():
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")):
                norm_name = posixpath.normpath(name).lstrip("/")
                data = zf.read(name)
                archive_images[norm_name] = data
                bname = posixpath.basename(norm_name)
                basename_counts[bname] += 1
                archive_by_basename[bname] = data

        # Map original normalized chapter paths to rebuilt filenames so internal links stay valid
        old_to_new_ch: dict[str, str] = {
            posixpath.normpath(entry).lstrip("/"): f"ch_{idx:04d}.xhtml"
            for idx, entry in enumerate(html_entries, start=1)
        }

        for idx, entry_name in enumerate(html_entries, start=1):
            raw_bytes = zf.read(entry_name)
            raw_text = _decode_html_bytes(raw_bytes)
            soup = _parse_xhtml_soup(raw_text)
            ch_title = _extract_chapter_title(soup, fallback=f"第 {idx} 节")[:160]
            ch_imgs: list[ProcessedImage] = []

            for img in soup.find_all("img"):
                img_src = str(img.get("src") or "").strip()
                resolved_img = _resolve_epub_path(entry_name, img_src) if img_src else ""
                img_bytes: bytes | None = archive_images.get(resolved_img)
                if img_bytes is None and img_src:
                    bname = posixpath.basename(unquote(img_src.split("#", 1)[0].split("?", 1)[0]))
                    if basename_counts.get(bname) == 1:
                        img_bytes = archive_by_basename.get(bname)
                if img_bytes is not None:
                    opt = optimize_image_bytes(img_bytes, max_width=1000)
                    if opt:
                        out_name = f"images/rebuilt_{idx:03d}_{len(ch_imgs) + 1:02d}.jpg"
                        img["src"] = out_name
                        # Clear lazy attributes BEFORE sanitize_article_html so external data-src
                        # cannot hijack our rewritten local EPUB image!
                        for lazy_attr in (
                            "data-src",
                            "data-original",
                            "data-actualsrc",
                            "data-lazy-src",
                            "srcset",
                        ):
                            img.attrs.pop(lazy_attr, None)
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

            # Rewrite or unwrap relative internal links so rebuilt EPUB has zero broken links
            for a_tag in soup.find_all("a"):
                href = str(a_tag.get("href") or "").strip()
                if not href or href.startswith("#") or _SCHEME_RE.match(href):
                    continue
                resolved_href = _resolve_epub_path(entry_name, href)
                if resolved_href in old_to_new_ch:
                    a_tag["href"] = old_to_new_ch[resolved_href]
                else:
                    a_tag.unwrap()

            body = soup.body if soup.body else soup
            clean_inner, _, _ = sanitize_article_html(body.decode_contents())
            safe_inner = _xml_safe_fragment(clean_inner)
            anchor_id = f"sec-{idx:04d}"
            full_inner, final_anchor = _ensure_leading_h1_or_prepend(
                safe_inner, ch_title, anchor_id, add_article_title_class=False
            )
            chapters.append(
                _CompiledChapter(
                    filename=f"ch_{idx:04d}.xhtml",
                    item_id=f"ch-{idx:04d}",
                    anchor_id=final_anchor,
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


def _split_markdown_to_chapters(text: str, default_title: str) -> list[tuple[str, str]]:
    """Split Markdown into chapters on top-level # / ## headings while ignoring fenced code."""
    sections: list[tuple[str, str]] = []
    cur_title = default_title
    cur_lines: list[str] = []
    in_fence = False
    fence_marker = ""
    found_heading = False

    for line in text.splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0] * len(marker)
            elif stripped.startswith(fence_marker[0] * len(fence_marker)):
                in_fence = False
                fence_marker = ""
            cur_lines.append(line)
            continue

        if not in_fence:
            heading_match = re.match(r"^(#{1,2})\s+(.+?)\s*#*\s*$", stripped)
            if heading_match:
                body = "\n".join(cur_lines).strip()
                if body:
                    sections.append((cur_title, body))
                cur_title = heading_match.group(2).strip()[:120] or default_title
                cur_lines = []
                found_heading = True
                continue

        cur_lines.append(line)

    tail = "\n".join(cur_lines).strip()
    if tail or (found_heading and not sections):
        sections.append((cur_title, tail))

    return sections or [(default_title, text.strip())]


def _join_pdf_lines(lines: list[str]) -> str:
    """Merge wrapped PDF lines cleanly for both CJK and Latin text."""
    merged = ""
    for raw_line in lines:
        part = re.sub(r"\s+", " ", raw_line).strip()
        if not part:
            continue
        if not merged:
            merged = part
            continue
        # De-hyphenate wrapped English words
        if re.search(r"[A-Za-z]-$", merged) and re.match(r"^[a-z]", part):
            merged = merged[:-1] + part
        elif _CJK_CHAR_RE.search(merged[-1]) and _CJK_CHAR_RE.search(part[0]):
            merged = merged + part
        else:
            merged = merged + " " + part
    return merged


def _normalize_band_text(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text).strip()).lower()


def _extract_pdf_page_elements(
    pdf_doc: Any,
    page_idx: int,
    *,
    body_font_size: float,
    repeating_headers: set[str],
    repeating_footers: set[str],
    seen_xrefs: dict[int, ProcessedImage],
) -> tuple[list[tuple[str, str]], list[ProcessedImage]]:
    """Extract ordered (kind, payload) elements ('h2' | 'p' | 'img') and images from one PDF page."""
    page = pdf_doc[page_idx]
    page_h = float(page.rect.height) or 1.0
    top_band = page_h * 0.10
    bottom_band = page_h * 0.90

    positioned: list[tuple[float, float, str, str]] = []
    page_images: list[ProcessedImage] = []

    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        bbox = block.get("bbox") or (0.0, 0.0, 0.0, 0.0)
        bx0, by0, _bx1, by1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))

        line_entries: list[tuple[float, float, float, float, str, float]] = []
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = "".join(str(s.get("text") or "") for s in spans).strip()
            if not line_text:
                continue
            lb = line.get("bbox") or bbox
            sizes = [float(s.get("size") or body_font_size) for s in spans if str(s.get("text") or "").strip()]
            avg_size = statistics.median(sizes) if sizes else body_font_size
            line_entries.append(
                (float(lb[0]), float(lb[1]), float(lb[2]), float(lb[3]), line_text, avg_size)
            )
        if not line_entries:
            continue

        block_full = _join_pdf_lines([le[4] for le in line_entries])
        norm_band = _normalize_band_text(block_full)

        # Strip repeating headers/footers and standalone page numbers in top/bottom 10% bands
        if by1 <= top_band and (
            norm_band in repeating_headers or _PAGE_NUMBER_RE.match(block_full.strip())
        ):
            continue
        if by0 >= bottom_band and (
            norm_band in repeating_footers or _PAGE_NUMBER_RE.match(block_full.strip())
        ):
            continue

        # Split block into real paragraphs using vertical gap & first-line indent heuristics
        para_groups: list[list[tuple[float, float, float, float, str, float]]] = [[line_entries[0]]]
        for entry in line_entries[1:]:
            prev = para_groups[-1][-1]
            prev_h = max(prev[3] - prev[1], prev[5], 8.0)
            v_gap = entry[1] - prev[3]
            indented = (entry[0] - bx0) > (body_font_size * 1.15)
            prev_ended = prev[4].rstrip().endswith(_SENTENCE_END_CHARS)
            if v_gap > prev_h * 0.65 or (indented and prev_ended):
                para_groups.append([entry])
            else:
                para_groups[-1].append(entry)

        for group in para_groups:
            para_text = _join_pdf_lines([g[4] for g in group])
            if not para_text:
                continue
            group_size = statistics.median(g[5] for g in group)
            is_heading = (
                group_size >= body_font_size * 1.25
                and len(para_text) <= 90
                and not para_text.endswith(("。", ".", ",", "，", ";", "；"))
            )
            kind = "h2" if is_heading else "p"
            positioned.append((group[0][1], group[0][0], kind, para_text))

    # Extract embedded raster images on this page
    for img_info in page.get_images(full=True):
        xref = int(img_info[0])
        if xref <= 0:
            continue
        rects = page.get_image_rects(xref)
        if rects:
            rect = rects[0]
            if rect.width < 36 or rect.height < 36:
                continue
            iy0, ix0 = float(rect.y0), float(rect.x0)
        else:
            iy0, ix0 = page_h, 0.0

        pimg = seen_xrefs.get(xref)
        if pimg is None:
            if len(seen_xrefs) >= _MAX_PDF_IMAGES:
                continue
            try:
                extracted = pdf_doc.extract_image(xref)
                raw_img = extracted.get("image")
                w = int(extracted.get("width") or 0)
                h = int(extracted.get("height") or 0)
                if not raw_img or w < 40 or h < 40:
                    continue
                opt = optimize_image_bytes(raw_img, max_width=1000)
                if opt is None:
                    continue
                pimg = ProcessedImage(
                    original_url=f"pdf://xref/{xref}",
                    filename=f"images/pdf_img_{xref}.jpg",
                    media_type="image/jpeg",
                    data=opt,
                )
                seen_xrefs[xref] = pimg
            except Exception:
                continue

        page_images.append(pimg)
        positioned.append((iy0, ix0, "img", pimg.filename))

    positioned.sort(key=lambda item: (item[0], item[1]))
    return [(kind, payload) for _y, _x, kind, payload in positioned], page_images


def _elements_to_html(elements: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for kind, payload in elements:
        if kind == "img":
            parts.append(f'<figure><img src="{html_escape.escape(payload)}" alt="插图"/></figure>')
        elif kind == "h2":
            parts.append(f"<h2>{html_escape.escape(payload)}</h2>")
        else:
            parts.append(f"<p>{html_escape.escape(payload)}</p>")
    return "\n".join(parts)


def _build_epub_from_sections(
    *,
    sections: list[tuple[str, str]],  # (chapter_title, html_fragment)
    title: str,
    author: str,
    language: str,
    dst: Path,
    cover_out: Path,
    notes: str = "",
    section_images: list[list[ProcessedImage]] | None = None,
) -> ConvertedBookResult:
    compiled: list[_CompiledChapter] = []
    for idx, (ch_title, ch_html) in enumerate(sections, start=1):
        anchor_id = f"sec-{idx:04d}"
        safe_inner = _xml_safe_fragment(ch_html)
        full_inner, final_anchor = _ensure_leading_h1_or_prepend(
            safe_inner, ch_title, anchor_id, add_article_title_class=True
        )
        ch_imgs = section_images[idx - 1] if section_images and idx - 1 < len(section_images) else []
        compiled.append(
            _CompiledChapter(
                filename=f"ch_{idx:04d}.xhtml",
                item_id=f"ch-{idx:04d}",
                anchor_id=final_anchor,
                title=ch_title,
                category="书库",
                xhtml_bytes=_wrap_xhtml_document(
                    title=ch_title, body_inner=full_inner, language=language
                ),
                images=ch_imgs,
            )
        )

    # Preserve pre-rendered cover (e.g. PDF page 1 thumbnail) if it already exists
    if cover_out.exists() and cover_out.stat().st_size > 0:
        cover_bytes = cover_out.read_bytes()
    else:
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
        md_sections = _split_markdown_to_chapters(text, title)
        html_sections = []
        for sec_title, sec_md in md_sections:
            rendered = md_lib.markdown(sec_md, extensions=["tables", "fenced_code", "toc"])
            clean_html, _, _ = sanitize_article_html(rendered)
            html_sections.append((sec_title, clean_html or "<p>（正文为空）</p>"))
        return await asyncio.to_thread(
            _build_epub_from_sections,
            sections=html_sections,
            title=title,
            author=author,
            language=lang,
            dst=dst,
            cover_out=cover_out,
            notes=f"Converted from Markdown ({len(html_sections)} chapter(s))",
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
        if _has_binary("pandoc"):
            raw_epub = output_dir / f"pandoc_tmp_{uid}.epub"
            output_dir.mkdir(parents=True, exist_ok=True)
            try:
                await _run_pandoc_docx_to_epub(src, raw_epub, title=title, author=author)
                res = await asyncio.to_thread(normalize_epub_for_kindle, raw_epub, dst, cover_out)
                if title_override.strip():
                    res.title = title_override.strip()
                if author_override.strip():
                    res.author = author_override.strip()
                res.notes = f"Converted from DOCX via Pandoc ({res.notes})"
                return res
            except Exception as exc:
                logger.warning("pandoc_docx_fallback", src=src.name, err=str(exc))
            finally:
                raw_epub.unlink(missing_ok=True)

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
                if pdf_doc.needs_pass:
                    raise ValueError("该 PDF 已加密受密码保护，请先解除密码后再上传")

                meta = pdf_doc.metadata or {}
                pdf_title = title_override.strip() or (meta.get("title") or "").strip() or title
                pdf_author = author_override.strip() or (meta.get("author") or "").strip() or author
                page_count = len(pdf_doc)

                # Save first page rendered thumbnail as cover (preserved by _build_epub_from_sections)
                if page_count > 0:
                    pix = pdf_doc[0].get_pixmap(dpi=130)
                    opt = optimize_image_bytes(pix.tobytes("png"), max_width=800)
                    if opt:
                        cover_out.parent.mkdir(parents=True, exist_ok=True)
                        cover_out.write_bytes(opt)

                # Classify scanned vs text PDF by per-page character density
                non_ws_counts: list[int] = []
                sample_texts: list[str] = []
                for page_idx in range(page_count):
                    txt = str(pdf_doc[page_idx].get_text("text")).strip()
                    non_ws = len(re.sub(r"\s+", "", txt))
                    non_ws_counts.append(non_ws)
                    if txt and len(sample_texts) < 10:
                        sample_texts.append(txt)

                median_chars = statistics.median(non_ws_counts) if non_ws_counts else 0
                sparse_ratio = (
                    sum(1 for c in non_ws_counts if c < 40) / len(non_ws_counts)
                    if non_ws_counts
                    else 1.0
                )
                if median_chars < 80 or sparse_ratio > 0.60:
                    # Scanned / image-heavy magazine PDF: preserve original PDF for native Kindle push
                    pdf_dst = output_dir / f"{src.stem}_{uid}.pdf"
                    pdf_dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, pdf_dst)
                    return ConvertedBookResult(
                        title=pdf_title,
                        author=pdf_author,
                        language=detect_language("\n".join(sample_texts)) if sample_texts else "zh-CN",
                        kindle_epub_path=pdf_dst,
                        cover_path=cover_out if cover_out.exists() else None,
                        file_size_bytes=pdf_dst.stat().st_size,
                        chapter_count=page_count,
                        notes="Scanned/layout PDF preserved directly (Amazon Send to Kindle supports native PDF)",
                    )

                # First pass: estimate body font size and detect repeating top/bottom bands
                span_sizes: list[float] = []
                top_band_counter: Counter[str] = Counter()
                bottom_band_counter: Counter[str] = Counter()
                for page_idx in range(page_count):
                    page = pdf_doc[page_idx]
                    page_h = float(page.rect.height) or 1.0
                    seen_top_page: set[str] = set()
                    seen_bottom_page: set[str] = set()
                    for block in page.get_text("dict").get("blocks", []):
                        if block.get("type") != 0:
                            continue
                        bb = block.get("bbox") or (0.0, 0.0, 0.0, 0.0)
                        by0, by1 = float(bb[1]), float(bb[3])
                        lines_txt: list[str] = []
                        for line in block.get("lines", []):
                            for sp in line.get("spans", []):
                                st = str(sp.get("text") or "").strip()
                                if st:
                                    lines_txt.append(st)
                                    if len(span_sizes) < 4000:
                                        span_sizes.append(float(sp.get("size") or 11.0))
                        btxt = _normalize_band_text(" ".join(lines_txt))
                        if not btxt:
                            continue
                        if by1 <= page_h * 0.10:
                            seen_top_page.add(btxt)
                        elif by0 >= page_h * 0.90:
                            seen_bottom_page.add(btxt)
                    for item in seen_top_page:
                        top_band_counter[item] += 1
                    for item in seen_bottom_page:
                        bottom_band_counter[item] += 1

                body_font_size = statistics.median(span_sizes) if span_sizes else 11.0
                repeat_threshold = max(2, int(page_count * 0.4)) if page_count >= 3 else page_count + 1
                repeating_headers = {
                    k for k, v in top_band_counter.items() if v >= repeat_threshold
                }
                repeating_footers = {
                    k for k, v in bottom_band_counter.items() if v >= repeat_threshold
                }

                # Second pass: extract structured paragraphs + embedded images per page
                seen_xrefs: dict[int, ProcessedImage] = {}
                per_page_elements: list[list[tuple[str, str]]] = []
                per_page_images: list[list[ProcessedImage]] = []
                for page_idx in range(page_count):
                    els, imgs = _extract_pdf_page_elements(
                        pdf_doc,
                        page_idx,
                        body_font_size=body_font_size,
                        repeating_headers=repeating_headers,
                        repeating_footers=repeating_footers,
                        seen_xrefs=seen_xrefs,
                    )
                    per_page_elements.append(els)
                    per_page_images.append(imgs)

                # Build chapters using PDF bookmarks (TOC) if available
                raw_toc = pdf_doc.get_toc(simple=True) or []
                valid_toc = [
                    (int(entry[0]), str(entry[1]).strip(), int(entry[2]))
                    for entry in raw_toc
                    if len(entry) >= 3
                    and str(entry[1]).strip()
                    and 1 <= int(entry[2]) <= page_count
                ]

                html_sections: list[tuple[str, str]] = []
                section_imgs: list[list[ProcessedImage]] = []

                if valid_toc:
                    min_level = min(e[0] for e in valid_toc)
                    top_toc = [e for e in valid_toc if e[0] == min_level]
                    chosen_toc = top_toc if len(top_toc) >= 2 else [
                        e for e in valid_toc if e[0] <= min_level + 1
                    ]
                    # Deduplicate same start page keeping earliest bookmark title
                    toc_points: list[tuple[int, str]] = []
                    seen_pages: set[int] = set()
                    for _lvl, t_title, p1 in sorted(chosen_toc, key=lambda x: x[2]):
                        p0 = p1 - 1
                        if p0 not in seen_pages:
                            seen_pages.add(p0)
                            toc_points.append((p0, t_title[:120]))

                    if toc_points and toc_points[0][0] > 0:
                        intro_els: list[tuple[str, str]] = []
                        intro_imgs: list[ProcessedImage] = []
                        for p in range(0, toc_points[0][0]):
                            intro_els.extend(per_page_elements[p])
                            intro_imgs.extend(per_page_images[p])
                        if intro_els:
                            html_sections.append(("前言", _elements_to_html(intro_els)))
                            section_imgs.append(intro_imgs)

                    for i, (start_p, sec_title) in enumerate(toc_points):
                        end_p = toc_points[i + 1][0] if i + 1 < len(toc_points) else page_count
                        sec_els: list[tuple[str, str]] = []
                        sec_im: list[ProcessedImage] = []
                        for p in range(start_p, end_p):
                            sec_els.extend(per_page_elements[p])
                            sec_im.extend(per_page_images[p])
                        if sec_els:
                            html_sections.append((sec_title, _elements_to_html(sec_els)))
                            section_imgs.append(sec_im)

                if not html_sections:
                    # Merge across pages and split on chapter headings or ~12,000 char chunks
                    all_els: list[tuple[str, str]] = []
                    img_by_fname: dict[str, ProcessedImage] = {}
                    for els, imgs in zip(per_page_elements, per_page_images, strict=True):
                        for im in imgs:
                            img_by_fname[im.filename] = im
                        # Merge sentence split across page boundary when natural
                        if (
                            all_els
                            and els
                            and all_els[-1][0] == "p"
                            and els[0][0] == "p"
                            and not all_els[-1][1].rstrip().endswith(_SENTENCE_END_CHARS)
                            and not CHAPTER_HEADING_RE.match(els[0][1])
                        ):
                            merged_p = _join_pdf_lines([all_els[-1][1], els[0][1]])
                            all_els[-1] = ("p", merged_p)
                            all_els.extend(els[1:])
                        else:
                            all_els.extend(els)

                    heading_indices = [
                        idx
                        for idx, (kind, payload) in enumerate(all_els)
                        if kind in ("p", "h2") and CHAPTER_HEADING_RE.match(payload.strip())
                    ]
                    if len(heading_indices) >= 2:
                        if heading_indices[0] > 0:
                            pre_els = all_els[: heading_indices[0]]
                            html_sections.append(("前言", _elements_to_html(pre_els)))
                            section_imgs.append(
                                [
                                    img_by_fname[p]
                                    for k, p in pre_els
                                    if k == "img" and p in img_by_fname
                                ]
                            )
                        for idx_pos, start_i in enumerate(heading_indices):
                            end_i = (
                                heading_indices[idx_pos + 1]
                                if idx_pos + 1 < len(heading_indices)
                                else len(all_els)
                            )
                            sec_title = all_els[start_i][1].strip()[:120]
                            body_els = all_els[start_i + 1 : end_i]
                            if body_els:
                                html_sections.append((sec_title, _elements_to_html(body_els)))
                                section_imgs.append(
                                    [
                                        img_by_fname[p]
                                        for k, p in body_els
                                        if k == "img" and p in img_by_fname
                                    ]
                                )
                    else:
                        buf_els: list[tuple[str, str]] = []
                        buf_chars = 0
                        for el in all_els:
                            buf_els.append(el)
                            if el[0] != "img":
                                buf_chars += len(el[1])
                            if buf_chars >= 12000:
                                label = f"{pdf_title} ({len(html_sections) + 1})"
                                html_sections.append((label, _elements_to_html(buf_els)))
                                section_imgs.append(
                                    [
                                        img_by_fname[p]
                                        for k, p in buf_els
                                        if k == "img" and p in img_by_fname
                                    ]
                                )
                                buf_els = []
                                buf_chars = 0
                        if buf_els:
                            label = (
                                pdf_title
                                if not html_sections
                                else f"{pdf_title} ({len(html_sections) + 1})"
                            )
                            html_sections.append((label, _elements_to_html(buf_els)))
                            section_imgs.append(
                                [
                                    img_by_fname[p]
                                    for k, p in buf_els
                                    if k == "img" and p in img_by_fname
                                ]
                            )

                if not html_sections:
                    html_sections = [(pdf_title, "<p>（正文为空）</p>")]
                    section_imgs = [[]]

                lang = detect_language("\n".join(sample_texts))
                return _build_epub_from_sections(
                    sections=html_sections,
                    title=pdf_title,
                    author=pdf_author,
                    language=lang,
                    dst=dst,
                    cover_out=cover_out,
                    notes=f"Reflowed text PDF ({page_count} pages, {len(seen_xrefs)} image(s)) to Kindle EPUB3",
                    section_images=section_imgs,
                )
            finally:
                pdf_doc.close()

        return await asyncio.to_thread(_convert_pdf)

    # MOBI / AZW3 / FB2 / CBZ / RTF via Calibre ebook-convert
    if _has_binary("ebook-convert"):
        raw_epub = output_dir / f"calibre_tmp_{uid}.epub"
        output_dir.mkdir(parents=True, exist_ok=True)
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
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = zf.namelist()
            opf_info = _parse_opf_structure(zf)
            ordered_files: list[str] = []
            if opf_info is not None and opf_info.spine_paths:
                for path in opf_info.spine_paths:
                    if path in opf_info.non_chapter_paths:
                        continue
                    base_lower = posixpath.basename(path).lower()
                    if base_lower in ("cover.xhtml", "cover.html", "nav.xhtml", "nav.html"):
                        continue
                    if path.lower().endswith((".xhtml", ".html", ".htm")):
                        ordered_files.append(path)

            if not ordered_files:
                non_ch = opf_info.non_chapter_paths if opf_info else set()
                ordered_files = sorted(
                    n
                    for n in names
                    if n.lower().endswith((".xhtml", ".html", ".htm"))
                    and n not in non_ch
                    and posixpath.basename(n).lower()
                    not in ("cover.xhtml", "cover.html", "nav.xhtml", "nav.html")
                )

            for idx, fname in enumerate(ordered_files):
                raw = _decode_html_bytes(zf.read(fname))
                soup = _parse_xhtml_soup(raw)
                title = _extract_chapter_title(soup, fallback=f"Chapter {idx + 1}")[:180]
                chapters.append({"index": idx, "filename": fname, "title": title})
    except zipfile.BadZipFile:
        return []
    return chapters


def read_epub_chapter_html(
    epub_path: Path,
    chapter_index: int,
    book_id: int,
    *,
    asset_url_prefix: str | None = None,
    asset_query_suffix: str = "",
) -> dict[str, object]:
    """Return strictly sanitized HTML of an EPUB chapter with rewritten asset & chapter URLs."""
    if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
        raise ValueError(f"EPUB file is unavailable or not an EPUB: {epub_path.name}")
    if not zipfile.is_zipfile(epub_path):
        raise ValueError(f"Invalid or corrupted EPUB file: {epub_path.name}")

    chapters = list_epub_chapters(epub_path)
    if chapter_index < 0 or chapter_index >= len(chapters):
        raise IndexError("Chapter index out of range")

    fname = str(chapters[chapter_index]["filename"])
    chapter_index_by_path: dict[str, int] = {
        posixpath.normpath(str(ch["filename"])).lstrip("/"): int(str(ch["index"]))
        for ch in chapters
    }
    eff_prefix = (asset_url_prefix or f"/api/v1/books/{book_id}/asset").rstrip("/")

    with zipfile.ZipFile(epub_path, "r") as zf:
        zf_names = {posixpath.normpath(n).lstrip("/") for n in zf.namelist()}
        raw = _decode_html_bytes(zf.read(fname))
        soup = _parse_xhtml_soup(raw)

        # Strip executable/embedded tags before rewriting & nh3 sanitization
        for bad in soup.find_all(["script", "style", "iframe", "object", "embed", "noscript", "svg"]):
            bad.decompose()

        # Strip .chapter-nav blocks (web reader renders its own navigation)
        for div in list(soup.find_all("div")):
            cls_attr = div.get("class")
            cls_tokens = (
                cls_attr if isinstance(cls_attr, list) else str(cls_attr or "").split()
            )
            if "chapter-nav" in cls_tokens:
                div.decompose()

        # Normalize relative image src -> authenticated asset endpoint
        for img in soup.find_all("img"):
            for lazy_attr in ("data-src", "data-original", "data-actualsrc", "data-lazy-src", "srcset"):
                img.attrs.pop(lazy_attr, None)
            src = str(img.get("src") or "").strip()
            if not src or src.lower().startswith(("javascript:", "vbscript:", "data:")):
                img.decompose()
                continue
            if not src.startswith(("http://", "https://")):
                resolved = _resolve_epub_path(fname, src)
                if resolved.startswith("..") or resolved not in zf_names:
                    img.decompose()
                    continue
                img["src"] = f"{eff_prefix}/{quote(resolved, safe='/')}{asset_query_suffix}"

        # Rewrite intra-EPUB chapter links into data-chapter-index attributes
        for a_tag in soup.find_all("a"):
            href = str(a_tag.get("href") or "").strip()
            if not href:
                continue
            lower = href.lower()
            if lower.startswith(("javascript:", "vbscript:", "data:")):
                a_tag.unwrap()
                continue
            if lower.startswith(("http://", "https://", "mailto:")) or href.startswith("#"):
                continue
            resolved_href = _resolve_epub_path(fname, href)
            if resolved_href in chapter_index_by_path:
                target_idx = chapter_index_by_path[resolved_href]
                a_tag["data-chapter-index"] = str(target_idx)
                a_tag["href"] = f"#chapter-{target_idx}"
            else:
                a_tag.unwrap()

        body = soup.body if soup.body else soup
        raw_inner = body.decode_contents()

    html_out = nh3.clean(
        raw_inner,
        tags=READER_ALLOWED_TAGS,
        attributes=READER_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        clean_content_tags={"script", "style"},
        strip_comments=True,
    )
    return {
        "index": chapter_index,
        "total": len(chapters),
        "title": chapters[chapter_index]["title"],
        "html": html_out,
    }
