from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

MISSING_NCX_ERROR = "Missing EPUB2 toc.ncx (required for maximum Kindle compatibility)"
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass
class EpubValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    chapter_count: int = 0
    image_count: int = 0
    size_bytes: int = 0


def _make_safe_xml_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def _resolve_zip_path(base_member: str, relative_href: str) -> str:
    clean_href = relative_href.split("#", 1)[0].split("?", 1)[0]
    clean_href = unquote(clean_href).strip()
    if not clean_href:
        return base_member
    base_dir = posixpath.dirname(base_member)
    joined = posixpath.join(base_dir, clean_href) if base_dir else clean_href
    return posixpath.normpath(joined).lstrip("/")


def validate_epub(epub_path: Path) -> EpubValidationResult:
    """Strict Amazon Send-to-Kindle pre-flight validator to catch E999 triggers early."""
    errors: list[str] = []
    warnings: list[str] = []
    chapter_count = 0
    image_count = 0

    if not epub_path.exists():
        return EpubValidationResult(valid=False, errors=["EPUB file does not exist"])

    size_bytes = epub_path.stat().st_size
    if size_bytes == 0:
        return EpubValidationResult(valid=False, errors=["EPUB file is empty"])
    if size_bytes > 18 * 1024 * 1024:
        warnings.append(
            f"EPUB size ({size_bytes / 1024 / 1024:.1f} MB) exceeds 18 MB safe email threshold"
        )

    parser = _make_safe_xml_parser()

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            infos = zf.infolist()
            if not infos:
                return EpubValidationResult(valid=False, errors=["Empty ZIP archive"])

            # 1. mimetype MUST be first and stored uncompressed
            if infos[0].filename != "mimetype":
                errors.append(f"First ZIP entry must be 'mimetype', found '{infos[0].filename}'")
            elif infos[0].compress_type != zipfile.ZIP_STORED:
                errors.append("mimetype entry must be ZIP_STORED (uncompressed)")
            else:
                mt = zf.read("mimetype").decode("ascii", errors="replace").strip()
                if mt != "application/epub+zip":
                    errors.append(f"Invalid mimetype content: {mt!r}")

            names = set(zf.namelist())
            opf_path: str | None = None
            if "META-INF/container.xml" not in names:
                errors.append("Missing META-INF/container.xml")
            else:
                try:
                    container_root = etree.fromstring(
                        zf.read("META-INF/container.xml"), parser=parser
                    )
                    rootfile = container_root.find(".//{*}rootfile")
                    if rootfile is not None:
                        full_path = unquote((rootfile.get("full-path") or "").strip()).lstrip("/")
                        full_path = posixpath.normpath(full_path) if full_path else ""
                        if full_path and full_path in names:
                            opf_path = full_path
                        elif full_path:
                            errors.append(
                                f"META-INF/container.xml references missing OPF '{full_path}'"
                            )
                except etree.XMLSyntaxError as exc:
                    errors.append(f"Invalid META-INF/container.xml: {exc}")

            if opf_path is None:
                opf_candidates = [n for n in names if n.lower().endswith(".opf")]
                if opf_candidates:
                    opf_path = opf_candidates[0]

            has_ncx = any(n.lower().endswith(".ncx") for n in names)
            has_nav = any(
                "nav" in posixpath.basename(n).lower() and n.lower().endswith((".xhtml", ".html"))
                for n in names
            )
            non_chapter_pages: set[str] = set()

            # 2. Validate OPF metadata, manifest, and spine consistency
            if opf_path is None:
                errors.append("Missing .opf package document")
            else:
                opf_bytes = zf.read(opf_path)
                try:
                    opf_root = etree.fromstring(opf_bytes, parser=parser)
                    dc_ns = {"dc": "http://purl.org/dc/elements/1.1/"}
                    for req in ("title", "language", "identifier"):
                        el = opf_root.find(f".//dc:{req}", namespaces=dc_ns)
                        if el is None or not (el.text or "").strip():
                            errors.append(f"Missing required OPF Dublin Core metadata: dc:{req}")

                    manifest_by_id: dict[str, tuple[str, str, str]] = {}
                    for item in opf_root.xpath(".//*[local-name()='manifest']/*[local-name()='item']"):
                        iid = (item.get("id") or "").strip()
                        href = (item.get("href") or "").strip()
                        mtype = (item.get("media-type") or "").strip().lower()
                        props = (item.get("properties") or "").strip().lower()
                        if not iid or not href:
                            errors.append("OPF manifest contains <item> missing id or href")
                            continue
                        resolved = _resolve_zip_path(opf_path, href)
                        manifest_by_id[iid] = (resolved, mtype, props)
                        if resolved not in names:
                            errors.append(
                                f"OPF manifest item '{iid}' references missing ZIP member '{resolved}'"
                            )
                        prop_tokens = set(props.split())
                        if "nav" in prop_tokens:
                            has_nav = True
                            non_chapter_pages.add(resolved)
                        if mtype == "application/x-dtbncx+xml":
                            has_ncx = True
                        if iid.lower() in ("nav", "cover-page", "coverpage", "cover"):
                            non_chapter_pages.add(resolved)

                    for ref in opf_root.xpath(
                        ".//*[local-name()='guide']/*[local-name()='reference']"
                    ):
                        rtype = (ref.get("type") or "").strip().lower()
                        rhref = (ref.get("href") or "").strip()
                        if rtype in ("cover", "toc") and rhref:
                            non_chapter_pages.add(_resolve_zip_path(opf_path, rhref))

                    spine_el = opf_root.find(".//{*}spine")
                    if spine_el is None:
                        errors.append("Missing OPF <spine> element")
                    else:
                        toc_id = (spine_el.get("toc") or "").strip()
                        if toc_id and toc_id not in manifest_by_id:
                            errors.append(
                                f"OPF <spine toc='{toc_id}'> does not match any manifest item"
                            )
                        itemrefs = spine_el.xpath("./*[local-name()='itemref']")
                        if not itemrefs:
                            errors.append("OPF <spine> has no <itemref> entries")
                        for ref in itemrefs:
                            idref = (ref.get("idref") or "").strip()
                            if not idref or idref not in manifest_by_id:
                                errors.append(
                                    f"OPF spine itemref '{idref}' not found in manifest"
                                )
                except etree.XMLSyntaxError as exc:
                    errors.append(f"Invalid OPF XML: {exc}")

            if not has_ncx:
                errors.append(MISSING_NCX_ERROR)
            if not has_nav:
                errors.append("Missing EPUB3 nav.xhtml")

            # 3. Validate every XHTML document: UTF-8, valid XML, and valid internal img/link refs
            for name in sorted(names):
                lower = name.lower()
                if lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")):
                    image_count += 1
                    continue
                if not lower.endswith((".xhtml", ".html", ".htm")):
                    continue

                base_lower = posixpath.basename(name).lower()
                is_non_chapter = name in non_chapter_pages or base_lower in (
                    "cover.xhtml",
                    "cover.html",
                    "nav.xhtml",
                    "nav.html",
                )
                if not is_non_chapter:
                    chapter_count += 1

                raw = zf.read(name)
                head_snippet = raw[:512].decode("utf-8", errors="replace").lower()
                if "utf-8" not in head_snippet:
                    errors.append(f"{name}: Missing explicit UTF-8 XML/meta declaration")
                try:
                    doc = etree.fromstring(raw, parser=parser)
                except etree.XMLSyntaxError as exc:
                    errors.append(f"{name}: Malformed XHTML ({exc})")
                    continue

                for img in doc.xpath(".//*[local-name()='img']"):
                    src = (img.get("src") or "").strip()
                    if not src:
                        errors.append(f"{name}: Contains <img> tag with empty src")
                    elif not _SCHEME_RE.match(src):
                        target = _resolve_zip_path(name, src)
                        if target.startswith("..") or target not in names:
                            errors.append(
                                f"{name}: Broken internal <img src='{src}'> -> '{target}' missing"
                            )

                for anchor in doc.xpath(".//*[local-name()='a']"):
                    href = (anchor.get("href") or "").strip()
                    if not href or href.startswith("#") or _SCHEME_RE.match(href):
                        continue
                    target = _resolve_zip_path(name, href)
                    if target.startswith("..") or target not in names:
                        errors.append(
                            f"{name}: Broken internal <a href='{href}'> -> '{target}' missing"
                        )

                for body in doc.xpath(".//*[local-name()='body']"):
                    if body.get("id"):
                        warnings.append(
                            f"{name}: <body> has id attribute (avoid TOC links to body)"
                        )
    except zipfile.BadZipFile:
        return EpubValidationResult(valid=False, errors=["Corrupted ZIP / EPUB file"])

    return EpubValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        chapter_count=chapter_count,
        image_count=image_count,
        size_bytes=size_bytes,
    )
