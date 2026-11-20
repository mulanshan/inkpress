from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.ai.client import AIClient
from inkpress.api.deps import get_current_user
from inkpress.api.schemas import (
    AiSettingsUpdate,
    McpTokenCreate,
    McpTokenUpdate,
    NetworkSettingsUpdate,
    SmtpSettingsUpdate,
    SmtpTestRequest,
)
from inkpress.db.base import get_db
from inkpress.db.models import McpToken, User
from inkpress.delivery.smtp import parse_recipients, send_ebook_via_smtp
from inkpress.security import generate_mcp_token
from inkpress.settings_store import (
    get_ai_config,
    get_network_config,
    get_smtp_config,
    update_ai_config,
    update_network_config,
    update_smtp_config,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


def _masked_smtp(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": cfg.get("host", ""),
        "port": cfg.get("port", 465),
        "use_ssl": cfg.get("use_ssl", True),
        "username": cfg.get("username", ""),
        "password_set": bool(cfg.get("password")),
        "from_email": cfg.get("from_email", ""),
        "default_kindle_email": cfg.get("default_kindle_email", ""),
    }


def _masked_ai(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": cfg.get("enabled", False),
        "base_url": cfg.get("base_url", ""),
        "api_key_set": bool(cfg.get("api_key")),
        "model": cfg.get("model", ""),
        "use_proxy": cfg.get("use_proxy", False),
        "max_concurrency": cfg.get("max_concurrency", 3),
        "timeout_seconds": cfg.get("timeout_seconds", 60),
    }


@router.get("")
async def get_all_settings(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return {
        "smtp": _masked_smtp(await get_smtp_config(session)),
        "ai": _masked_ai(await get_ai_config(session)),
        "network": await get_network_config(session),
    }


@router.put("/smtp")
async def put_smtp_settings(
    payload: SmtpSettingsUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cfg = await update_smtp_config(session, payload.model_dump(exclude_none=True))
    return {"smtp": _masked_smtp(cfg)}


@router.post("/smtp/test")
async def test_smtp_settings(
    payload: SmtpTestRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cfg = await get_smtp_config(session)
    target = payload.target_email.strip() or str(cfg.get("from_email") or cfg.get("username") or "")
    recipients = parse_recipients(target)
    if not recipients:
        raise HTTPException(status_code=400, detail="请先配置收件邮箱或发件人邮箱")
    try:
        await send_ebook_via_smtp(
            smtp_config=cfg,
            recipients=recipients,
            subject="InkPress SMTP Connectivity Test",
            body_text="Your InkPress SMTP configuration is working properly.",
            attachments=[],
            retries=1,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SMTP 测试失败: {exc}") from exc
    return {"ok": True, "sent_to": recipients}


@router.put("/ai")
async def put_ai_settings(
    payload: AiSettingsUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cfg = await update_ai_config(session, payload.model_dump(exclude_none=True))
    return {"ai": _masked_ai(cfg)}


@router.post("/ai/test")
async def test_ai_settings(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cfg = await get_ai_config(session)
    net = await get_network_config(session)
    if not cfg.get("api_key"):
        raise HTTPException(status_code=400, detail="请先填写 AI API Key")
    client = AIClient(
        base_url=str(cfg["base_url"]),
        api_key=str(cfg["api_key"]),
        model=str(cfg["model"]),
        outbound_proxy=net["outbound_proxy"],
        use_proxy=bool(cfg.get("use_proxy", False)),
        timeout_seconds=int(cfg.get("timeout_seconds", 30)),
    )
    try:
        return await client.test_connection()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 连接测试失败: {exc}") from exc
    finally:
        await client.close()


@router.put("/network")
async def put_network_settings(
    payload: NetworkSettingsUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cfg = await update_network_config(session, payload.model_dump(exclude_none=True))
    return {"network": cfg}


@router.get("/mcp-tokens")
async def list_mcp_tokens(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = (await session.execute(select(McpToken).order_by(desc(McpToken.id)))).scalars().all()
    return {
        "items": [
            {
                "id": t.id,
                "name": t.name,
                "token_prefix": t.token_prefix,
                "readonly": t.readonly,
                "enabled": t.enabled,
                "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                "created_at": t.created_at.isoformat(),
            }
            for t in rows
        ]
    }


@router.post("/mcp-tokens", status_code=status.HTTP_201_CREATED)
async def create_mcp_token(
    payload: McpTokenCreate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    raw, prefix, digest = generate_mcp_token()
    tok = McpToken(
        name=payload.name.strip(),
        token_prefix=prefix,
        token_hash=digest,
        readonly=payload.readonly,
        enabled=True,
    )
    session.add(tok)
    await session.commit()
    await session.refresh(tok)
    return {
        "id": tok.id,
        "name": tok.name,
        "token": raw,  # Shown once on creation
        "token_prefix": tok.token_prefix,
        "readonly": tok.readonly,
    }


@router.patch("/mcp-tokens/{token_id}")
async def update_mcp_token(
    token_id: int,
    payload: McpTokenUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    tok = await session.get(McpToken, token_id)
    if tok is None:
        raise HTTPException(status_code=404, detail="MCP token not found")
    if payload.readonly is not None:
        tok.readonly = payload.readonly
    if payload.enabled is not None:
        tok.enabled = payload.enabled
    await session.commit()
    return {"id": tok.id, "readonly": tok.readonly, "enabled": tok.enabled}


@router.delete("/mcp-tokens/{token_id}")
async def delete_mcp_token(
    token_id: int,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    tok = await session.get(McpToken, token_id)
    if tok is None:
        raise HTTPException(status_code=404, detail="MCP token not found")
    await session.delete(tok)
    await session.commit()
    return {"status": "deleted"}
