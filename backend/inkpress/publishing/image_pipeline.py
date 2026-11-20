from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

import httpx
from PIL import Image

from inkpress.ingestion.fetcher import smart_fetch_bytes
from inkpress.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ProcessedImage:
    original_url: str
    filename: str
    media_type: str
    data: bytes


def _is_forbidden_error(exc: Exception) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, httpx.HTTPStatusError) and cur.response.status_code in (401, 403):
            return True
        cur = cur.__cause__
    return False


def optimize_image_bytes(
    raw: bytes,
    *,
    max_width: int = 1000,
    grayscale: bool = False,
    quality: int = 78,
) -> bytes | None:
    """Convert any Pillow-supported image (WebP/PNG/JPEG/GIF) into Kindle-safe JPEG."""
    try:
        with Image.open(BytesIO(raw)) as img:
            if img.width < 12 or img.height < 12:
                # Filter out 1x1 tracking pixels and tiny spacer gifs
                return None

            # Handle transparency cleanly onto white background
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                work = bg
            else:
                work = img.convert("RGB")

            if work.width > max_width:
                ratio = max_width / float(work.width)
                new_h = max(1, int(work.height * ratio))
                work = work.resize((max_width, new_h), Image.Resampling.LANCZOS)

            if grayscale:
                work = work.convert("L")

            out = BytesIO()
            work.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue()
    except Exception:
        return None


async def download_and_optimize_images(
    urls: Sequence[str | tuple[str, str]],
    *,
    referer: str = "",
    url_referers: dict[str, str] | None = None,
    outbound_proxy: str = "",
    grayscale: bool = False,
    max_width: int = 1000,
    max_images: int = 150,
) -> dict[str, ProcessedImage]:
    """Download unique image URLs concurrently (with per-URL Referer & 401/403 retry) into JPEGs."""
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
                        raw, _ = await smart_fetch_bytes(
                            url,
                            outbound_proxy=outbound_proxy,
                            referer="",
                            timeout=12.0,
                        )
                    else:
                        raise

                optimized = await asyncio.to_thread(
                    optimize_image_bytes,
                    raw,
                    max_width=max_width,
                    grayscale=grayscale,
                )
                if optimized is None:
                    return
                digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                results[url] = ProcessedImage(
                    original_url=url,
                    filename=f"images/img_{digest}.jpg",
                    media_type="image/jpeg",
                    data=optimized,
                )
            except Exception as exc:
                logger.debug("image_download_skipped", url=url, err=str(exc))

    await asyncio.gather(*[_fetch_one(u, ref) for u, ref in selected_items])
    return results
