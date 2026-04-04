"""
FastAPI web dashboard — admin interface for viewing alerts, managing subscribers,
and tuning keyword rules.
"""

import asyncio
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flrules.config import settings
from flrules.db import get_session, init_db
from flrules.models import Alert, FARIssue, FARNotice, Subscriber

app = FastAPI(title="FLRules Monitor", version="0.1.0")


@app.on_event("startup")
async def startup():
    await init_db()


# ── Pydantic schemas ────────────────────────────────────────────

class SubscriberCreate(BaseModel):
    email: str | None = None
    phone: str | None = None
    name: str = ""
    categories: str = "all"
    notify_email: bool = True
    notify_sms: bool = False


class PipelineRunRequest(BaseModel):
    issue_count: int = 3
    notify: bool = False


# ── API routes ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(session: AsyncSession = Depends(get_session)):
    """Minimal HTML dashboard — shows recent alerts and stats."""
    alert_count = await session.scalar(select(func.count(Alert.id)))
    notice_count = await session.scalar(select(func.count(FARNotice.id)))
    issue_count = await session.scalar(select(func.count(FARIssue.id)))
    sub_count = await session.scalar(select(func.count(Subscriber.id)))

    result = await session.execute(
        select(Alert).order_by(Alert.created_at.desc()).limit(25)
    )
    recent_alerts = result.scalars().all()

    alert_rows = ""
    for a in recent_alerts:
        notified = "&#10003;" if a.notified else "&middot;"
        alert_rows += f"""
        <tr>
            <td>{a.created_at.strftime('%Y-%m-%d %H:%M') if isinstance(a.created_at, datetime) else a.created_at}</td>
            <td><span class="badge">{a.category}</span></td>
            <td>{a.relevance_score:.1f}</td>
            <td>{a.summary[:150]}</td>
            <td>{notified}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FLRules Monitor</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background:#f8f9fa; color:#212529; padding:2rem; }}
  h1 {{ margin-bottom:0.5rem; }}
  .subtitle {{ color:#6c757d; margin-bottom:2rem; }}
  .stats {{ display:flex; gap:1rem; margin-bottom:2rem; flex-wrap:wrap; }}
  .stat {{ background:white; border-radius:8px; padding:1.25rem; min-width:150px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
  .stat .number {{ font-size:2rem; font-weight:700; color:#0d6efd; }}
  .stat .label {{ color:#6c757d; font-size:0.875rem; }}
  table {{ width:100%; border-collapse:collapse; background:white; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
  th {{ background:#e9ecef; padding:0.75rem 1rem; text-align:left; font-size:0.875rem; text-transform:uppercase; color:#495057; }}
  td {{ padding:0.75rem 1rem; border-top:1px solid #dee2e6; font-size:0.875rem; }}
  .badge {{ background:#0d6efd; color:white; padding:2px 8px; border-radius:4px; font-size:0.75rem; }}
  .actions {{ margin:1.5rem 0; }}
  .btn {{ display:inline-block; padding:0.5rem 1rem; background:#0d6efd; color:white; border:none; border-radius:6px; cursor:pointer; text-decoration:none; font-size:0.875rem; }}
  .btn:hover {{ background:#0b5ed7; }}
  .btn-outline {{ background:transparent; border:1px solid #0d6efd; color:#0d6efd; }}
</style>
</head>
<body>
  <h1>FLRules Monitor</h1>
  <p class="subtitle">Florida Administrative Register &mdash; Civil Rights Alert System</p>

  <div class="stats">
    <div class="stat"><div class="number">{alert_count}</div><div class="label">Alerts</div></div>
    <div class="stat"><div class="number">{notice_count}</div><div class="label">Notices Tracked</div></div>
    <div class="stat"><div class="number">{issue_count}</div><div class="label">Issues Scanned</div></div>
    <div class="stat"><div class="number">{sub_count}</div><div class="label">Subscribers</div></div>
  </div>

  <div class="actions">
    <a class="btn" href="/api/run?issue_count=3&notify=false" onclick="this.textContent='Running...'"
       >Run Pipeline (dry)</a>
    <a class="btn btn-outline" href="/api/subscribers">View Subscribers</a>
  </div>

  <h2 style="margin:1rem 0">Recent Alerts</h2>
  <table>
    <thead><tr><th>Date</th><th>Category</th><th>Score</th><th>Summary</th><th>Sent</th></tr></thead>
    <tbody>
      {alert_rows if alert_rows else '<tr><td colspan="5" style="text-align:center;padding:2rem;color:#6c757d;">No alerts yet. Run the pipeline to start monitoring.</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/api/subscribers", response_model=dict)
async def create_subscriber(
    body: SubscriberCreate, session: AsyncSession = Depends(get_session)
):
    import secrets as _secrets

    sub = Subscriber(
        email=body.email,
        phone=body.phone,
        name=body.name,
        categories=body.categories,
        notify_email=body.notify_email,
        notify_sms=body.notify_sms,
        unsubscribe_token=_secrets.token_urlsafe(16),
    )
    session.add(sub)
    await session.commit()
    await session.refresh(sub)
    return {"id": sub.id, "email": sub.email, "status": "subscribed"}


@app.get("/api/subscribers")
async def list_subscribers(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Subscriber).order_by(Subscriber.created_at.desc()))
    subs = result.scalars().all()
    return [
        {"id": s.id, "email": s.email, "name": s.name, "active": s.active, "categories": s.categories}
        for s in subs
    ]


@app.get("/api/alerts")
async def list_alerts(
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Alert).order_by(Alert.created_at.desc()).limit(limit)
    )
    return [
        {
            "id": a.id,
            "notice_id": a.notice_id,
            "category": a.category,
            "score": a.relevance_score,
            "summary": a.summary,
            "notified": a.notified,
            "created_at": str(a.created_at),
        }
        for a in result.scalars().all()
    ]


@app.get("/api/run")
async def trigger_pipeline(
    issue_count: int = Query(3, le=10),
    notify: bool = Query(False),
):
    """Trigger a pipeline run via the API."""
    from flrules.pipeline import run_pipeline

    stats = await run_pipeline(issue_count=issue_count, notify=notify)
    return {"status": "completed", "stats": stats}


@app.delete("/api/subscribers/{subscriber_id}")
async def deactivate_subscriber(
    subscriber_id: int, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.id == subscriber_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "Subscriber not found")
    sub.active = False
    await session.commit()
    return {"id": sub.id, "status": "deactivated"}
