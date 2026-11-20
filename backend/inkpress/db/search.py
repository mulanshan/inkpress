from __future__ import annotations

from sqlalchemy import desc, or_, select, text
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
    limit: int = 40,
    offset: int = 0,
) -> list[Article]:
    """Search articles using ParadeDB BM25 on PostgreSQL when available,
    gracefully falling back to ILIKE matching on standard PG or SQLite."""
    cleaned = query.strip()
    if not cleaned:
        stmt = select(Article)
        if category:
            stmt = stmt.where(Article.category == category)
        if source_id is not None:
            stmt = stmt.where(Article.source_id == source_id)
        if starred_only:
            stmt = stmt.where(Article.is_starred.is_(True))
        stmt = (
            stmt.order_by(desc(Article.published_at), desc(Article.id)).limit(limit).offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all())

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        try:
            bm25_sql = text(
                """
                SELECT id FROM articles
                WHERE (title ||| :q OR plain_text ||| :q OR COALESCE(ai_summary, '') ||| :q)
                  AND (:category IS NULL OR category = :category)
                  AND (:source_id IS NULL OR source_id = :source_id)
                  AND (:starred_only = FALSE OR is_starred = TRUE)
                ORDER BY paradedb.score(id) DESC, published_at DESC
                LIMIT :limit OFFSET :offset
                """
            )
            async with session.begin_nested():
                rows: list[int] = list(
                    (
                        await session.execute(
                            bm25_sql,
                            {
                                "q": cleaned,
                                "category": category,
                                "source_id": source_id,
                                "starred_only": starred_only,
                                "limit": limit,
                                "offset": offset,
                            },
                        )
                    )
                    .scalars()
                    .all()
                )
            if rows:
                articles_map = {
                    a.id: a
                    for a in (await session.execute(select(Article).where(Article.id.in_(rows))))
                    .scalars()
                    .all()
                }
                return [articles_map[aid] for aid in rows if aid in articles_map]
            return []
        except Exception as exc:
            logger.debug("paradedb_bm25_fallback", reason=str(exc))

    pattern = f"%{cleaned}%"
    stmt = select(Article).where(
        or_(
            Article.title.ilike(pattern),
            Article.plain_text.ilike(pattern),
            Article.ai_summary.ilike(pattern),
            Article.source_name.ilike(pattern),
        )
    )
    if category:
        stmt = stmt.where(Article.category == category)
    if source_id is not None:
        stmt = stmt.where(Article.source_id == source_id)
    if starred_only:
        stmt = stmt.where(Article.is_starred.is_(True))
    stmt = stmt.order_by(desc(Article.published_at), desc(Article.id)).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())
