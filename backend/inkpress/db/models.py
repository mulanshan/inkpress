from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from inkpress.db.base import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), default="Reader", nullable=False)
    kindle_email: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # kind: newsnow | rss | rsshub | webpage
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # identifier: NewsNow id (e.g. "zhihu"), RSS URL, or "rsshub://cw/today"
    identifier: Mapped[str] = mapped_column(String(1024), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="综合", nullable=False)
    # fetch_strategy: fulltext_rss | extract_web | newsnow_digest | byparr_browser
    fetch_strategy: Mapped[str] = mapped_column(String(32), default="extract_web", nullable=False)
    # use_proxy: auto | direct | proxy
    use_proxy: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    ai_options: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    max_items: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    articles: Mapped[list[Article]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    edition_links: Mapped[list[EditionSource]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("kind", "identifier", name="uq_source_kind_identifier"),)


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    canonical_url: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    translated_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    author: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="综合", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="zh-CN", nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    lead_image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    clean_html: Mapped[str] = mapped_column(Text, default="", nullable=False)
    plain_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    bilingual_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reading_time_min: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    extra_meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    source: Mapped[Source | None] = relationship(back_populates="articles")


class Edition(Base):
    __tablename__ = "editions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(256), default="每日精选刊物", nullable=False)
    cover_style: Mapped[str] = mapped_column(String(32), default="editorial", nullable=False)
    cron_expr: Mapped[str] = mapped_column(String(64), default="0 6 * * *", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)
    lookback_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    dedup_window_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    max_articles: Mapped[int] = mapped_column(Integer, default=40, nullable=False)
    max_mb_per_volume: Mapped[int] = mapped_column(Integer, default=18, nullable=False)
    grayscale_images: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    include_qr_links: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_briefing_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    target_emails: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    issue_counter: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    source_links: Mapped[list[EditionSource]] = relationship(
        back_populates="edition",
        cascade="all, delete-orphan",
        order_by="EditionSource.sort_order",
    )


class EditionSource(Base):
    __tablename__ = "edition_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_id: Mapped[int] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_items_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    edition: Mapped[Edition] = relationship(back_populates="source_links")
    source: Mapped[Source] = relationship(back_populates="edition_links")

    __table_args__ = (UniqueConstraint("edition_id", "source_id", name="uq_edition_source"),)


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # kind: edition_issue | uploaded_book | magazine | web_clip
    kind: Mapped[str] = mapped_column(String(32), default="uploaded_book", nullable=False)
    edition_id: Mapped[int | None] = mapped_column(
        ForeignKey("editions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="zh-CN", nullable=False)
    original_format: Mapped[str] = mapped_column(String(16), default="epub", nullable=False)
    original_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    kindle_epub_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    cover_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # conversion_status: ready | converting | failed
    conversion_status: Mapped[str] = mapped_column(String(24), default="ready", nullable=False)
    conversion_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    scheduled_push_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    pushed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_id: Mapped[int | None] = mapped_column(
        ForeignKey("editions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    book_id: Mapped[int | None] = mapped_column(
        ForeignKey("books.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    target_email: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # status: pending | running | sent | skipped | failed
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    article_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    volume_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stage_timings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArticleDelivery(Base):
    __tablename__ = "article_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_id: Mapped[int] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    __table_args__ = (
        UniqueConstraint("edition_id", "article_id", name="uq_article_delivery_edition_article"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class McpToken(Base):
    __tablename__ = "mcp_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    readonly: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


Index("ix_articles_source_published", Article.source_id, Article.published_at.desc())
