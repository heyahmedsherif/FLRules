"""CLI interface — run the pipeline, manage subscribers, inspect alerts."""

import asyncio

import click
import structlog

structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(),
    ],
)


@click.group()
def cli():
    """FLRules Monitor — Florida Administrative Register alert system."""
    pass


@cli.command()
@click.option("--issues", default=3, help="Number of recent issues to check")
@click.option("--notify/--no-notify", default=False, help="Send notifications (default: dry run)")
def run(issues: int, notify: bool):
    """Run the monitoring pipeline."""
    from flrules.pipeline import run_pipeline

    stats = asyncio.run(run_pipeline(issue_count=issues, notify=notify))
    click.echo("\n── Pipeline Results ──")
    for key, val in stats.items():
        click.echo(f"  {key}: {val}")


@cli.command()
@click.option("--email", prompt=True, help="Subscriber email")
@click.option("--phone", default=None, help="Phone number for SMS")
@click.option("--name", default="", help="Subscriber name")
@click.option(
    "--categories",
    default="all",
    help="Comma-separated categories (or 'all')",
)
def subscribe(email: str, phone: str | None, name: str, categories: str):
    """Add a new subscriber."""
    from flrules.pipeline import add_subscriber

    sub = asyncio.run(
        add_subscriber(email=email, phone=phone, name=name, categories=categories)
    )
    click.echo(f"Subscriber added: {sub.name or sub.email} (id={sub.id})")


@cli.command()
@click.option("--limit", default=20, help="Number of alerts to show")
def alerts(limit: int):
    """Show recent alerts."""
    from flrules.db import async_session, init_db
    from flrules.models import Alert

    async def _show():
        from sqlalchemy import select

        await init_db()
        async with async_session() as session:
            result = await session.execute(
                select(Alert).order_by(Alert.created_at.desc()).limit(limit)
            )
            rows = result.scalars().all()
            if not rows:
                click.echo("No alerts yet. Run the pipeline first.")
                return
            for a in rows:
                notified = "✓" if a.notified else "·"
                click.echo(
                    f"  [{notified}] score={a.relevance_score:.1f}  "
                    f"cat={a.category}  "
                    f"notice={a.notice_id}"
                )
                click.echo(f"      {a.summary[:100]}")

    asyncio.run(_show())


@cli.command()
def scrape_test():
    """Quick test — scrape latest issue and show matches (no DB, no notifications)."""
    from flrules.relevance import filter_notices
    from flrules.scraper import fetch_latest_issue_ids, scrape_issue

    async def _test():
        issues = await fetch_latest_issue_ids(count=1)
        if not issues:
            click.echo("No issues found.")
            return
        iid = issues[0]["iid"]
        click.echo(f"Scraping issue IID={iid} ({issues[0]['text']})...")
        issue = await scrape_issue(iid, fetch_full_text=True)
        click.echo(f"Found {len(issue.notices)} notices across monitored sections.\n")

        matches = filter_notices(issue.notices)
        if not matches:
            click.echo("No relevant notices found in this issue.")
        else:
            click.echo(f"── {len(matches)} Relevant Notices ──")
            for notice, result in matches:
                click.echo(f"\n  Notice {notice.notice_id} (Section {notice.section_number})")
                click.echo(f"  Agency: {notice.agency_code}")
                click.echo(f"  Description: {notice.description[:120]}")
                click.echo(f"  Score: {result.score:.1f}  Categories: {result.categories}")
                click.echo(f"  URL: {notice.url}")

    asyncio.run(_test())


@cli.command()
def init_database():
    """Initialize the database (create tables)."""
    from flrules.db import init_db

    asyncio.run(init_db())
    click.echo("Database initialized.")


@cli.command()
def build_site():
    """Generate the static dashboard (site/index.html) for GitHub Pages."""
    from flrules.static_site import generate_static_site

    asyncio.run(generate_static_site())
    click.echo("Static site generated in site/")


if __name__ == "__main__":
    cli()
