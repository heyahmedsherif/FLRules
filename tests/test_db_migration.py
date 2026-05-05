"""Tests for the additive in-place migration on far_notices.

The migration path runs every time the pipeline boots — it must be idempotent
and must not corrupt an existing database that pre-dates the new columns.
"""

import importlib

from sqlalchemy import text


async def _column_names(conn, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result.fetchall()}


async def test_migration_adds_columns_to_legacy_db(tmp_path, monkeypatch):
    """A DB that lacks the new columns gets them added on init_db()."""
    db_file = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")

    from flrules.config import settings as config_settings

    monkeypatch.setattr(config_settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from flrules import db as db_module

    importlib.reload(db_module)

    # Simulate a legacy DB by creating only the old shape of far_notices.
    async with db_module.engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE far_notices ("
                "id INTEGER PRIMARY KEY, "
                "notice_id INTEGER UNIQUE, "
                "issue_iid INTEGER, "
                "section_number INTEGER, "
                "section_name VARCHAR, "
                "agency_code VARCHAR, "
                "description VARCHAR, "
                "full_text VARCHAR, "
                "publish_date VARCHAR, "
                "url VARCHAR, "
                "fetched_at DATETIME)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO far_notices (notice_id, issue_iid, section_number) "
                "VALUES (99999, 4616, 6)"
            )
        )

    # Run init_db — should add the missing columns without dropping the row.
    await db_module.init_db()

    async with db_module.engine.begin() as conn:
        cols = await _column_names(conn, "far_notices")
        for required in (
            "wayback_url", "wayback_timestamp", "tamper_detected",
            "content_hash", "chain_hash", "prev_chain_hash",
        ):
            assert required in cols, f"missing {required}"

        # FARIssue should also gain its new column.
        issue_cols = await _column_names(conn, "far_issues")
        assert "last_verified_at" in issue_cols

        result = await conn.execute(
            text("SELECT notice_id, wayback_url, tamper_detected, chain_hash FROM far_notices")
        )
        rows = result.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 99999
        assert rows[0][1] == ""  # default
        assert rows[0][2] in (0, False)  # default
        assert rows[0][3] == ""  # legacy row has no chain_hash yet

    await db_module.engine.dispose()


async def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Running init_db twice on a current DB must not raise."""
    db_file = tmp_path / "current.db"
    from flrules.config import settings as config_settings

    monkeypatch.setattr(config_settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from flrules import db as db_module

    importlib.reload(db_module)

    await db_module.init_db()
    await db_module.init_db()  # second call must be a no-op, not an error

    async with db_module.engine.begin() as conn:
        cols = await _column_names(conn, "far_notices")
        assert "wayback_url" in cols

    await db_module.engine.dispose()
