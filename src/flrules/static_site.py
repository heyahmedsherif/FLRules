"""
Static site generator — builds an index.html dashboard for GitHub Pages.

After each pipeline run, call generate_static_site() to produce a self-contained
HTML file in the `site/` directory. GitHub Actions deploys this to Pages.
"""

import json
from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy import func, select

from flrules.db import async_session, init_db
from flrules.models import Alert, FARIssue, FARNotice, Subscriber

log = structlog.get_logger()

SITE_DIR = Path(__file__).resolve().parent.parent.parent / "site"


async def generate_static_site():
    """Query the DB and write a static HTML dashboard to site/index.html."""
    await init_db()
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_session() as session:
        alert_count = await session.scalar(select(func.count(Alert.id))) or 0
        notice_count = await session.scalar(select(func.count(FARNotice.id))) or 0
        issue_count = await session.scalar(select(func.count(FARIssue.id))) or 0
        sub_count = await session.scalar(select(func.count(Subscriber.id))) or 0

        result = await session.execute(
            select(Alert).order_by(Alert.created_at.desc()).limit(50)
        )
        alerts = result.scalars().all()

        result = await session.execute(
            select(FARNotice).order_by(FARNotice.fetched_at.desc()).limit(30)
        )
        recent_notices = result.scalars().all()

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Build alert rows
    alert_rows = ""
    for a in alerts:
        ts = a.created_at.strftime("%Y-%m-%d %H:%M") if isinstance(a.created_at, datetime) else str(a.created_at)
        cats = " ".join(
            f'<span class="badge badge-{c.strip()}">{c.strip().replace("_", " ")}</span>'
            for c in a.category.split(",")
        )
        notified = "&#10003;" if a.notified else "&middot;"
        alert_rows += f"""<tr>
<td>{ts}</td>
<td>{cats}</td>
<td>{a.relevance_score:.1f}</td>
<td class="summary">{_esc(a.summary[:200])}</td>
<td class="center">{notified}</td>
</tr>
"""

    # Build recent notices rows
    notice_rows = ""
    for n in recent_notices:
        notice_rows += f"""<tr>
<td>{_esc(n.publish_date)}</td>
<td>{_esc(n.section_name)}</td>
<td>{_esc(n.agency_code)}</td>
<td><a href="{_esc(n.url)}" target="_blank">{_esc(n.description[:120])}</a></td>
</tr>
"""

    # Also dump alerts as JSON for programmatic access
    alerts_json = json.dumps(
        [
            {
                "notice_id": a.notice_id,
                "category": a.category,
                "score": a.relevance_score,
                "summary": a.summary,
                "created_at": str(a.created_at),
            }
            for a in alerts
        ],
        indent=2,
    )
    (SITE_DIR / "alerts.json").write_text(alerts_json)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLRules Monitor — FL Administrative Register Alerts</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{
  font-family:system-ui,-apple-system,sans-serif;
  background:#f4f5f7;color:#1a1a2e;
  padding:1.5rem;max-width:1200px;margin:0 auto;
}}
h1{{font-size:1.6rem;margin-bottom:.25rem}}
.subtitle{{color:#64748b;margin-bottom:1.5rem;font-size:.9rem}}
.updated{{color:#94a3b8;font-size:.75rem;margin-bottom:1.5rem}}
.stats{{display:flex;gap:.75rem;margin-bottom:1.5rem;flex-wrap:wrap}}
.stat{{
  background:#fff;border-radius:8px;padding:1rem 1.25rem;
  min-width:130px;box-shadow:0 1px 3px rgba(0,0,0,.08);
}}
.stat .num{{font-size:1.8rem;font-weight:700;color:#2563eb}}
.stat .lbl{{color:#64748b;font-size:.8rem}}
h2{{font-size:1.1rem;margin:1.5rem 0 .75rem;color:#334155}}
table{{
  width:100%;border-collapse:collapse;background:#fff;
  border-radius:8px;overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,.08);
  margin-bottom:2rem;
}}
th{{
  background:#e2e8f0;padding:.6rem .75rem;text-align:left;
  font-size:.75rem;text-transform:uppercase;color:#475569;
  letter-spacing:.03em;
}}
td{{
  padding:.6rem .75rem;border-top:1px solid #e2e8f0;
  font-size:.85rem;vertical-align:top;
}}
td.summary{{max-width:400px}}
td.center{{text-align:center}}
a{{color:#2563eb;text-decoration:none}}
a:hover{{text-decoration:underline}}
.badge{{
  display:inline-block;padding:2px 7px;border-radius:4px;
  font-size:.7rem;font-weight:600;color:#fff;margin:1px;
}}
.badge-domestic_terrorism{{background:#dc2626}}
.badge-cabinet_meeting{{background:#d97706}}
.badge-religious_freedom{{background:#7c3aed}}
.badge-immigration{{background:#0891b2}}
.badge-surveillance{{background:#be185d}}
.badge-civil_rights{{background:#059669}}
.badge-education{{background:#4f46e5}}
.badge-nonprofit_regulation{{background:#64748b}}
.badge-policy_general{{background:#94a3b8}}
.empty{{text-align:center;padding:2rem;color:#94a3b8}}
footer{{
  margin-top:2rem;padding-top:1rem;
  border-top:1px solid #e2e8f0;
  font-size:.75rem;color:#94a3b8;
}}
</style>
</head>
<body>
<h1>FLRules Monitor</h1>
<p class="subtitle">Florida Administrative Register &mdash; Civil Rights Alert Dashboard</p>
<p class="updated">Last updated: {now}</p>

<div class="stats">
<div class="stat"><div class="num">{alert_count}</div><div class="lbl">Alerts</div></div>
<div class="stat"><div class="num">{notice_count}</div><div class="lbl">Notices</div></div>
<div class="stat"><div class="num">{issue_count}</div><div class="lbl">Issues</div></div>
<div class="stat"><div class="num">{sub_count}</div><div class="lbl">Subscribers</div></div>
</div>

<h2>Flagged Alerts</h2>
<table>
<thead><tr>
<th>Date</th><th>Category</th><th>Score</th><th>Summary</th><th>Sent</th>
</tr></thead>
<tbody>
{alert_rows if alert_rows else '<tr><td colspan="5" class="empty">No alerts yet — the system is monitoring.</td></tr>'}
</tbody>
</table>

<h2>Recent Notices Scanned</h2>
<table>
<thead><tr>
<th>Published</th><th>Section</th><th>Agency</th><th>Description</th>
</tr></thead>
<tbody>
{notice_rows if notice_rows else '<tr><td colspan="4" class="empty">No notices scanned yet.</td></tr>'}
</tbody>
</table>

<footer>
Data from the <a href="https://flrules.org/bigdoc/default.asp">Florida Administrative Register</a>.
Alerts are generated automatically based on keyword relevance scoring.
<br>Machine-readable data: <a href="alerts.json">alerts.json</a>
</footer>
</body>
</html>"""

    (SITE_DIR / "index.html").write_text(html)
    log.info("static_site_generated", path=str(SITE_DIR / "index.html"))


def _esc(text: str) -> str:
    """Minimal HTML escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
