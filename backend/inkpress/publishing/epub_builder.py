from __future__ import annotations

import html as html_escape
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import qrcode
from bs4 import BeautifulSoup, Tag
from lxml import etree
from lxml import html as lxml_html

from inkpress.ingestion.extractor import estimate_word_count_and_time
from inkpress.publishing.cover_generator import generate_editorial_cover
from inkpress.publishing.epub_validator import validate_epub
from inkpress.publishing.image_pipeline import ProcessedImage, download_and_optimize_images

MAX_IMAGES_PER_CHAPTER = 60
MAX_IMAGES_TOTAL = 220

KINDLE_PAPER_CSS = """@charset "utf-8";
body {
  line-height: 1.75;
  margin: 0;
  padding: 0 0.4em;
  text-align: justify;
  word-wrap: break-word;
}
h1, h2, h3, h4 {
  line-height: 1.35;
  margin-top: 1.2em;
  margin-bottom: 0.55em;
  text-align: left;
  page-break-after: avoid;
}
h1.article-title {
  font-size: 1.55em;
  border-bottom: 1px solid #999999;
  padding-bottom: 0.35em;
  margin-top: 0.4em;
}
.article-meta {
  font-size: 0.85em;
  color: #555555;
  margin-bottom: 1.2em;
  text-align: left;
}
.ai-summary-box {
  border-left: 3px solid #333333;
  background-color: #f4f4f4;
  padding: 0.65em 0.9em;
  margin: 0.9em 0 1.3em 0;
  font-size: 0.92em;
}
.ai-summary-label {
  font-weight: bold;
  margin-bottom: 0.3em;
  display: block;
}
p {
  margin: 0 0 0.95em 0;
  text-indent: 2em;
}
p.no-indent, .article-meta p, .ai-summary-box p, blockquote p, li p {
  text-indent: 0;
}
p.bilingual-tr {
  color: #4a4a4a;
  border-left: 2px solid #bbbbbb;
  padding-left: 0.7em;
  text-indent: 0;
  margin-top: 0.2em;
  margin-bottom: 1.1em;
}
blockquote {
  border-left: 2px solid #888888;
  margin: 1em 0.5em;
  padding: 0.2em 0.9em;
  color: #333333;
}
pre, code {
  font-family: monospace;
  font-size: 0.88em;
}
pre {
  white-space: pre-wrap;
  background-color: #f5f5f5;
  padding: 0.6em;
  border: 1px solid #dddddd;
}
img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 0.9em auto;
}
img.sliced-img {
  margin-top: 0.25em;
}
figure {
  margin: 1em 0;
  text-align: center;
}
figcaption {
  font-size: 0.82em;
  color: #666666;
  margin-top: 0.3em;
}
table {
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0;
  font-size: 0.9em;
}
th, td {
  border: 1px solid #bbbbbb;
  padding: 0.35em 0.5em;
  text-align: left;
}
.chapter-nav {
  margin-top: 2em;
  padding-top: 0.8em;
  border-top: 1px solid #cccccc;
  font-size: 0.85em;
  text-align: center;
}
.qr-footer {
  margin-top: 1.4em;
  text-align: center;
  font-size: 0.8em;
  color: #666666;
}
.qr-footer img {
  width: 120px;
  height: 120px;
}
.digest-item {
  margin-bottom: 1.1em;
  padding-bottom: 0.7em;
  border-bottom: 1px dotted #cccccc;
}
.tugua-item {
  margin-bottom: 1.55em;
  padding-bottom: 1.05em;
  border-bottom: 1px dotted #cccccc;
}
.tugua-title {
  font-size: 1.06em;
  line-height: 1.45;
  margin: 0 0 0.5em 0;
  text-indent: 0;
  text-align: left;
  page-break-after: avoid;
}
.tugua-figure {
  margin: 0.75em 0 0.35em 0;
  text-align: center;
}
.meta-tag {
  font-size: 0.8em;
  color: #666666;
}
"""


def _safe_xml_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


@dataclass
class EpubChapterInput:
    title: str
    source_name: str
    category: str
    author: str
    published_at: datetime
    canonical_url: str
    body_html: str
    ai_summary: str | None = None
    bilingual_html: str | None = None
    language: str = "zh-CN"
    image_referers: dict[str, str] = field(default_factory=dict)
    reading_time_min: int | None = None


@dataclass
class BuiltEpubVolume:
    path: Path
    title: str
    volume_index: int
    total_volumes: int
    article_count: int
    size_bytes: int
    cover_bytes: bytes


@dataclass
class _CompiledChapter:
    filename: str
    item_id: str
    anchor_id: str
    title: str
    category: str
    xhtml_bytes: bytes
    images: list[ProcessedImage] = field(default_factory=list)
    reading_time_min: int = 0


@dataclass
class _DraftChapter:
    filename: str
    item_id: str
    anchor_id: str
    title: str
    category: str
    language: str
    body_without_nav: str
    images: list[ProcessedImage] = field(default_factory=list)
    reading_time_min: int = 0

    def estimated_bytes(self) -> int:
        return len(self.body_without_nav.encode("utf-8")) + 512 + sum(
            len(img.data) for img in self.images
        )


_DEAD_LINK_TEXTS = frozenset({"原文链接", "阅读原文", "查看原文", "点击查看全文", "查看详情"})


def _norm_heading_cmp(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


def _strip_duplicate_leading_heading(soup: BeautifulSoup, chapter_title: str) -> None:
    """Remove redundant leading <h1>/<h2>/<h3>/<p> inside extracted HTML that repeats the chapter header."""
    norm_title = _norm_heading_cmp(chapter_title)
    if not norm_title:
        return
    body = soup.body if soup.body else soup
    for child in list(body.children):
        if not isinstance(child, Tag):
            if str(child).strip():
                break
            continue
        if child.name in ("h1", "h2", "h3", "p") and not child.find("img"):
            heading_txt = _norm_heading_cmp(child.get_text(strip=True))
            if heading_txt and heading_txt == norm_title:
                child.decompose()
        break


def _process_body_links(soup: BeautifulSoup, *, strip_links: bool, base_url: str = "") -> None:
    """Strip or normalize hyperlinks inside article content before building XHTML."""
    for a_tag in list(soup.find_all("a")):
        if strip_links:
            link_txt = a_tag.get_text(" ", strip=True)
            parent = a_tag.parent
            if link_txt in _DEAD_LINK_TEXTS and not a_tag.find("img"):
                a_tag.decompose()
                if (
                    parent is not None
                    and parent.name == "p"
                    and not parent.get_text(strip=True)
                    and not parent.find("img")
                ):
                    parent.decompose()
            else:
                a_tag.unwrap()
            continue
        href = str(a_tag.get("href") or "").strip()
        if not href:
            a_tag.unwrap()
            continue
        if href.startswith("#"):
            continue
        lower = href.lower()
        if lower.startswith(("http://", "https://", "mailto:")):
            continue
        if base_url.startswith(("http://", "https://")) and not lower.startswith(
            ("javascript:", "vbscript:", "data:")
        ):
            resolved = urljoin(base_url, href)
            if resolved.startswith(("http://", "https://")):
                a_tag["href"] = resolved
                continue
        # Unwrap unsafe schemes or unresolved relative links that would break EPUB validation
        a_tag.unwrap()


def _xml_safe_fragment(raw_html: str) -> str:
    """Convert arbitrary HTML fragment into strictly well-formed XHTML fragment."""
    if not raw_html or not raw_html.strip():
        return "<p>（正文为空）</p>"
    # Remove control characters invalid in XML 1.0
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_html)
    try:
        container = lxml_html.fragment_fromstring(cleaned, create_parent="div")
        # Strip any residual event attributes or empty images
        for elem in container.iter():
            for attr in list(elem.attrib.keys()):
                if attr.startswith("on") or ":" in attr:
                    del elem.attrib[attr]
        for img in container.xpath(".//img"):
            if not (img.get("src") or "").strip():
                parent = img.getparent()
                if parent is not None:
                    parent.remove(img)
        serialized = etree.tostring(container, encoding="unicode", method="xml")
        # Unwrap outer <div>...</div>
        if serialized.startswith("<div>") and serialized.endswith("</div>"):
            return serialized[5:-6]
        return serialized
    except Exception:
        return (
            f"<p>{html_escape.escape(BeautifulSoup(cleaned, 'lxml').get_text(' ', strip=True))}</p>"
        )


def _wrap_xhtml_document(
    *,
    title: str,
    body_inner: str,
    language: str = "zh-CN",
) -> bytes:
    """Produce strictly valid UTF-8 XHTML5 document compliant with Send to Kindle."""
    safe_title = html_escape.escape(title)
    safe_lang = html_escape.escape(language or "zh-CN")
    doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{safe_lang}" lang="{safe_lang}">\n'
        "<head>\n"
        '  <meta charset="utf-8"/>\n'
        f"  <title>{safe_title}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="styles/paper.css"/>\n'
        "</head>\n"
        "<body>\n"
        f"{body_inner}\n"
        "</body>\n"
        "</html>\n"
    )
    raw = doc.encode("utf-8")
    # Guarantee XML well-formedness before writing to EPUB
    etree.fromstring(raw, parser=_safe_xml_parser())
    return raw


def _is_external_scannable_url(url: str) -> bool:
    """Return True iff URL is a real external HTTP(S) URL suitable for a reader-facing QR code."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return False
    return not (host.endswith(".local") or host.endswith(".localhost"))


def _make_qr_jpeg(url: str) -> bytes:
    qr = qrcode.QRCode(box_size=4, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _write_strict_epub_zip(
    output_path: Path,
    *,
    book_id: str,
    title: str,
    author: str,
    language: str,
    issue_date: datetime,
    cover_bytes: bytes,
    chapters: list[_CompiledChapter],
) -> None:
    """Write a spec-perfect EPUB3 + EPUB2 NCX archive with uncompressed leading mimetype."""
    safe_title = html_escape.escape(title)
    safe_author = html_escape.escape(author or "InkPress")
    safe_lang = html_escape.escape(language or "zh-CN")
    utc_date = (
        issue_date.astimezone(UTC) if issue_date.tzinfo is not None else issue_date.replace(tzinfo=UTC)
    )
    iso_date = utc_date.strftime("%Y-%m-%dT%H:%M:%SZ")
    parser = _safe_xml_parser()

    # Deduplicate embedded images across chapters
    images_by_name: dict[str, ProcessedImage] = {}
    for ch in chapters:
        for img in ch.images:
            images_by_name[img.filename] = img

    # Cover XHTML page
    cover_xhtml = _wrap_xhtml_document(
        title=f"{title} - 封面",
        language=language,
        body_inner=(
            '<div style="text-align:center;margin:0;padding:0;">'
            '<img src="images/cover.jpg" alt="Cover" style="max-height:98%;max-width:100%;margin:0 auto;"/>'
            "</div>"
        ),
    )

    def _toc_label(ch: _CompiledChapter) -> str:
        if ch.reading_time_min > 0:
            return f"{ch.title}（{ch.reading_time_min} 分钟）"
        return ch.title

    # EPUB3 nav.xhtml (with estimated reading time & Kindle KFX landmarks)
    nav_items = "\n".join(
        f'      <li><a href="{ch.filename}#{ch.anchor_id}">{html_escape.escape(_toc_label(ch))}</a></li>'
        for ch in chapters
    )
    bodymatter_href = chapters[0].filename if chapters else "nav.xhtml"
    landmarks_block = (
        '  <nav epub:type="landmarks" id="landmarks" hidden="hidden">\n'
        "    <h2>导览</h2>\n"
        "    <ol>\n"
        '      <li><a epub:type="cover" href="cover.xhtml">封面</a></li>\n'
        '      <li><a epub:type="toc" href="nav.xhtml">目录</a></li>\n'
        f'      <li><a epub:type="bodymatter" href="{bodymatter_href}">正文</a></li>\n'
        "    </ol>\n"
        "  </nav>\n"
    )
    nav_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{safe_lang}" lang="{safe_lang}">\n'
        "<head>\n"
        '  <meta charset="utf-8"/>\n'
        f"  <title>{safe_title} · 目录</title>\n"
        '  <link rel="stylesheet" type="text/css" href="styles/paper.css"/>\n'
        "</head>\n"
        "<body>\n"
        '  <nav epub:type="toc" id="toc">\n'
        f'    <h1 id="toc-heading">{safe_title} · 目录</h1>\n'
        "    <ol>\n"
        f"{nav_items}\n"
        "    </ol>\n"
        "  </nav>\n"
        f"{landmarks_block}"
        "</body>\n"
        "</html>\n"
    ).encode()
    etree.fromstring(nav_xhtml, parser=parser)

    # EPUB2 toc.ncx
    ncx_points = "\n".join(
        f'    <navPoint id="navPoint-{idx}" playOrder="{idx}">\n'
        f"      <navLabel><text>{html_escape.escape(_toc_label(ch))}</text></navLabel>\n"
        f'      <content src="{ch.filename}#{ch.anchor_id}"/>\n'
        "    </navPoint>"
        for idx, ch in enumerate(chapters, start=1)
    )
    toc_ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        "  <head>\n"
        f'    <meta name="dtb:uid" content="{html_escape.escape(book_id)}"/>\n'
        '    <meta name="dtb:depth" content="1"/>\n'
        '    <meta name="dtb:totalPageCount" content="0"/>\n'
        '    <meta name="dtb:maxPageNumber" content="0"/>\n'
        "  </head>\n"
        f"  <docTitle><text>{safe_title}</text></docTitle>\n"
        "  <navMap>\n"
        f"{ncx_points}\n"
        "  </navMap>\n"
        "</ncx>\n"
    ).encode()
    etree.fromstring(toc_ncx, parser=parser)

    # content.opf
    manifest_lines = [
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="css" href="styles/paper.css" media-type="text/css"/>',
        '    <item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
        '    <item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_lines = [
        '    <itemref idref="cover-page" linear="no"/>',
        '    <itemref idref="nav"/>',
    ]
    for ch in chapters:
        manifest_lines.append(
            f'    <item id="{ch.item_id}" href="{ch.filename}" media-type="application/xhtml+xml"/>'
        )
        spine_lines.append(f'    <itemref idref="{ch.item_id}"/>')
    for idx, (fname, img) in enumerate(images_by_name.items(), start=1):
        manifest_lines.append(
            f'    <item id="img-{idx}" href="{fname}" media-type="{img.media_type}"/>'
        )

    content_opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{html_escape.escape(book_id)}</dc:identifier>\n'
        f"    <dc:title>{safe_title}</dc:title>\n"
        f"    <dc:creator>{safe_author}</dc:creator>\n"
        f"    <dc:language>{safe_lang}</dc:language>\n"
        f"    <dc:date>{iso_date}</dc:date>\n"
        f'    <meta property="dcterms:modified">{iso_date}</meta>\n'
        '    <meta name="cover" content="cover-image"/>\n'
        "  </metadata>\n"
        "  <manifest>\n" + "\n".join(manifest_lines) + "\n  </manifest>\n"
        '  <spine toc="ncx">\n' + "\n".join(spine_lines) + "\n  </spine>\n"
        "  <guide>\n"
        '    <reference type="cover" title="封面" href="cover.xhtml"/>\n'
        '    <reference type="toc" title="目录" href="nav.xhtml"/>\n'
        "  </guide>\n"
        "</package>\n"
    ).encode("utf-8")
    etree.fromstring(content_opf, parser=parser)

    container_xml = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        b"  <rootfiles>\n"
        b'    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        b"  </rootfiles>\n"
        b"</container>\n"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as zf:
        # 1. mimetype MUST be first and ZIP_STORED (uncompressed)
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", content_opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", toc_ncx, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr(
            "OEBPS/styles/paper.css",
            KINDLE_PAPER_CSS.encode("utf-8"),
            compress_type=zipfile.ZIP_DEFLATED,
        )
        zf.writestr("OEBPS/cover.xhtml", cover_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/images/cover.jpg", cover_bytes, compress_type=zipfile.ZIP_DEFLATED)
        for ch in chapters:
            zf.writestr(f"OEBPS/{ch.filename}", ch.xhtml_bytes, compress_type=zipfile.ZIP_DEFLATED)
        for fname, img in images_by_name.items():
            zf.writestr(f"OEBPS/{fname}", img.data, compress_type=zipfile.ZIP_DEFLATED)


async def build_kindle_edition_epubs(
    *,
    output_dir: Path,
    edition_title: str,
    subtitle: str,
    issue_number: int,
    issue_date: datetime,
    chapters_input: list[EpubChapterInput],
    briefing_html: str = "",
    cover_style: str = "editorial",
    max_mb_per_volume: int = 18,
    grayscale_images: bool = False,
    include_qr_links: bool = False,
    outbound_proxy: str = "",
    download_images: bool = True,
    strip_links: bool = True,
) -> list[BuiltEpubVolume]:
    """Compile articles into one or more Send-to-Kindle-validated EPUB3 volumes."""
    if not chapters_input and not briefing_html:
        raise ValueError("Cannot build EPUB with zero articles and no briefing")

    # 1. Gather per-chapter image URLs with canonical_url Referer and fair round-robin cap
    per_chapter_img_items: list[list[tuple[str, str]]] = []
    if download_images:
        for ch in chapters_input:
            source_html = ch.bilingual_html or ch.body_html
            soup = BeautifulSoup(source_html, "lxml")
            ch_urls: list[tuple[str, str]] = []
            seen_in_ch: set[str] = set()
            ref = ch.canonical_url if ch.canonical_url.startswith(("http://", "https://")) else ""
            for img in soup.find_all("img"):
                src = str(img.get("src") or "").strip()
                if src.startswith(("http://", "https://")) and src not in seen_in_ch:
                    seen_in_ch.add(src)
                    item_ref = ch.image_referers.get(src, ref)
                    ch_urls.append((src, item_ref))
                    if len(ch_urls) >= MAX_IMAGES_PER_CHAPTER:
                        break
            per_chapter_img_items.append(ch_urls)

    img_items_to_fetch: list[tuple[str, str]] = []
    seen_global: set[str] = set()
    if per_chapter_img_items:
        max_depth = max((len(lst) for lst in per_chapter_img_items), default=0)
        for slot in range(max_depth):
            for lst in per_chapter_img_items:
                if slot < len(lst):
                    img_url, ref = lst[slot]
                    if img_url not in seen_global:
                        seen_global.add(img_url)
                        img_items_to_fetch.append((img_url, ref))
                        if len(img_items_to_fetch) >= MAX_IMAGES_TOTAL:
                            break
            if len(img_items_to_fetch) >= MAX_IMAGES_TOTAL:
                break

    downloaded = (
        await download_and_optimize_images(
            img_items_to_fetch,
            outbound_proxy=outbound_proxy,
            grayscale=grayscale_images,
            max_images=MAX_IMAGES_TOTAL,
        )
        if img_items_to_fetch
        else {}
    )

    # 2. Prepare draft chapters WITHOUT .chapter-nav so cross-volume links cannot break
    drafts: list[_DraftChapter] = []

    if briefing_html.strip():
        brief_soup = BeautifulSoup(briefing_html, "lxml")
        _process_body_links(brief_soup, strip_links=strip_links)
        brief_root = brief_soup.body if brief_soup.body else brief_soup
        safe_brief = _xml_safe_fragment(brief_root.decode_contents())
        _, brief_mins = estimate_word_count_and_time(brief_soup.get_text(" ", strip=True))
        brief_body = (
            '<h1 id="ch-briefing" class="article-title">今日头版综述</h1>\n'
            f'<div class="article-meta">{html_escape.escape(edition_title)} · 第 {issue_number} 期 · '
            f"{issue_date.strftime('%Y-%m-%d')}</div>\n"
            f"{safe_brief}"
        )
        drafts.append(
            _DraftChapter(
                filename="ch_000_briefing.xhtml",
                item_id="ch-000",
                anchor_id="ch-briefing",
                title="今日头版综述",
                category="导读",
                language="zh-CN",
                body_without_nav=brief_body,
                reading_time_min=brief_mins,
            )
        )

    for idx, ch in enumerate(chapters_input, start=1):
        source_html = ch.bilingual_html or ch.body_html
        soup = BeautifulSoup(source_html, "lxml")
        ch_images: list[ProcessedImage] = []
        embedded_in_ch = 0

        for img in list(soup.find_all("img")):
            src = str(img.get("src") or "").strip()
            if src in downloaded and embedded_in_ch < MAX_IMAGES_PER_CHAPTER:
                pimg = downloaded[src]
                img["src"] = pimg.filename
                ch_images.append(pimg)
                embedded_in_ch += 1
                if pimg.extra_slices:
                    anchor_node = img
                    alt_base = str(img.get("alt") or "").strip()
                    total_parts = 1 + len(pimg.extra_slices)
                    for s_idx, s_img in enumerate(pimg.extra_slices, start=2):
                        if embedded_in_ch >= MAX_IMAGES_PER_CHAPTER:
                            break
                        slice_tag = soup.new_tag(
                            "img",
                            attrs={
                                "src": s_img.filename,
                                "alt": f"{alt_base} ({s_idx}/{total_parts})".strip(),
                                "class": "sliced-img",
                            },
                        )
                        anchor_node.insert_after(slice_tag)
                        anchor_node = slice_tag
                        ch_images.append(s_img)
                        embedded_in_ch += 1
            else:
                img.decompose()

        _strip_duplicate_leading_heading(soup, ch.title)
        _process_body_links(soup, strip_links=strip_links, base_url=ch.canonical_url)

        body_root = soup.body if soup.body else soup
        safe_body = _xml_safe_fragment(body_root.decode_contents())

        est_minutes = (
            ch.reading_time_min
            if (ch.reading_time_min is not None and ch.reading_time_min > 0)
            else estimate_word_count_and_time(soup.get_text(" ", strip=True))[1]
        )

        anchor_id = f"ch-{idx:03d}-h"
        summary_block = ""
        if ch.ai_summary:
            summary_block = (
                '<div class="ai-summary-box">'
                '<span class="ai-summary-label">AI 导读</span>'
                f"<p>{html_escape.escape(ch.ai_summary)}</p>"
                "</div>\n"
            )

        distinct_author = (
            ch.author.strip()
            if ch.author.strip()
            and ch.author.strip().lower() != (ch.source_name or "").strip().lower()
            else ""
        )
        meta_parts = [
            p
            for p in (
                ch.category,
                ch.source_name,
                distinct_author,
                ch.published_at.strftime("%Y-%m-%d %H:%M"),
            )
            if p
        ]
        meta_line = html_escape.escape(" · ".join(meta_parts))

        qr_block = ""
        if include_qr_links and _is_external_scannable_url(ch.canonical_url):
            qr_bytes = _make_qr_jpeg(ch.canonical_url)
            qr_name = f"images/qr_{idx:03d}.jpg"
            ch_images.append(
                ProcessedImage(
                    original_url=ch.canonical_url,
                    filename=qr_name,
                    media_type="image/jpeg",
                    data=qr_bytes,
                )
            )
            qr_block = (
                '<div class="qr-footer">'
                f'<img src="{qr_name}" alt="原文二维码"/>'
                '<p class="no-indent">手机扫码查看原文</p>'
                "</div>\n"
            )

        body_without_nav = (
            f'<h1 id="{anchor_id}" class="article-title">{html_escape.escape(ch.title)}</h1>\n'
            f'<div class="article-meta">{meta_line}</div>\n'
            f"{summary_block}"
            f"{safe_body}\n"
            f"{qr_block}".rstrip("\n")
        )
        drafts.append(
            _DraftChapter(
                filename=f"ch_{idx:03d}.xhtml",
                item_id=f"ch-{idx:03d}",
                anchor_id=anchor_id,
                title=ch.title,
                category=ch.category,
                language=ch.language,
                body_without_nav=body_without_nav,
                images=ch_images,
                reading_time_min=est_minutes,
            )
        )

    # 3. Partition drafts into volumes under max_mb_per_volume (reserve ~400 KB for cover/metadata)
    max_bytes = max(1, max_mb_per_volume) * 1024 * 1024 - 400 * 1024
    volumes_drafts: list[list[_DraftChapter]] = []
    cur_bucket: list[_DraftChapter] = []
    cur_bytes = 0
    for item in drafts:
        item_cost = item.estimated_bytes()
        if cur_bucket and cur_bytes + item_cost > max_bytes:
            volumes_drafts.append(cur_bucket)
            cur_bucket = [item]
            cur_bytes = item_cost
        else:
            cur_bucket.append(item)
            cur_bytes += item_cost
    if cur_bucket:
        volumes_drafts.append(cur_bucket)

    # 4. Finalize volume-local .chapter-nav links, write, and validate each volume
    total_vols = len(volumes_drafts)
    date_slug = issue_date.strftime("%Y%m%d_%H%M")
    raw_slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", edition_title).strip("_") or "edition"
    safe_slug = raw_slug[:40].rstrip("_") or "edition"
    build_uid = uuid.uuid4().hex[:6]
    built: list[BuiltEpubVolume] = []

    for v_idx, draft_bucket in enumerate(volumes_drafts, start=1):
        compiled_bucket: list[_CompiledChapter] = []
        for pos, draft in enumerate(draft_bucket):
            next_link = (
                f' · <a href="{draft_bucket[pos + 1].filename}">下一篇 →</a>'
                if pos + 1 < len(draft_bucket)
                else ""
            )
            final_inner = (
                f"{draft.body_without_nav}\n"
                f'<div class="chapter-nav"><a href="nav.xhtml">返回目录</a>{next_link}</div>'
            )
            compiled_bucket.append(
                _CompiledChapter(
                    filename=draft.filename,
                    item_id=draft.item_id,
                    anchor_id=draft.anchor_id,
                    title=draft.title,
                    category=draft.category,
                    xhtml_bytes=_wrap_xhtml_document(
                        title=draft.title,
                        body_inner=final_inner,
                        language=draft.language,
                    ),
                    images=draft.images,
                    reading_time_min=draft.reading_time_min,
                )
            )

        vol_suffix = f" ({v_idx}/{total_vols})" if total_vols > 1 else ""
        vol_title = f"{edition_title} {issue_date.strftime('%m-%d')}{vol_suffix}"
        cover_bytes = generate_editorial_cover(
            title=edition_title,
            subtitle=f"{subtitle}{vol_suffix}",
            issue_number=issue_number,
            issue_date=issue_date,
            headlines=[c.title for c in compiled_bucket if c.item_id != "ch-000"][:4],
            style=cover_style,
        )
        file_suffix = f"_vol{v_idx}" if total_vols > 1 else ""
        epub_path = output_dir / f"{safe_slug}_{date_slug}_{build_uid}{file_suffix}.epub"
        _write_strict_epub_zip(
            epub_path,
            book_id=f"urn:uuid:{uuid.uuid4()}",
            title=vol_title,
            author="InkPress",
            language="zh-CN",
            issue_date=issue_date,
            cover_bytes=cover_bytes,
            chapters=compiled_bucket,
        )
        validation = validate_epub(epub_path)
        if not validation.valid:
            raise RuntimeError(f"Generated EPUB failed validation: {'; '.join(validation.errors)}")

        built.append(
            BuiltEpubVolume(
                path=epub_path,
                title=vol_title,
                volume_index=v_idx,
                total_volumes=total_vols,
                article_count=sum(1 for c in compiled_bucket if c.item_id != "ch-000"),
                size_bytes=epub_path.stat().st_size,
                cover_bytes=cover_bytes,
            )
        )

    return built
