"""Database engine and session helpers."""

import os
from pathlib import Path

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from flrules.config import settings

log = structlog.get_logger()

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
    """Create all tables if they don't exist, then apply lightweight in-place
    migrations for additive columns. SQLModel.metadata.create_all does not add
    columns to existing tables, so any new column on an existing model needs an
    ALTER TABLE here. Each migration is idempotent: it checks the column list
    first and skips if already applied. Safe to run on every startup.

    create_all and the migration run in separate transactions because PRAGMA
    table_info does not observe DDL queued earlier in the same transaction
    when it was issued via conn.run_sync."""
    # Importing models here (rather than at module top) keeps the import
    # graph clean while guaranteeing the model classes are registered with
    # SQLModel.metadata before create_all runs. Without this, callers that
    # only import flrules.db (e.g. the `flrules init-database` CLI command)
    # would call create_all against an empty metadata and create no tables.
    from flrules import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with engine.begin() as conn:
        await _migrate_additive_columns(conn)


async def _migrate_additive_columns(conn) -> None:
    """Add columns introduced after the original schema if they are missing.
    Only runs against SQLite — other backends would need a proper migration
    tool, but the project ships SQLite-only. All additions are idempotent."""
    if engine.dialect.name != "sqlite":
        return

    migrations = {
        "far_notices": [
            ("wayback_url", "VARCHAR DEFAULT ''"),
            ("wayback_timestamp", "VARCHAR DEFAULT ''"),
            ("tamper_detected", "BOOLEAN DEFAULT 0"),
            ("content_hash", "VARCHAR DEFAULT ''"),
            ("chain_hash", "VARCHAR DEFAULT ''"),
            ("prev_chain_hash", "VARCHAR DEFAULT ''"),
        ],
        "far_issues": [
            ("last_verified_at", "DATETIME"),
        ],
    }

    for table, additions in migrations.items():
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing = {row[1] for row in result.fetchall()}
        for col, ddl in additions:
            if col not in existing:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                )
                log.info("db_migration_applied", table=table, column=col)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
