"""
Pipeline — the main orchestrator that ties scraping, filtering, storage, and notification together.

Called on a schedule (default: hourly) or manually via CLI.
"""

import secrets
from datetime import datetime, timedelta

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flrules.archive import submit_to_archive
from flrules.config import settings
from flrules.db import async_session, init_db
from flrules.models import Alert, FARIssue, FARNotice, Subscriber
from flrules.notifier import notify_subscribers, send_opt_in_confirmation, send_opt_in_email
from flrules.provenance import GENESIS_PREV_HASH, build_entry
from flrules.relevance import filter_notices
from flrules.scraper import (
    SECTIONS_TO_MONITOR,
    ScrapedNotice,
    fetch_latest_issue_ids,
    scrape_issue,
    scrape_section,
)
from flrules.static_site import generate_static_site

log = structlog.get_logger()


async def _issue_already_processed(session: AsyncSession, iid: int) -> bool:
    result = await session.execute(select(FARIssue).where(FARIssue.iid == iid))
    return result.scalar_one_or_none() is not None


async def _notice_already_stored(session: AsyncSession, notice_id: int) -> bool:
    result = await session.execute(
        select(FARNotice).where(FARNotice.notice_id == notice_id)
    )
    return result.scalar_one_or_none() is not None


async def _store_notice(session: AsyncSession, notice: ScrapedNotice) -> FARNotice:
    db_notice = FARNotice(
        notice_id=notice.notice_id,
        issue_iid=notice.issue_iid,
        section_number=notice.section_number,
        section_name=notice.section_name,
        agency_code=notice.agency_code,
        description=notice.description,
        full_text=notice.full_text,
        publish_date=notice.publish_date,
        url=notice.url,
    )
    session.add(db_notice)
    await session.flush()
    return db_notice


async def _store_alert(
    session: AsyncSession, notice: ScrapedNotice, match_result
) -> Alert:
    # Subscribers want to know what the notice is about, not a meta-description
    # of why it matched. Use the notice's own description as the summary, with
    # the match reason kept as a fallback for sparse notices.
    desc = (notice.description or "").strip()
    if len(desc) > 280:
        desc = desc[:277].rstrip() + "..."
    summary = desc or match_result.summary_reason

    alert = Alert(
        notice_id=notice.notice_id,
        matched_keywords=", ".join(match_result.matched_keywords[:10]),
        relevance_score=match_result.score,
        category=", ".join(match_result.categories),
        summary=summary,
    )
    session.add(alert)
    await session.flush()
    return alert


async def _get_active_subscribers(session: AsyncSession) -> list[Subscriber]:
    result = await session.execute(select(Subscriber).where(Subscriber.active == True))  # noqa: E712
    return list(result.scalars().all())


async def _current_chain_head(session: AsyncSession) -> str:
    """Return the chain_hash of the most recently inserted notice, or the
    genesis sentinel if the chain is empty. Notices written before the chain
    feature was deployed have empty chain_hash and are skipped."""
    result = await session.execute(
        select(FARNotice.chain_hash)
        .where(FARNotice.chain_hash != "")
        .order_by(FARNotice.id.desc())
        .limit(1)
    )
    head = result.scalar_one_or_none()
    return head or GENESIS_PREV_HASH


async def _check_for_disappearances(
    session: AsyncSession, stats: dict
) -> None:
    """For each of the most-recent N issues we already have stored, re-scrape
    its sections and create a 'disappearance' alert for any notice_id that we
    previously saw but is no longer present on the live site. Rate-limited per
    issue by settings.verify_interval_hours."""
    cutoff = datetime.utcnow() - timedelta(hours=settings.verify_interval_hours)

    result = await session.execute(
        select(FARIssue).order_by(FARIssue.iid.desc()).limit(settings.verify_recent_issues)
    )
    issues_to_check = list(result.scalars().all())

    for issue in issues_to_check:
        if issue.last_verified_at and issue.last_verified_at > cutoff:
            log.debug("disappearance_check_skipped_recent", iid=issue.iid)
            continue

        stored_ids: set[int] = set()
        result = await session.execute(
            select(FARNotice.notice_id).where(FARNotice.issue_iid == issue.iid)
        )
        stored_ids = {row[0] for row in result.fetchall()}
        if not stored_ids:
            continue

        live_ids: set[int] = set()
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": settings.user_agent},
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                for section_num in SECTIONS_TO_MONITOR:
                    try:
                        notices = await scrape_section(client, issue.iid, section_num)
                        live_ids.update(n.notice_id for n in notices)
                    except Exception as e:
                        log.warning(
                            "disappearance_check_section_failed",
                            iid=issue.iid,
                            section=section_num,
                            error=str(e),
                        )
        except Exception as e:
            log.warning("disappearance_check_failed", iid=issue.iid, error=str(e))
            continue

        # If the live fetch returned nothing at all, treat as a transient site
        # error rather than mass disappearance — refusing to alert is the
        # conservative call here.
        if not live_ids:
            log.warning("disappearance_check_no_live_ids", iid=issue.iid)
            continue

        missing = stored_ids - live_ids
        for missing_id in missing:
            alert = Alert(
                notice_id=missing_id,
                matched_keywords="disappearance",
                relevance_score=0.0,
                category="disappearance",
                summary=(
                    f"Notice {missing_id} from issue {issue.iid} was previously "
                    "observed but is no longer present on flrules.org. This may "
                    "indicate a withdrawal, a site error, or tampering."
                ),
            )
            session.add(alert)
            stats.setdefault("disappearances_detected", 0)
            stats["disappearances_detected"] += 1
            log.warning(
                "disappearance_detected",
                iid=issue.iid,
                missing_notice_id=missing_id,
            )

        issue.last_verified_at = datetime.utcnow()
        session.add(issue)


async def run_pipeline(issue_count: int = 3, notify: bool = True) -> dict:
    """
    Main pipeline: fetch → filter → store → notify.

    Args:
        issue_count: Number of recent issues to check.
        notify: Whether to send notifications (disable for dry runs).

    Returns:
        Stats dict with counts of processed items.
    """
    await init_db()

    stats = {
        "issues_checked": 0,
        "issues_new": 0,
        "notices_scraped": 0,
        "notices_new": 0,
        "alerts_generated": 0,
        "notifications_sent": 0,
    }

    # 1. Discover recent issues
    recent_issues = await fetch_latest_issue_ids(count=issue_count)
    log.info("discovered_issues", count=len(recent_issues))

    async with async_session() as session:
        # Chain head is read once at the start of the run and advanced in-memory
        # as we insert notices. Persisted hashes survive across runs because each
        # FARNotice records its own chain_hash.
        chain_head = await _current_chain_head(session)

        for issue_meta in recent_issues:
            iid = issue_meta["iid"]
            stats["issues_checked"] += 1

            # Skip already-processed issues
            if await _issue_already_processed(session, iid):
                log.debug("issue_already_processed", iid=iid)
                continue

            stats["issues_new"] += 1

            # 2. Scrape the issue
            issue = await scrape_issue(iid)
            stats["notices_scraped"] += len(issue.notices)

            # Record the issue
            db_issue = FARIssue(
                iid=iid, volume=issue.volume, publish_date=issue.publish_date
            )
            session.add(db_issue)

            # 3. Store new notices and run relevance filter
            new_notices: list[ScrapedNotice] = []
            new_db_notices: list[FARNotice] = []
            for notice in issue.notices:
                if not await _notice_already_stored(session, notice.notice_id):
                    db_notice = await _store_notice(session, notice)
                    # Cryptographic provenance: link this notice into the chain.
                    # Pure computation — no I/O, no failure mode beyond bad inputs.
                    entry = build_entry(
                        notice_id=notice.notice_id,
                        section_number=notice.section_number,
                        agency_code=notice.agency_code,
                        description=notice.description,
                        full_text=notice.full_text,
                        prev_chain_hash=chain_head,
                    )
                    db_notice.content_hash = entry.content_hash
                    db_notice.prev_chain_hash = entry.prev_chain_hash
                    db_notice.chain_hash = entry.chain_hash
                    chain_head = entry.chain_hash
                    new_notices.append(notice)
                    new_db_notices.append(db_notice)
                    stats["notices_new"] += 1

            # 3b. Submit each new notice to the Wayback Machine for an independent,
            #     timestamped third-party witness. Best-effort: any failure logs and
            #     proceeds — the archive is not on the critical path for alerting.
            if settings.archive_enabled and new_db_notices:
                stats["archive_attempted"] = 0
                stats["archive_succeeded"] = 0
                cap = settings.archive_max_per_run
                for notice, db_notice in list(zip(new_notices, new_db_notices))[:cap]:
                    stats["archive_attempted"] += 1
                    result = await submit_to_archive(notice.url)
                    if result.success:
                        db_notice.wayback_url = result.wayback_url
                        db_notice.wayback_timestamp = result.timestamp
                        session.add(db_notice)
                        stats["archive_succeeded"] += 1

            # 4. Filter for relevant notices
            matches = filter_notices(new_notices)

            # 5. Create alerts and notify
            subscribers = await _get_active_subscribers(session) if notify else []

            for notice, match_result in matches:
                alert = await _store_alert(session, notice, match_result)
                stats["alerts_generated"] += 1

                if notify and subscribers:
                    delivery = await notify_subscribers(subscribers, alert, notice.url)
                    alert.notified = True
                    stats["notifications_sent"] += (
                        delivery.get("email_sent", 0) + delivery.get("sms_sent", 0)
                    )

            await session.commit()

        # 6. Disappearance detection — re-scrape the most recent stored issues
        #    and alert on notices that have vanished. Rate-limited and opt-in;
        #    runs in its own session block so a failure doesn't roll back the
        #    main pipeline transaction.
        if settings.verify_disappearances:
            try:
                await _check_for_disappearances(session, stats)
                await session.commit()
            except Exception as e:
                log.warning("disappearance_check_unexpected_error", error=str(e))
                await session.rollback()

    # Generate static dashboard for GitHub Pages
    await generate_static_site()

    log.info("pipeline_complete", stats=stats)
    return stats


async def add_subscriber(
    email: str | None = None,
    phone: str | None = None,
    name: str = "",
    categories: str = "all",
    notify_email: bool = True,
    notify_sms: bool = False,
) -> Subscriber:
    """Add a new subscriber."""
    await init_db()
    async with async_session() as session:
        sub = Subscriber(
            email=email,
            phone=phone,
            name=name,
            categories=categories,
            notify_email=notify_email,
            notify_sms=notify_sms,
            unsubscribe_token=secrets.token_urlsafe(16),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        log.info("subscriber_added", id=sub.id, email=email)

        if email and notify_email:
            await send_opt_in_email(email, name, sub.unsubscribe_token)

        if phone and notify_sms:
            await send_opt_in_confirmation(phone)

        return sub
