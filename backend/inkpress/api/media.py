from __future__ import annotations

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.api.deps import get_current_user
from inkpress.db.base import get_db
from inkpress.db.models import User
from inkpress.ingestion.fetcher import (
    assert_safe_public_url,
    infer_default_image_referer,
    smart_fetch_bytes,
)
from inkpress.settings_store import get_network_config

router = APIRouter(prefix="/api/v1/media", tags=["media"])

MAX_MEDIA_PROXY_BYTES = 10 * 1024 * 1024  # 10 MB

ALLOWED_PROXY_IMAGE_MIMES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)


def _sanitize_referer(referer: str) -> str:
    cleaned = referer.strip()
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return cleaned
    return ""


@router.get("/proxy")
async def proxy_media_image(
    url: str = Query(..., min_length=8, max_length=2048),
    referer: str = Query(default="", max_length=2048),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> Response:
    """Authenticated, SSRF-hardened image proxy for anti-hotlink CDNs and overseas hosts."""
    target_url = url.strip()
    try:
        await assert_safe_public_url(target_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unsafe media URL: {exc}") from exc

    safe_referer = _sanitize_referer(referer) or infer_default_image_referer(target_url)
    net_cfg = await get_network_config(session)
    outbound_proxy = str(net_cfg.get("outbound_proxy") or "")

    data: bytes
    content_type: str
    try:
        data, content_type = await smart_fetch_bytes(
            target_url,
            outbound_proxy=outbound_proxy,
            referer=safe_referer,
            timeout=15.0,
            max_bytes=MAX_MEDIA_PROXY_BYTES,
        )
    except httpx.HTTPStatusError as exc:
        if safe_referer and exc.response.status_code in (401, 403, 418):
            default_ref = infer_default_image_referer(target_url)
            fallback_ref = default_ref if (default_ref and default_ref != safe_referer) else ""
            try:
                data, content_type = await smart_fetch_bytes(
                    target_url,
                    outbound_proxy=outbound_proxy,
                    referer=fallback_ref,
                    timeout=15.0,
                    max_bytes=MAX_MEDIA_PROXY_BYTES,
                )
            except ValueError as retry_val_exc:
                code = 413 if "exceeded" in str(retry_val_exc).lower() else 400
                raise HTTPException(status_code=code, detail=str(retry_val_exc)) from retry_val_exc
            except Exception as retry_exc:
                raise HTTPException(
                    status_code=502, detail=f"Upstream media fetch failed: {retry_exc}"
                ) from retry_exc
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Upstream media returned HTTP {exc.response.status_code}",
            ) from exc
    except ValueError as exc:
        code = 413 if "exceeded" in str(exc).lower() else 400
        raise HTTPException(status_code=code, detail=f"Unsafe or oversized media URL: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream media fetch failed: {exc}") from exc

    if len(data) > MAX_MEDIA_PROXY_BYTES:
        raise HTTPException(status_code=413, detail="Proxied image exceeds 10 MB limit")

    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if media_type not in ALLOWED_PROXY_IMAGE_MIMES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media content type: {media_type or 'unknown'}",
        )

    return Response(
        content=data,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Cache-Control": "private, max-age=86400",
        },
    )
