"""Initial InkPress schema and ParadeDB BM25 indexes.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False, server_default="Reader"),
        sa.Column("kindle_email", sa.String(256), nullable=False, server_default=""),
        sa.Column("preferences", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("identifier", sa.String(1024), nullable=False),
        sa.Column("category", sa.String(64), nullable=False, server_default="综合"),
        sa.Column("fetch_strategy", sa.String(32), nullable=False, server_default="extract_web"),
        sa.Column("use_proxy", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("ai_options", json_type, nullable=False),
        sa.Column("max_items", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("kind", "identifier", name="uq_source_kind_identifier"),
    )

    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("canonical_url", sa.String(2048), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("translated_title", sa.String(512), nullable=True),
        sa.Column("author", sa.String(256), nullable=False, server_default=""),
        sa.Column("source_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("category", sa.String(64), nullable=False, server_default="综合"),
        sa.Column("language", sa.String(16), nullable=False, server_default="zh-CN"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lead_image_url", sa.String(2048), nullable=True),
        sa.Column("clean_html", sa.Text(), nullable=False, server_default=""),
        sa.Column("plain_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("bilingual_html", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reading_time_min", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_starred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("extra_meta", json_type, nullable=False),
    )
    op.create_index("ix_articles_source_id", "articles", ["source_id"])
    op.create_index("ix_articles_content_hash", "articles", ["content_hash"])
    op.create_index("ix_articles_published_at", "articles", ["published_at"])
    op.create_index("ix_articles_fetched_at", "articles", ["fetched_at"])
    op.create_index("ix_articles_is_read", "articles", ["is_read"])
    op.create_index("ix_articles_is_starred", "articles", ["is_starred"])
    op.create_index("ix_articles_source_published", "articles", ["source_id", "published_at"])

    op.create_table(
        "editions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("subtitle", sa.String(256), nullable=False, server_default="每日精选刊物"),
        sa.Column("cover_style", sa.String(32), nullable=False, server_default="editorial"),
        sa.Column("cron_expr", sa.String(64), nullable=False, server_default="0 6 * * *"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("lookback_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("dedup_window_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("max_articles", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("max_mb_per_volume", sa.Integer(), nullable=False, server_default="18"),
        sa.Column("grayscale_images", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("include_qr_links", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_briefing_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("target_emails", sa.String(512), nullable=False, server_default=""),
        sa.Column("issue_counter", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "edition_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "edition_id",
            sa.Integer(),
            sa.ForeignKey("editions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_items_override", sa.Integer(), nullable=True),
        sa.UniqueConstraint("edition_id", "source_id", name="uq_edition_source"),
    )
    op.create_index("ix_edition_sources_edition_id", "edition_sources", ["edition_id"])
    op.create_index("ix_edition_sources_source_id", "edition_sources", ["source_id"])

    op.create_table(
        "books",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="uploaded_book"),
        sa.Column(
            "edition_id",
            sa.Integer(),
            sa.ForeignKey("editions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("author", sa.String(256), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("language", sa.String(16), nullable=False, server_default="zh-CN"),
        sa.Column("original_format", sa.String(16), nullable=False, server_default="epub"),
        sa.Column("original_path", sa.String(1024), nullable=False, server_default=""),
        sa.Column("kindle_epub_path", sa.String(1024), nullable=False, server_default=""),
        sa.Column("cover_path", sa.String(1024), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversion_status", sa.String(24), nullable=False, server_default="ready"),
        sa.Column("conversion_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", json_type, nullable=False),
        sa.Column("scheduled_push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pushed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_books_edition_id", "books", ["edition_id"])
    op.create_index("ix_books_scheduled_push_at", "books", ["scheduled_push_at"])
    op.create_index("ix_books_created_at", "books", ["created_at"])

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "edition_id",
            sa.Integer(),
            sa.ForeignKey("editions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "book_id",
            sa.Integer(),
            sa.ForeignKey("books.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("target_email", sa.String(512), nullable=False, server_default=""),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("article_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("volume_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_timings", json_type, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_deliveries_edition_id", "deliveries", ["edition_id"])
    op.create_index("ix_deliveries_book_id", "deliveries", ["book_id"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])
    op.create_index("ix_deliveries_created_at", "deliveries", ["created_at"])

    op.create_table(
        "article_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "edition_id",
            sa.Integer(),
            sa.ForeignKey("editions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "article_id",
            sa.Integer(),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "delivery_id",
            sa.Integer(),
            sa.ForeignKey("deliveries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("edition_id", "article_id", name="uq_article_delivery_edition_article"),
    )
    op.create_index("ix_article_deliveries_edition_id", "article_deliveries", ["edition_id"])
    op.create_index("ix_article_deliveries_article_id", "article_deliveries", ["article_id"])
    op.create_index("ix_article_deliveries_delivered_at", "article_deliveries", ["delivered_at"])

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value_json", json_type, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "mcp_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_prefix", sa.String(24), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("readonly", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_tokens_token_hash", "mcp_tokens", ["token_hash"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DO $$
            BEGIN
                CREATE EXTENSION IF NOT EXISTS pg_search;
                CREATE INDEX IF NOT EXISTS ix_articles_bm25 ON articles
                USING bm25 (id, title, plain_text, ai_summary)
                WITH (key_field='id');
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'ParadeDB pg_search unavailable, skipping BM25 index: %', SQLERRM;
            END $$;
            """
        )


def downgrade() -> None:
    op.drop_table("mcp_tokens")
    op.drop_table("app_settings")
    op.drop_table("article_deliveries")
    op.drop_table("deliveries")
    op.drop_table("books")
    op.drop_table("edition_sources")
    op.drop_table("editions")
    op.drop_table("articles")
    op.drop_table("sources")
    op.drop_table("users")
