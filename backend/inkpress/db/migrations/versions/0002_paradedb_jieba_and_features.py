"""Upgrade ParadeDB BM25 index to pg_search 0.27 pdb.jieba and add Edition features.

Revision ID: 0002_paradedb_jieba
Revises: 0001_initial
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_paradedb_jieba"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "editions",
        sa.Column("strip_links", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "editions",
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DO $$
            BEGIN
                CREATE EXTENSION IF NOT EXISTS pg_search CASCADE;
                DROP INDEX IF EXISTS ix_articles_bm25;
                CREATE INDEX ix_articles_bm25 ON articles
                USING bm25 (id, (title::pdb.jieba), (plain_text::pdb.jieba), (ai_summary::pdb.jieba));
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'ParadeDB pg_search jieba index upgrade skipped: %', SQLERRM;
            END $$;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DO $$
            BEGIN
                DROP INDEX IF EXISTS ix_articles_bm25;
                CREATE INDEX IF NOT EXISTS ix_articles_bm25 ON articles
                USING bm25 (id, title, plain_text, ai_summary)
                WITH (key_field='id');
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'ParadeDB pg_search downgrade skipped: %', SQLERRM;
            END $$;
            """
        )

    op.drop_column("editions", "retention_days")
    op.drop_column("editions", "strip_links")
