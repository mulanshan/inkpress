from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from inkpress.db.models import Article
from inkpress.logging import get_logger

logger = get_logger(__name__)


async def search_articles_fulltext(
    session: AsyncSession,
    query: str,
    *,
    category: str | None = None,
    source_id: int | None = None,
    starred_only: bool = False,
    unread_only: bool = False,
    limit: int = 40,
    offset: int = 0,
) -> tuple[list[Article], int]:
    """Search articles using ParadeDB BM25 (pg_search 0.27 pdb.jieba) on PostgreSQL
    when available, gracefully falling back to ILIKE matching on standard PG or SQLite.

    Returns ``(items, total_count)``.
    """
    cleaned = query.strip()

    orm_filters: list[ColumnElement[bool]] = []
    if category:
        orm_filters.append(Article.category == category)
    if source_id is not None:
        orm_filters.append(Article.source_id == source_id)
    if starred_only:
        orm_filters.append(Article.is_starred.is_(True))
    if unread_only:
        orm_filters.append(Article.is_read.is_(False))

    if not cleaned:
        count_stmt = select(func.count(Article.id)).where(*orm_filters)
        total = int((await session.execute(count_stmt)).scalar_one())
        stmt = (
            select(Article)
            .where(*orm_filters)
            .order_by(desc(Article.published_at), desc(Article.id))
            .limit(limit)
            .offset(offset)
        )
        items = list((await session.execute(stmt)).scalars().all())
        return items, total

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        try:
            where_clauses = ["(title ||| :q OR plain_text ||| :q OR ai_summary ||| :q)"]
            params: dict[str, Any] = {"q": cleaned}
            if category:
                where_clauses.append("category = :category")
                params["category"] = category
            if source_id is not None:
                where_clauses.append("source_id = :source_id")
                params["source_id"] = source_id
            if starred_only:
                where_clauses.append("is_starred = TRUE")
            if unread_only:
                where_clauses.append("is_read = FALSE")
            where_sql = " AND ".join(where_clauses)

            count_sql = text(f"SELECT COUNT(*) FROM articles WHERE {where_sql}")
            bm25_sql = text(
                f"""
                SELECT id FROM articles
                WHERE {where_sql}
                ORDER BY pdb.score(id) DESC, published_at DESC, id DESC
                LIMIT :limit OFFSET :offset
                """
            )
            page_params = {**params, "limit": limit, "offset": offset}

            async with session.begin_nested():
                total_bm25 = int((await session.execute(count_sql, params)).scalar_one())
                if total_bm25 == 0:
                    return [], 0
                rows: list[int] = list(
                    (await session.execute(bm25_sql, page_params)).scalars().all()
                )
            if not rows:
                return [], total_bm25
            articles_map = {
                a.id: a
                for a in (await session.execute(select(Article).where(Article.id.in_(rows))))
                .scalars()
                .all()
            }
            ordered = [articles_map[aid] for aid in rows if aid in articles_map]
            return ordered, total_bm25
        except Exception as exc:
            logger.debug("paradedb_bm25_fallback", reason=str(exc))

    pattern = f"%{cleaned}%"
    search_filter = or_(
        Article.title.ilike(pattern),
        Article.plain_text.ilike(pattern),
        Article.ai_summary.ilike(pattern),
        Article.source_name.ilike(pattern),
    )
    all_filters = [search_filter, *orm_filters]
    total = int((await session.execute(select(func.count(Article.id)).where(*all_filters))).scalar_one())
    stmt = (
        select(Article)
        .where(*all_filters)
        .order_by(desc(Article.published_at), desc(Article.id))
        .limit(limit)
        .offset(offset)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return items, total
