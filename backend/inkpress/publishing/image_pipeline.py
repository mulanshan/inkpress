from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import BytesIO

import httpx
from PIL import Image, ImageOps

from inkpress.ingestion.fetcher import infer_default_image_referer, smart_fetch_bytes
from inkpress.logging import get_logger

logger = get_logger(__name__)

TALL_SLICE_RATIO = 2.0
TALL_SLICE_MIN_HEIGHT = 1600
TALL_SLICE_ASPECT = 1.35
TALL_SLICE_OVERLAP_PX = 48
MAX_SLICES_PER_IMAGE = 12


@dataclass
class ProcessedImage:
    original_url: str
    filename: str
    media_type: str
    data: bytes
    extra_slices: list[ProcessedImage] = field(default_factory=list)


def _is_forbidden_error(exc: Exception) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, httpx.HTTPStatusError) and cur.response.status_code in (401, 403, 418):
            return True
        cur = cur.__cause__
    return False


def _encode_jpeg(img: Image.Image, *, quality: int) -> bytes:
    out = BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def optimize_image_slices(
    raw: bytes,
    *,
    max_width: int = 1000,
    grayscale: bool = False,
    quality: int = 78,
    slice_tall: bool = True,
) -> list[bytes]:
    """Convert any Pillow-supported image into Kindle-safe JPEG(s), automatically slicing tall infographics."""
    try:
        with Image.open(BytesIO(raw)) as img:
            if img.width < 52 or img.height < 52:
                # Filter out 1x1 tracking pixels, tiny spacer gifs, and 16-48px UI badge icons
                return []

            # Handle transparency cleanly onto white background
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                work: Image.Image = bg
            else:
                work = img.convert("RGB")

            if work.width > max_width:
                ratio = max_width / float(work.width)
                new_h = max(1, int(work.height * ratio))
                work = work.resize((max_width, new_h), Image.Resampling.LANCZOS)

            if grayscale:
                work = ImageOps.autocontrast(work.convert("L"), cutoff=0.5)

            if (
                slice_tall
                and work.height > work.width * TALL_SLICE_RATIO
                and work.height > TALL_SLICE_MIN_HEIGHT
            ):
                slice_h = max(600, int(work.width * TALL_SLICE_ASPECT))
                step = max(200, slice_h - TALL_SLICE_OVERLAP_PX)
                if work.height > step * MAX_SLICES_PER_IMAGE:
                    slice_h = max(
                        slice_h,
                        (work.height + TALL_SLICE_OVERLAP_PX * (MAX_SLICES_PER_IMAGE - 1))
                        // MAX_SLICES_PER_IMAGE
                        + 1,
                    )
                    step = max(200, slice_h - TALL_SLICE_OVERLAP_PX)

                slices: list[bytes] = []
                top = 0
                while top < work.height:
                    if work.height - top <= int(slice_h * 1.22):
                        crop_box = (0, top, work.width, work.height)
                        slices.append(_encode_jpeg(work.crop(crop_box), quality=quality))
                        break
                    crop_box = (0, top, work.width, min(work.height, top + slice_h))
                    slices.append(_encode_jpeg(work.crop(crop_box), quality=quality))
                    top += step
                return slices

            return [_encode_jpeg(work, quality=quality)]
    except Exception:
        return []


def optimize_image_bytes(
    raw: bytes,
    *,
    max_width: int = 1000,
    grayscale: bool = False,
    quality: int = 78,
) -> bytes | None:
    """Convert any Pillow-supported image (WebP/PNG/JPEG/GIF) into a single Kindle-safe JPEG."""
    slices = optimize_image_slices(
        raw,
        max_width=max_width,
        grayscale=grayscale,
        quality=quality,
        slice_tall=False,
    )
    return slices[0] if slices else None


async def download_and_optimize_images(
    urls: Sequence[str | tuple[str, str]],
    *,
    referer: str = "",
    url_referers: dict[str, str] | None = None,
    outbound_proxy: str = "",
    grayscale: bool = False,
    max_width: int = 1000,
    max_images: int = 150,
    slice_tall: bool = True,
) -> dict[str, ProcessedImage]:
    """Download unique image URLs concurrently (with per-URL Referer & 401/403/418 retry) into JPEGs."""
    dedup_items: dict[str, str] = {}
    for entry in urls:
        if isinstance(entry, tuple):
            img_url, item_ref = entry
        else:
            img_url, item_ref = entry, ""
        img_url = img_url.strip()
        if not img_url.startswith(("http://", "https://")):
            continue
        effective_ref = (
            item_ref.strip()
            or (url_referers.get(img_url, "").strip() if url_referers else "")
            or referer.strip()
            or infer_default_image_referer(img_url)
        )
        if img_url not in dedup_items or (not dedup_items[img_url] and effective_ref):
            dedup_items[img_url] = effective_ref

    selected_items = list(dedup_items.items())[:max_images]
    sem = asyncio.Semaphore(6)
    results: dict[str, ProcessedImage] = {}

    async def _fetch_one(url: str, item_referer: str) -> None:
        async with sem:
            try:
                try:
                    raw, _ = await smart_fetch_bytes(
                        url,
                        outbound_proxy=outbound_proxy,
                        referer=item_referer,
                        timeout=12.0,
                    )
                except Exception as first_exc:
                    if item_referer and _is_forbidden_error(first_exc):
                        default_ref = infer_default_image_referer(url)
                        fallback_ref = (
                            default_ref if (default_ref and default_ref != item_referer) else ""
                        )
                        raw, _ = await smart_fetch_bytes(
                            url,
                            outbound_proxy=outbound_proxy,
                            referer=fallback_ref,
                            timeout=12.0,
                        )
                    else:
                        raise

                slices = await asyncio.to_thread(
                    optimize_image_slices,
                    raw,
                    max_width=max_width,
                    grayscale=grayscale,
                    slice_tall=slice_tall,
                )
                if not slices:
                    return
                digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                extra = [
                    ProcessedImage(
                        original_url=url,
                        filename=f"images/img_{digest}_p{part_idx}.jpg",
                        media_type="image/jpeg",
                        data=part_bytes,
                    )
                    for part_idx, part_bytes in enumerate(slices[1:], start=2)
                ]
                results[url] = ProcessedImage(
                    original_url=url,
                    filename=f"images/img_{digest}.jpg",
                    media_type="image/jpeg",
                    data=slices[0],
                    extra_slices=extra,
                )
            except Exception as exc:
                logger.debug("image_download_skipped", url=url, err=str(exc))

    await asyncio.gather(*[_fetch_one(u, ref) for u, ref in selected_items])
    return results
