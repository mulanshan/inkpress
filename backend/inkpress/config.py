from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_INSECURE_SECRET_KEY = "dev-insecure-secret-key-change-in-production-32chars"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    tz: str = Field(default="Asia/Shanghai", alias="TZ")
    app_env: str = Field(default="production", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")
    static_dir: Path | None = Field(default=None, alias="STATIC_DIR")

    # Security
    app_secret_key: str = Field(
        default=DEFAULT_INSECURE_SECRET_KEY,
        alias="APP_SECRET_KEY",
    )
    jwt_expire_hours: int = Field(default=168, alias="JWT_EXPIRE_HOURS")
    admin_username: str | None = Field(default=None, alias="ADMIN_USERNAME")
    admin_password: str | None = Field(default=None, alias="ADMIN_PASSWORD")

    # Database & Redis
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/inkpress.db",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://127.0.0.1:6379/5", alias="REDIS_URL")

    # Upstream content & scraping helpers
    newsnow_base_url: str = Field(default="http://newsnow:4444", alias="NEWSNOW_BASE_URL")
    rsshub_base_url: str = Field(default="http://rsshub:1200", alias="RSSHUB_BASE_URL")
    byparr_base_url: str = Field(default="http://byparr:8191", alias="BYPARR_BASE_URL")
    outbound_proxy: str = Field(default="", alias="OUTBOUND_PROXY")

    # Storage
    library_data_dir: Path = Field(default=Path("./data/library"), alias="LIBRARY_DATA_DIR")

    # Optional default SMTP (can also be configured & encrypted in DB via Studio UI)
    smtp_host: str = Field(default="smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(default=465, alias="SMTP_PORT")
    smtp_use_ssl: bool = Field(default=True, alias="SMTP_USE_SSL")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", alias="SMTP_FROM_EMAIL")
    default_kindle_email: str = Field(default="", alias="DEFAULT_KINDLE_EMAIL")

    # Optional default AI (OpenAI-compatible)
    ai_base_url: str = Field(default="https://api.openai.com/v1", alias="AI_BASE_URL")
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    ai_model: str = Field(default="gpt-4o-mini", alias="AI_MODEL")
    ai_enabled: bool = Field(default=False, alias="AI_ENABLED")

    def ensure_storage_dirs(self) -> None:
        for sub in ("originals", "epubs", "covers", "editions", "tmp", "exports"):
            (self.library_data_dir / sub).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
