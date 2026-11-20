from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree


@dataclass
class EpubValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    chapter_count: int = 0
    image_count: int = 0
    size_bytes: int = 0


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
    if size_bytes > 24 * 1024 * 1024:
        warnings.append(
            f"EPUB size ({size_bytes / 1024 / 1024:.1f} MB) approaches 25 MB email limit"
        )

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
            if "META-INF/container.xml" not in names:
                errors.append("Missing META-INF/container.xml")

            has_ncx = any(n.endswith(".ncx") for n in names)
            has_nav = any(
                "nav" in Path(n).name.lower() and n.endswith((".xhtml", ".html")) for n in names
            )
            if not has_ncx:
                errors.append("Missing EPUB2 toc.ncx (required for maximum Kindle compatibility)")
            if not has_nav:
                errors.append("Missing EPUB3 nav.xhtml")

            # 2. Validate OPF metadata (dc:title, dc:language, dc:identifier)
            opf_files = [n for n in names if n.endswith(".opf")]
            if not opf_files:
                errors.append("Missing .opf package document")
            else:
                opf_bytes = zf.read(opf_files[0])
                try:
                    opf_root = etree.fromstring(opf_bytes)
                    dc_ns = {"dc": "http://purl.org/dc/elements/1.1/"}
                    for req in ("title", "language", "identifier"):
                        el = opf_root.find(f".//dc:{req}", namespaces=dc_ns)
                        if el is None or not (el.text or "").strip():
                            errors.append(f"Missing required OPF Dublin Core metadata: dc:{req}")
                except etree.XMLSyntaxError as exc:
                    errors.append(f"Invalid OPF XML: {exc}")

            # 3. Validate every XHTML document: UTF-8 XML declaration + valid XML + no empty <img>
            for name in names:
                lower = name.lower()
                if lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    image_count += 1
                    continue
                if not lower.endswith((".xhtml", ".html")):
                    continue
                chapter_count += 1
                raw = zf.read(name)
                head_snippet = raw[:240].decode("utf-8", errors="replace").lower()
                if "utf-8" not in head_snippet:
                    errors.append(f"{name}: Missing explicit UTF-8 XML/meta declaration")
                try:
                    doc = etree.fromstring(raw)
                except etree.XMLSyntaxError as exc:
                    errors.append(f"{name}: Malformed XHTML ({exc})")
                    continue

                for img in doc.xpath(".//*[local-name()='img']"):
                    src = (img.get("src") or "").strip()
                    if not src:
                        errors.append(f"{name}: Contains <img> tag with empty src")
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
