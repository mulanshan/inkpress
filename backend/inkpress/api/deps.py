from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.db.base import get_db
from inkpress.db.models import User
from inkpress.security import decode_access_token

AUTH_COOKIE_NAME = "inkpress_session"


async def get_current_user(
    session: AsyncSession = Depends(get_db),
    inkpress_session: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> User:
    token = inkpress_session
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session"
        )

    user = await session.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user
