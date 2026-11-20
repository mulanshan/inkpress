from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from inkpress import __version__
from inkpress.api import (
    articles,
    auth,
    books,
    deliveries,
    editions,
    health,
    media,
    settings,
    sources,
)
from inkpress.config import DEFAULT_INSECURE_SECRET_KEY, Settings, get_settings
from inkpress.db.base import get_session_factory
from inkpress.db.models import User
from inkpress.logging import configure_logging, get_logger
from inkpress.mcp import server as mcp_server
from inkpress.security import hash_password

logger = get_logger(__name__)


def validate_runtime_security(cfg: Settings) -> None:
    """Refuse to start in production with the default insecure secret key."""
    if cfg.app_env.strip().lower() == "production" and (
        not cfg.app_secret_key
        or cfg.app_secret_key == DEFAULT_INSECURE_SECRET_KEY
        or cfg.app_secret_key.startswith("dev-insecure-secret-key")
    ):
        raise RuntimeError(
            "Refusing to start in production with the default insecure APP_SECRET_KEY. "
            "Set a strong random APP_SECRET_KEY environment variable."
        )


async def _bootstrap_admin_from_env() -> None:
    cfg = get_settings()
    if not (cfg.admin_username and cfg.admin_password):
        return
    factory = get_session_factory()
    async with factory() as session:
        count = int((await session.execute(select(func.count(User.id)))).scalar_one())
        if count == 0:
            session.add(
                User(
                    username=cfg.admin_username.strip(),
                    password_hash=hash_password(cfg.admin_password),
                    display_name=cfg.admin_username.strip(),
                    kindle_email=cfg.default_kindle_email,
                    preferences={
                        "theme": "paper",
                        "font_family": "serif",
                        "font_size": 18,
                        "line_height": 1.8,
                    },
                )
            )
            await session.commit()
            logger.info("admin_bootstrapped_from_env", username=cfg.admin_username)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    cfg = get_settings()
    validate_runtime_security(cfg)
    cfg.ensure_storage_dirs()
    try:
        await _bootstrap_admin_from_env()
    except Exception as exc:
        logger.debug("bootstrap_env_skipped", err=str(exc))
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="InkPress",
        version=__version__,
        description="Self-hosted editorial reading website & zero-E999 Kindle publishing studio",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(sources.router)
    app.include_router(articles.router)
    app.include_router(editions.router)
    app.include_router(books.router)
    app.include_router(deliveries.router)
    app.include_router(settings.router)
    app.include_router(media.router)
    app.include_router(mcp_server.router)

    cfg = get_settings()
    static_dir = cfg.static_dir
    if static_dir and static_dir.exists() and (static_dir / "index.html").exists():
        static_root = static_dir.resolve()
        assets_dir = static_root / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_html = static_root / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> FileResponse:
            normalized = full_path.lstrip("/")
            if normalized == "api" or normalized.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            if normalized:
                candidate = (static_root / normalized).resolve()
                if not candidate.is_relative_to(static_root):
                    raise HTTPException(status_code=404, detail="Not Found")
                if candidate.exists() and candidate.is_file():
                    return FileResponse(candidate)
            return FileResponse(index_html)

    return app


app = create_app()
