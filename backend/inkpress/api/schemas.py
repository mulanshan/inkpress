from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="Reader", max_length=128)
    kindle_email: str = Field(default="", max_length=256)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PreferencesUpdate(BaseModel):
    display_name: str | None = None
    kindle_email: str | None = None
    preferences: dict[str, Any] | None = None


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: str = Field(pattern="^(newsnow|rss|rsshub|webpage|webpage_list)$")
    identifier: str = Field(min_length=1, max_length=1024)
    category: str = Field(default="综合", max_length=64)
    fetch_strategy: str = Field(
        default="extract_web",
        pattern="^(fulltext_rss|extract_web|newsnow_digest|byparr_browser)$",
    )
    use_proxy: str = Field(default="auto", pattern="^(auto|direct|proxy)$")
    ai_options: dict[str, Any] = Field(default_factory=dict)
    max_items: int = Field(default=15, ge=1, le=60)
    enabled: bool = True


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    kind: str | None = Field(
        default=None, pattern="^(newsnow|rss|rsshub|webpage|webpage_list)$"
    )
    identifier: str | None = Field(default=None, min_length=1, max_length=1024)
    category: str | None = Field(default=None, max_length=64)
    fetch_strategy: str | None = Field(
        default=None,
        pattern="^(fulltext_rss|extract_web|newsnow_digest|byparr_browser)$",
    )
    use_proxy: str | None = Field(default=None, pattern="^(auto|direct|proxy)$")
    ai_options: dict[str, Any] | None = None
    max_items: int | None = Field(default=None, ge=1, le=60)
    enabled: bool | None = None


class ArticlePatch(BaseModel):
    is_read: bool | None = None
    is_starred: bool | None = None


class ClipUrlRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    title: str = Field(default="", max_length=500)
    push_to_kindle: bool = False
    target_email: str = ""
    use_proxy: str = "auto"


class ClipTextRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    content_format: str = Field(default="markdown", pattern="^(markdown|text|html)$")
    author: str = Field(default="", max_length=250)
    push_to_kindle: bool = False
    target_email: str = ""


class EditionSourceLinkIn(BaseModel):
    source_id: int
    sort_order: int = 0
    max_items_override: int | None = Field(default=None, ge=1, le=60)


class EditionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    subtitle: str = Field(default="每日精选刊物", max_length=256)
    cover_style: str = Field(default="editorial", pattern="^(editorial|dark|minimal)$")
    cron_expr: str = Field(default="0 6 * * *", max_length=64)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    lookback_hours: int = Field(default=24, ge=1, le=168)
    dedup_window_days: int = Field(default=14, ge=1, le=90)
    max_articles: int = Field(default=40, ge=1, le=120)
    max_mb_per_volume: int = Field(default=18, ge=4, le=23)
    grayscale_images: bool = False
    include_qr_links: bool = False
    strip_links: bool = True
    retention_days: int = Field(default=30, ge=1, le=365)
    ai_briefing_enabled: bool = True
    target_emails: str = ""
    enabled: bool = True
    sources: list[EditionSourceLinkIn] = Field(default_factory=list)


class EditionUpdate(BaseModel):
    name: str | None = None
    subtitle: str | None = None
    cover_style: str | None = Field(default=None, pattern="^(editorial|dark|minimal)$")
    cron_expr: str | None = None
    timezone: str | None = None
    lookback_hours: int | None = Field(default=None, ge=1, le=168)
    dedup_window_days: int | None = Field(default=None, ge=1, le=90)
    max_articles: int | None = Field(default=None, ge=1, le=120)
    max_mb_per_volume: int | None = Field(default=None, ge=4, le=23)
    grayscale_images: bool | None = None
    include_qr_links: bool | None = None
    strip_links: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=365)
    ai_briefing_enabled: bool | None = None
    target_emails: str | None = None
    enabled: bool | None = None
    sources: list[EditionSourceLinkIn] | None = None


class BookScheduleRequest(BaseModel):
    scheduled_push_at: datetime | None = None


class BookDeliverRequest(BaseModel):
    target_email: str = ""


class SmtpSettingsUpdate(BaseModel):
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    use_ssl: bool | None = None
    username: str | None = None
    password: str | None = None
    from_email: str | None = None
    default_kindle_email: str | None = None


class SmtpTestRequest(BaseModel):
    target_email: str = ""


class AiSettingsUpdate(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    use_proxy: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=10)
    timeout_seconds: int | None = Field(default=None, ge=10, le=180)


class NetworkSettingsUpdate(BaseModel):
    outbound_proxy: str | None = None
    newsnow_base_url: str | None = None
    rsshub_base_url: str | None = None
    byparr_base_url: str | None = None


class McpTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    readonly: bool = True


class McpTokenUpdate(BaseModel):
    readonly: bool | None = None
    enabled: bool | None = None
