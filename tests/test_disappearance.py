"""Tests for disappearance detection.

The check re-scrapes recent issues and creates a 'disappearance' alert for any
notice we previously stored that the live site no longer shows. Critical edge
cases tested here:

  - alert is created when a notice vanishes
  - empty live result is treated as a transient error, not mass-disappearance
  - rate limiting via last_verified_at prevents repeated re-scrapes
  - feature is OFF by default so existing deploys don't get extra requests
"""

import importlib
from datetime import datetime, timedelta

from sqlalchemy import select


async def _fresh_modules(monkeypatch, tmp_path):
    """Reload db + pipeline against a tmp database so each test starts clean."""
    from flrules.config import settings as config_settings

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(
        config_settings, "database_url", f"sqlite+aiosqlite:///{db_file}"
    )

    from flrules import db as db_module

    importlib.reload(db_module)
    from flrules import pipeline as pipeline_module

    importlib.reload(pipeline_module)
    return db_module, pipeline_module, config_settings


async def test_disappearance_creates_alert(monkeypatch, tmp_path):
    db_module, pipeline_module, settings = await _fresh_modules(monkeypatch, tmp_path)
    from flrules.models import Alert, FARIssue, FARNotice
    from flrules.scraper import ScrapedNotice

    monkeypatch.setattr(settings, "verify_disappearances", True)
    monkeypatch.setattr(settings, "verify_recent_issues", 1)
    monkeypatch.setattr(settings, "verify_interval_hours", 0)  # always re-check

    await db_module.init_db()
    async with db_module.async_session() as session:
        session.add(FARIssue(iid=4616, volume="52/65", publish_date="April 3, 2026"))
        session.add(FARNotice(notice_id=11111, issue_iid=4616, section_number=2,
                              description="Will still be there"))
        session.add(FARNotice(notice_id=22222, issue_iid=4616, section_number=2,
                              description="Will vanish from the live site"))
        await session.commit()

    # Mock scrape_section so notice 22222 is no longer present in section 2.
    async def fake_scrape_section(client, iid, section_num):
        if iid == 4616 and section_num == 2:
            return [
                ScrapedNotice(
                    notice_id=11111, issue_iid=iid, section_number=2,
                    section_name="Proposed Rules", agency_code="40C",
                    description="Will still be there", publish_date="4/3/2026",
                    url="https://flrules.org/Gateway/View_notice.asp?id=11111",
                )
            ]
        return []  # other sections are empty in this fixture

    monkeypatch.setattr(pipeline_module, "scrape_section", fake_scrape_section)

    async with db_module.async_session() as session:
        stats: dict = {}
        await pipeline_module._check_for_disappearances(session, stats)
        await session.commit()

    assert stats.get("disappearances_detected") == 1

    async with db_module.async_session() as session:
        result = await session.execute(
            select(Alert).where(Alert.category == "disappearance")
        )
        alerts = list(result.scalars().all())
        assert len(alerts) == 1
        assert alerts[0].notice_id == 22222


async def test_empty_live_result_does_not_alert(monkeypatch, tmp_path):
    """If every section returns zero notices, treat as a transient site error
    rather than declaring everything has disappeared."""
    db_module, pipeline_module, settings = await _fresh_modules(monkeypatch, tmp_path)
    from flrules.models import Alert, FARIssue, FARNotice

    monkeypatch.setattr(settings, "verify_disappearances", True)
    monkeypatch.setattr(settings, "verify_recent_issues", 1)
    monkeypatch.setattr(settings, "verify_interval_hours", 0)

    await db_module.init_db()
    async with db_module.async_session() as session:
        session.add(FARIssue(iid=4616, volume="52/65", publish_date="April 3, 2026"))
        session.add(FARNotice(notice_id=11111, issue_iid=4616, section_number=2,
                              description="something"))
        await session.commit()

    async def empty_scrape(client, iid, section_num):
        return []

    monkeypatch.setattr(pipeline_module, "scrape_section", empty_scrape)

    async with db_module.async_session() as session:
        stats: dict = {}
        await pipeline_module._check_for_disappearances(session, stats)
        await session.commit()

    assert stats.get("disappearances_detected", 0) == 0
    async with db_module.async_session() as session:
        result = await session.execute(
            select(Alert).where(Alert.category == "disappearance")
        )
        assert list(result.scalars().all()) == []


async def test_rate_limit_skips_recently_verified_issue(monkeypatch, tmp_path):
    db_module, pipeline_module, settings = await _fresh_modules(monkeypatch, tmp_path)
    from flrules.models import FARIssue, FARNotice

    monkeypatch.setattr(settings, "verify_disappearances", True)
    monkeypatch.setattr(settings, "verify_recent_issues", 1)
    monkeypatch.setattr(settings, "verify_interval_hours", 24)

    await db_module.init_db()
    # Pre-set last_verified_at to "1 hour ago" so the 24-hour limiter skips it.
    recent = datetime.utcnow() - timedelta(hours=1)
    async with db_module.async_session() as session:
        session.add(FARIssue(iid=4616, volume="x", publish_date="x", last_verified_at=recent))
        session.add(FARNotice(notice_id=11111, issue_iid=4616, section_number=2,
                              description="x"))
        await session.commit()

    called_with = []

    async def fake_scrape_section(client, iid, section_num):
        called_with.append((iid, section_num))
        return []

    monkeypatch.setattr(pipeline_module, "scrape_section", fake_scrape_section)

    async with db_module.async_session() as session:
        stats: dict = {}
        await pipeline_module._check_for_disappearances(session, stats)

    assert called_with == []  # rate-limited, no scraping happened


async def test_disappearance_check_off_by_default(monkeypatch, tmp_path):
    """Defaulting OFF is the safety property — existing deploys don't gain
    extra HTTP traffic just by upgrading."""
    db_module, pipeline_module, settings = await _fresh_modules(monkeypatch, tmp_path)

    assert settings.verify_disappearances is False
