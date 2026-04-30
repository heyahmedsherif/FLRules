"""Database engine and session helpers."""

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from flrules.config import settings

_db_url = settings.database_url

# When DATA_DIR is set (e.g. a mounted volume on Railway), override any
# SQLite path so the file lives on persistent storage. This guarantees
# subscribers, alerts, and notices survive container redeploys.
_data_dir = os.environ.get("DATA_DIR")
if _data_dir and "sqlite" in _db_url:
    _db_url = f"sqlite+aiosqlite:///{_data_dir.rstrip('/')}/flrules.db"

if "sqlite" in _db_url:
    db_path = _db_url.split("///")[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(_db_url, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
