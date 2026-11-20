from __future__ import annotations

import time
from collections import deque
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.api.deps import AUTH_COOKIE_NAME, get_current_user
from inkpress.api.schemas import (
    BootstrapRequest,
    ChangePasswordRequest,
    LoginRequest,
    PreferencesUpdate,
)
from inkpress.config import get_settings
from inkpress.db.base import get_db
from inkpress.db.models import User
from inkpress.security import create_access_token, hash_password, verify_password
from inkpress.settings_store import update_smtp_config

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 10
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300.0
_LOGIN_ATTEMPTS: dict[str, deque[float]] = {}


def reset_login_rate_limit() -> None:
    """Clear in-memory login rate-limit buckets (used by tests)."""
    _LOGIN_ATTEMPTS.clear()


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _prune_attempts(ip: str, now: float) -> deque[float]:
    bucket = _LOGIN_ATTEMPTS.get(ip)
    if bucket is None:
        bucket = deque()
        _LOGIN_ATTEMPTS[ip] = bucket
    cutoff = now - LOGIN_RATE_LIMIT_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(_LOGIN_ATTEMPTS) > 10_000:
        stale_ips = [k for k, v in _LOGIN_ATTEMPTS.items() if not v or v[-1] < cutoff]
        for k in stale_ips:
            _LOGIN_ATTEMPTS.pop(k, None)
    return bucket


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    xfp = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return xfp == "https"


def _user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "kindle_email": user.kindle_email,
        "preferences": user.preferences or {},
        "created_at": user.created_at.isoformat(),
    }


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_is_https_request(request),
        samesite="lax",
        max_age=settings.jwt_expire_hours * 3600,
        path="/",
    )


@router.get("/status")
async def auth_status(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    count = int((await session.execute(select(func.count(User.id)))).scalar_one())
    return {"initialized": count > 0}


@router.post("/bootstrap", status_code=status.HTTP_201_CREATED)
async def bootstrap_admin(
    payload: BootstrapRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    count = int((await session.execute(select(func.count(User.id)))).scalar_one())
    if count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Administrator account has already been initialized",
        )
    user = User(
        username=payload.username.strip(),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip() or payload.username.strip(),
        kindle_email=payload.kindle_email.strip(),
        preferences={
            "theme": "paper",
            "font_family": "serif",
            "font_size": 18,
            "line_height": 1.8,
        },
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    if payload.kindle_email.strip():
        await update_smtp_config(session, {"default_kindle_email": payload.kindle_email.strip()})

    token = create_access_token(user.id, user.username)
    _set_session_cookie(response, request, token)
    return {"user": _user_dict(user), "access_token": token}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    ip = _client_ip(request)
    now = time.monotonic()
    bucket = _prune_attempts(ip, now)
    if len(bucket) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试过于频繁，请 5 分钟后再试",
        )

    user = (
        await session.execute(select(User).where(User.username == payload.username.strip()))
    ).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        bucket.append(now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )

    _LOGIN_ATTEMPTS.pop(ip, None)
    token = create_access_token(user.id, user.username)
    _set_session_cookie(response, request, token)
    return {"user": _user_dict(user), "access_token": token}


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前密码不正确",
        )
    user.password_hash = hash_password(payload.new_password)
    await session.commit()
    token = create_access_token(user.id, user.username)
    _set_session_cookie(response, request, token)
    return {"status": "ok"}


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"user": _user_dict(user)}


@router.patch("/me")
async def update_me(
    payload: PreferencesUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or user.display_name
    if payload.kindle_email is not None:
        user.kindle_email = payload.kindle_email.strip()
        if user.kindle_email:
            await update_smtp_config(session, {"default_kindle_email": user.kindle_email})
    if payload.preferences is not None:
        merged = dict(user.preferences or {})
        merged.update(payload.preferences)
        user.preferences = merged
    await session.commit()
    await session.refresh(user)
    return {"user": _user_dict(user)}
