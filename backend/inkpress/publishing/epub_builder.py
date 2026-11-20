from __future__ import annotations

import html as html_escape
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path

import qrcode
from bs4 import BeautifulSoup
from lxml import etree
from lxml import html as lxml_html

from inkpress.publishing.cover_generator import generate_editorial_cover
from inkpress.publishing.epub_validator import validate_epub
from inkpress.publishing.image_pipeline import ProcessedImage, download_and_optimize_images

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
  font-style: italic;
  text-indent: 2em;
  margin-top: -0.45em;
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
.meta-tag {
  font-size: 0.8em;
  color: #666666;
}
"""


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
    etree.fromstring(raw)
    return raw


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
    iso_date = issue_date.strftime("%Y-%m-%dT%H:%M:%SZ")

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

    # EPUB3 nav.xhtml
    nav_items = "\n".join(
        f'      <li><a href="{ch.filename}#{ch.anchor_id}">{html_escape.escape(ch.title)}</a></li>'
        for ch in chapters
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
        "</body>\n"
        "</html>\n"
    ).encode()
    etree.fromstring(nav_xhtml)

    # EPUB2 toc.ncx
    ncx_points = "\n".join(
        f'    <navPoint id="navPoint-{idx}" playOrder="{idx}">\n'
        f"      <navLabel><text>{html_escape.escape(ch.title)}</text></navLabel>\n"
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
    etree.fromstring(toc_ncx)

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
    etree.fromstring(content_opf)

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
) -> list[BuiltEpubVolume]:
    """Compile articles into one or more Send-to-Kindle-validated EPUB3 volumes."""
    if not chapters_input and not briefing_html:
        raise ValueError("Cannot build EPUB with zero articles and no briefing")

    # 1. Gather all image URLs across chapters
    all_img_urls: list[str] = []
    if download_images:
        for ch in chapters_input:
            source_html = ch.bilingual_html or ch.body_html
            soup = BeautifulSoup(source_html, "lxml")
            for img in soup.find_all("img"):
                src = str(img.get("src") or "").strip()
                if src.startswith(("http://", "https://")):
                    all_img_urls.append(src)

    downloaded = (
        await download_and_optimize_images(
            all_img_urls,
            outbound_proxy=outbound_proxy,
            grayscale=grayscale_images,
        )
        if all_img_urls
        else {}
    )

    # 2. Compile each chapter into strict XHTML5
    compiled: list[_CompiledChapter] = []

    if briefing_html.strip():
        safe_brief = _xml_safe_fragment(briefing_html)
        brief_body = (
            '<h1 id="ch-briefing" class="article-title">今日头版综述</h1>\n'
            f'<div class="article-meta">{html_escape.escape(edition_title)} · 第 {issue_number} 期 · '
            f"{issue_date.strftime('%Y-%m-%d')}</div>\n"
            f"{safe_brief}\n"
            '<div class="chapter-nav"><a href="nav.xhtml">返回目录</a></div>'
        )
        compiled.append(
            _CompiledChapter(
                filename="ch_000_briefing.xhtml",
                item_id="ch-000",
                anchor_id="ch-briefing",
                title="今日头版综述",
                category="导读",
                xhtml_bytes=_wrap_xhtml_document(title="今日头版综述", body_inner=brief_body),
            )
        )

    for idx, ch in enumerate(chapters_input, start=1):
        source_html = ch.bilingual_html or ch.body_html
        soup = BeautifulSoup(source_html, "lxml")
        ch_images: list[ProcessedImage] = []

        for img in soup.find_all("img"):
            src = str(img.get("src") or "").strip()
            if src in downloaded:
                pimg = downloaded[src]
                img["src"] = pimg.filename
                ch_images.append(pimg)
            else:
                img.decompose()

        body_root = soup.body if soup.body else soup
        safe_body = _xml_safe_fragment(body_root.decode_contents())

        anchor_id = f"ch-{idx:03d}-h"
        summary_block = ""
        if ch.ai_summary:
            summary_block = (
                '<div class="ai-summary-box">'
                '<span class="ai-summary-label">AI 导读</span>'
                f"<p>{html_escape.escape(ch.ai_summary)}</p>"
                "</div>\n"
            )

        meta_parts = [
            p
            for p in (
                ch.category,
                ch.source_name,
                ch.author,
                ch.published_at.strftime("%Y-%m-%d %H:%M"),
            )
            if p
        ]
        meta_line = html_escape.escape(" · ".join(meta_parts))

        qr_block = ""
        if include_qr_links and ch.canonical_url.startswith(("http://", "https://")):
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

        next_link = (
            f' · <a href="ch_{idx + 1:03d}.xhtml">下一篇 →</a>' if idx < len(chapters_input) else ""
        )
        inner_html = (
            f'<h1 id="{anchor_id}" class="article-title">{html_escape.escape(ch.title)}</h1>\n'
            f'<div class="article-meta">{meta_line}</div>\n'
            f"{summary_block}"
            f"{safe_body}\n"
            f"{qr_block}"
            f'<div class="chapter-nav"><a href="nav.xhtml">返回目录</a>{next_link}</div>'
        )
        compiled.append(
            _CompiledChapter(
                filename=f"ch_{idx:03d}.xhtml",
                item_id=f"ch-{idx:03d}",
                anchor_id=anchor_id,
                title=ch.title,
                category=ch.category,
                xhtml_bytes=_wrap_xhtml_document(
                    title=ch.title, body_inner=inner_html, language=ch.language
                ),
                images=ch_images,
            )
        )

    # 3. Partition into volumes under max_mb_per_volume (reserve ~400 KB for cover/metadata)
    max_bytes = max(2, max_mb_per_volume) * 1024 * 1024 - 400 * 1024
    volumes_chapters: list[list[_CompiledChapter]] = []
    cur_bucket: list[_CompiledChapter] = []
    cur_bytes = 0
    for item in compiled:
        item_cost = len(item.xhtml_bytes) + sum(len(img.data) for img in item.images)
        if cur_bucket and cur_bytes + item_cost > max_bytes:
            volumes_chapters.append(cur_bucket)
            cur_bucket = [item]
            cur_bytes = item_cost
        else:
            cur_bucket.append(item)
            cur_bytes += item_cost
    if cur_bucket:
        volumes_chapters.append(cur_bucket)

    # 4. Write & validate each volume
    total_vols = len(volumes_chapters)
    date_slug = issue_date.strftime("%Y%m%d_%H%M")
    safe_slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", edition_title).strip("_") or "edition"
    built: list[BuiltEpubVolume] = []

    for v_idx, bucket in enumerate(volumes_chapters, start=1):
        vol_suffix = f" ({v_idx}/{total_vols})" if total_vols > 1 else ""
        vol_title = f"{edition_title} {issue_date.strftime('%m-%d')}{vol_suffix}"
        cover_bytes = generate_editorial_cover(
            title=edition_title,
            subtitle=f"{subtitle}{vol_suffix}",
            issue_number=issue_number,
            issue_date=issue_date,
            headlines=[c.title for c in bucket if c.item_id != "ch-000"][:4],
            style=cover_style,
        )
        file_suffix = f"_vol{v_idx}" if total_vols > 1 else ""
        epub_path = output_dir / f"{safe_slug}_{date_slug}{file_suffix}.epub"
        _write_strict_epub_zip(
            epub_path,
            book_id=f"urn:uuid:{uuid.uuid4()}",
            title=vol_title,
            author="InkPress",
            language="zh-CN",
            issue_date=issue_date,
            cover_bytes=cover_bytes,
            chapters=bucket,
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
                article_count=sum(1 for c in bucket if c.item_id != "ch-000"),
                size_bytes=epub_path.stat().st_size,
                cover_bytes=cover_bytes,
            )
        )

    return built
