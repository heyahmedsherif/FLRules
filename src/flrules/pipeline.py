"""
Pipeline — the main orchestrator that ties scraping, filtering, storage, and notification together.

Called on a schedule (default: hourly) or manually via CLI.
"""

import secrets

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flrules.db import async_session, init_db
from flrules.models import Alert, FARIssue, FARNotice, Subscriber
from flrules.notifier import notify_subscribers
from flrules.relevance import filter_notices
from flrules.scraper import ScrapedNotice, fetch_latest_issue_ids, scrape_issue
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
    alert = Alert(
        notice_id=notice.notice_id,
        matched_keywords=", ".join(match_result.matched_keywords[:10]),
        relevance_score=match_result.score,
        category=", ".join(match_result.categories),
        summary=match_result.summary_reason,
    )
    session.add(alert)
    await session.flush()
    return alert


async def _get_active_subscribers(session: AsyncSession) -> list[Subscriber]:
    result = await session.execute(select(Subscriber).where(Subscriber.active == True))  # noqa: E712
    return list(result.scalars().all())


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
            for notice in issue.notices:
                if not await _notice_already_stored(session, notice.notice_id):
                    await _store_notice(session, notice)
                    new_notices.append(notice)
                    stats["notices_new"] += 1

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
        return sub
