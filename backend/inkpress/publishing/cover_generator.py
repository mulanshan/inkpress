from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES_SERIF = (
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Bold.ttc",
    "/System/Library/Fonts/Songti.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
)

FONT_CANDIDATES_SANS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _load_font(
    candidates: tuple[str, ...], size: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _measure_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> float:
    if not text:
        return 0.0
    bbox = draw.textbbox((0, 0), text, font=font)
    return float(bbox[2] - bbox[0])


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> list[str]:
    cleaned = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
    if not cleaned or max_lines <= 0:
        return []

    lines: list[str] = []
    current = ""
    truncated = False

    for idx, ch in enumerate(cleaned):
        candidate = current + ch
        if _measure_width(draw, candidate, font) <= max_width:
            current = candidate
            continue

        if len(lines) >= max_lines - 1:
            # We are already filling the last allowed line and `ch` overflows
            truncated = True
            break

        if current:
            lines.append(current.rstrip())
            current = "" if ch.isspace() else ch
        else:
            # Single glyph exceeds max_width; force progress to avoid infinite loss
            lines.append(ch)
            current = ""
            if len(lines) >= max_lines and idx < len(cleaned) - 1:
                truncated = True
                break

    if current and len(lines) < max_lines:
        lines.append(current.rstrip())

    if truncated:
        if len(lines) == max_lines:
            last = lines[-1].rstrip()
            while last and _measure_width(draw, last + "…", font) > max_width:
                last = last[:-1].rstrip()
            lines[-1] = (last + "…") if last else "…"
        elif len(lines) < max_lines:
            lines.append("…")

    return lines


def generate_editorial_cover(
    *,
    title: str,
    subtitle: str = "每日精选刊物",
    issue_number: int = 1,
    issue_date: datetime | None = None,
    headlines: list[str] | None = None,
    style: str = "editorial",
    width: int = 800,
    height: int = 1200,
) -> bytes:
    """Render a crisp, high-contrast Kinfolk/Economist-inspired e-ink JPEG cover."""
    dt = issue_date or datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
    date_label = f"{dt.strftime('%Y.%m.%d')}  ·  {weekday_cn}  ·  NO.{issue_number:03d}"

    if style == "dark":
        bg = (22, 22, 20)
        fg = (244, 240, 232)
        muted = (166, 160, 148)
        accent = (204, 96, 68)
        rule = (62, 60, 56)
    elif style == "minimal":
        bg = (255, 255, 255)
        fg = (16, 16, 16)
        muted = (96, 96, 96)
        accent = (16, 16, 16)
        rule = (210, 210, 210)
    else:  # editorial paper (default)
        bg = (250, 247, 240)
        fg = (28, 27, 24)
        muted = (110, 105, 96)
        accent = (180, 75, 50)
        rule = (216, 210, 198)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    pad = 72
    inner_w = width - pad * 2

    # Outer frame & top masthead accent bar
    draw.rectangle([pad, 56, width - pad, 66], fill=accent)

    meta_font = _load_font(FONT_CANDIDATES_SANS, 24)
    title_font = _load_font(FONT_CANDIDATES_SERIF, 74)
    sub_font = _load_font(FONT_CANDIDATES_SANS, 28)
    section_font = _load_font(FONT_CANDIDATES_SANS, 22)
    head_font = _load_font(FONT_CANDIDATES_SERIF, 30)
    footer_font = _load_font(FONT_CANDIDATES_SANS, 22)

    draw.text((pad, 92), "INKPRESS EDITION", fill=accent, font=meta_font)
    draw.text((pad, 132), date_label, fill=muted, font=meta_font)
    draw.line([(pad, 182), (width - pad, 182)], fill=rule, width=2)

    # Masthead title (wrapped up to 2 lines)
    y = 228
    for line in _wrap_text(draw, title, title_font, inner_w, max_lines=2):
        draw.text((pad, y), line, fill=fg, font=title_font)
        y += 96

    if subtitle:
        y += 12
        for line in _wrap_text(draw, subtitle, sub_font, inner_w, max_lines=2):
            draw.text((pad, y), line, fill=muted, font=sub_font)
            y += 42

    # Decorative editorial divider
    y = max(y + 44, 520)
    draw.line([(pad, y), (width - pad, y)], fill=fg, width=3)
    draw.line([(pad, y + 8), (width - pad, y + 8)], fill=rule, width=1)
    y += 42

    # Featured headlines
    lead_list = [h.strip() for h in (headlines or []) if h.strip()][:4]
    if lead_list:
        draw.text((pad, y), "本期焦点  /  IN THIS ISSUE", fill=accent, font=section_font)
        y += 48
        for idx, headline in enumerate(lead_list, start=1):
            wrapped = _wrap_text(draw, f"{idx:02d}  {headline}", head_font, inner_w, max_lines=2)
            for line in wrapped:
                draw.text((pad, y), line, fill=fg, font=head_font)
                y += 44
            y += 22
            if y > height - 180:
                break

    # Footer
    draw.line([(pad, height - 110), (width - pad, height - 110)], fill=rule, width=2)
    draw.text(
        (pad, height - 84),
        "Compiled for Kindle · Curated by InkPress",
        fill=muted,
        font=footer_font,
    )

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88, optimize=True)
    return buf.getvalue()
