from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.config import get_settings
from inkpress.db.models import AppSetting
from inkpress.security import decrypt_secret, encrypt_secret

SMTP_KEY = "smtp"
AI_KEY = "ai"
NETWORK_KEY = "network"


async def get_setting_dict(session: AsyncSession, key: str) -> dict[str, Any]:
    row = await session.get(AppSetting, key)
    return dict(row.value_json) if row and isinstance(row.value_json, dict) else {}


async def save_setting_dict(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value_json=value)
        session.add(row)
    else:
        row.value_json = value
    await session.commit()


async def get_smtp_config(session: AsyncSession) -> dict[str, Any]:
    env = get_settings()
    stored = await get_setting_dict(session, SMTP_KEY)
    password = (
        decrypt_secret(stored.get("password_enc", ""))
        if stored.get("password_enc")
        else env.smtp_password
    )
    return {
        "host": stored.get("host") or env.smtp_host,
        "port": int(stored.get("port") or env.smtp_port),
        "use_ssl": stored.get("use_ssl", env.smtp_use_ssl),
        "username": stored.get("username") or env.smtp_username,
        "password": password,
        "from_email": stored.get("from_email")
        or env.smtp_from_email
        or stored.get("username")
        or env.smtp_username,
        "default_kindle_email": stored.get("default_kindle_email") or env.default_kindle_email,
    }


async def update_smtp_config(session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    stored = await get_setting_dict(session, SMTP_KEY)
    for field in ("host", "port", "use_ssl", "username", "from_email", "default_kindle_email"):
        if field in payload and payload[field] is not None:
            stored[field] = payload[field]
    if payload.get("password"):
        stored["password_enc"] = encrypt_secret(str(payload["password"]))
    await save_setting_dict(session, SMTP_KEY, stored)
    return await get_smtp_config(session)


async def get_ai_config(session: AsyncSession) -> dict[str, Any]:
    env = get_settings()
    stored = await get_setting_dict(session, AI_KEY)
    api_key = (
        decrypt_secret(stored.get("api_key_enc", ""))
        if stored.get("api_key_enc")
        else env.ai_api_key
    )
    enabled = stored["enabled"] if "enabled" in stored else (env.ai_enabled or bool(api_key))
    return {
        "enabled": bool(enabled),
        "base_url": stored.get("base_url") or env.ai_base_url,
        "api_key": api_key,
        "model": stored.get("model") or env.ai_model,
        "use_proxy": bool(stored.get("use_proxy", False)),
        "max_concurrency": int(stored.get("max_concurrency") or 3),
        "timeout_seconds": int(stored.get("timeout_seconds") or 60),
    }


async def update_ai_config(session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    stored = await get_setting_dict(session, AI_KEY)
    for field in (
        "enabled",
        "base_url",
        "model",
        "use_proxy",
        "max_concurrency",
        "timeout_seconds",
    ):
        if field in payload and payload[field] is not None:
            stored[field] = payload[field]
    if payload.get("api_key"):
        stored["api_key_enc"] = encrypt_secret(str(payload["api_key"]))
    await save_setting_dict(session, AI_KEY, stored)
    return await get_ai_config(session)


async def get_network_config(session: AsyncSession) -> dict[str, Any]:
    env = get_settings()
    stored = await get_setting_dict(session, NETWORK_KEY)
    return {
        "outbound_proxy": stored.get("outbound_proxy", env.outbound_proxy),
        "newsnow_base_url": stored.get("newsnow_base_url") or env.newsnow_base_url,
        "rsshub_base_url": stored.get("rsshub_base_url") or env.rsshub_base_url,
        "byparr_base_url": stored.get("byparr_base_url") or env.byparr_base_url,
    }


async def update_network_config(session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    stored = await get_setting_dict(session, NETWORK_KEY)
    for field in ("outbound_proxy", "newsnow_base_url", "rsshub_base_url", "byparr_base_url"):
        if field in payload and payload[field] is not None:
            stored[field] = payload[field]
    await save_setting_dict(session, NETWORK_KEY, stored)
    return await get_network_config(session)


async def get_all_settings_keys(session: AsyncSession) -> list[str]:
    rows = (await session.execute(select(AppSetting.key))).scalars().all()
    return list(rows)
