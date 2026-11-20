from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from inkpress.config import get_settings
from inkpress.db.base import Base, get_engine, reset_db_globals
from inkpress.main import create_app


@pytest.fixture
async def test_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    db_file = tmp_path / "test_inkpress.db"
    lib_dir = tmp_path / "library"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("LIBRARY_DATA_DIR", str(lib_dir))
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-at-least-32-bytes-long!!")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/15")
    monkeypatch.setenv("LOG_FORMAT", "console")

    get_settings.cache_clear()
    reset_db_globals()

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    await engine.dispose()
    reset_db_globals()
    get_settings.cache_clear()
