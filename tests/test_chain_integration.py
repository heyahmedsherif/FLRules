"""End-to-end test: notices ingested through the pipeline form a valid chain
that survives a restart (chain head is read from the DB on next run)."""

import importlib

from sqlalchemy import select


async def test_pipeline_builds_valid_chain_across_runs(monkeypatch, tmp_path):
    from flrules.config import settings as config_settings

    db_file = tmp_path / "chain.db"
    monkeypatch.setattr(config_settings, "database_url", f"sqlite+aiosqlite:///{db_file}")

    from flrules import db as db_module

    importlib.reload(db_module)
    from flrules import pipeline as pipeline_module

    importlib.reload(pipeline_module)
    from flrules.models import FARNotice
    from flrules.provenance import GENESIS_PREV_HASH, ChainEntry, verify_chain
    from flrules.scraper import ScrapedIssue, ScrapedNotice

    await db_module.init_db()

    # Skip the static site step (writes to disk; not what we're testing here).
    async def noop_site():
        return None

    monkeypatch.setattr(pipeline_module, "generate_static_site", noop_site)

    # First run: 2 notices.
    async def fetch_first_run(count=5):
        return [{"iid": 4616, "text": "April 3"}]

    async def scrape_first_run(iid, fetch_full_text=True):
        return ScrapedIssue(
            iid=iid,
            volume="52/65",
            publish_date="April 3, 2026",
            notices=[
                ScrapedNotice(
                    notice_id=1001, issue_iid=iid, section_number=2,
                    section_name="Proposed", agency_code="40C",
                    description="first notice", publish_date="4/3", url="u1",
                ),
                ScrapedNotice(
                    notice_id=1002, issue_iid=iid, section_number=2,
                    section_name="Proposed", agency_code="40C",
                    description="second notice", publish_date="4/3", url="u2",
                ),
            ],
        )

    monkeypatch.setattr(pipeline_module, "fetch_latest_issue_ids", fetch_first_run)
    monkeypatch.setattr(pipeline_module, "scrape_issue", scrape_first_run)

    await pipeline_module.run_pipeline(issue_count=1, notify=False)

    # Second run: a third notice in a different issue.
    async def fetch_second_run(count=5):
        return [{"iid": 4617, "text": "April 4"}]

    async def scrape_second_run(iid, fetch_full_text=True):
        return ScrapedIssue(
            iid=iid,
            volume="52/66",
            publish_date="April 4, 2026",
            notices=[
                ScrapedNotice(
                    notice_id=1003, issue_iid=iid, section_number=2,
                    section_name="Proposed", agency_code="40C",
                    description="third notice", publish_date="4/4", url="u3",
                ),
            ],
        )

    monkeypatch.setattr(pipeline_module, "fetch_latest_issue_ids", fetch_second_run)
    monkeypatch.setattr(pipeline_module, "scrape_issue", scrape_second_run)

    await pipeline_module.run_pipeline(issue_count=1, notify=False)

    # All three notices form a single valid chain rooted at GENESIS_PREV_HASH.
    async with db_module.async_session() as session:
        result = await session.execute(
            select(FARNotice).order_by(FARNotice.id.asc())
        )
        rows = list(result.scalars().all())

    assert len(rows) == 3
    assert rows[0].prev_chain_hash == GENESIS_PREV_HASH

    entries = [
        ChainEntry(
            notice_id=r.notice_id,
            content_hash=r.content_hash,
            prev_chain_hash=r.prev_chain_hash,
            chain_hash=r.chain_hash,
        )
        for r in rows
    ]
    ok, idx = verify_chain(entries)
    assert ok, f"chain invalid at index {idx}"
