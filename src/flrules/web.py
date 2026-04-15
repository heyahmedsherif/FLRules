"""
FastAPI web app with Google OAuth login, role-based access, and dashboard.

Routes:
  /                  → Dashboard (requires login)
  /auth/login        → Google OAuth login page
  /auth/callback     → Google OAuth callback
  /auth/logout       → Logout
  /my/alerts         → Member's alert feed
  /my/settings       → Member subscription settings
  /admin/subscribers → Admin: manage subscribers
  /admin/run         → Admin: trigger pipeline
  /api/*             → JSON API endpoints
"""

import secrets as _secrets
from datetime import datetime

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from flrules.auth import (
    get_current_user,
    handle_callback,
    handle_login,
    handle_logout,
    require_admin,
    require_login,
)
from flrules.config import settings
from flrules.db import get_session, init_db
from flrules.models import Alert, FARIssue, FARNotice, Subscriber, User

app = FastAPI(title="FLRules Monitor", version="0.2.0")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)


@app.on_event("startup")
async def startup():
    await init_db()


# ── Auth routes ─────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=302)

    html = _page("Sign In", """
<div class="login-container">
  <div class="login-card">
    <div class="login-logo">
      <svg width="48" height="48" viewBox="0 0 36 36" fill="none">
        <rect x="4" y="2" width="20" height="28" rx="3" fill="#1e40af" opacity="0.15"/>
        <rect x="6" y="4" width="20" height="28" rx="3" fill="#2563eb" opacity="0.3"/>
        <rect x="8" y="6" width="20" height="28" rx="3" fill="white"/>
        <circle cx="27" cy="27" r="9" fill="#2563eb"/>
        <path d="M27 21.5c-3 0-5.5 1.5-5.5 1.5s0 5 1.5 7c1.5 2 4 3 4 3s2.5-1 4-3c1.5-2 1.5-7 1.5-7s-2.5-1.5-5.5-1.5z" fill="white" opacity="0.95"/>
        <path d="M25 27l1.5 1.5 3-3" stroke="#2563eb" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
    <h1>FLRules Monitor</h1>
    <p class="login-subtitle">Florida Administrative Register<br>Civil Rights Alert System</p>
    <a href="/auth/google" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#34A853" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#FBBC05" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
      Sign in with Google
    </a>
    <p class="login-footer">Access is limited to authorized organization members.</p>
  </div>
</div>
""", logged_in=False)
    return HTMLResponse(content=html)


@app.get("/auth/google")
async def google_login(request: Request):
    return await handle_login(request)


@app.get("/auth/callback")
async def google_callback(request: Request):
    return await handle_callback(request)


@app.get("/auth/logout")
async def logout(request: Request):
    return await handle_logout(request)


# ── Dashboard (requires login) ──────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    alert_count = await session.scalar(select(func.count(Alert.id))) or 0
    notice_count = await session.scalar(select(func.count(FARNotice.id))) or 0
    issue_count = await session.scalar(select(func.count(FARIssue.id))) or 0
    sub_count = await session.scalar(select(func.count(Subscriber.id))) or 0

    result = await session.execute(
        select(Alert).order_by(Alert.created_at.desc()).limit(25)
    )
    alerts = result.scalars().all()

    alert_rows = ""
    for a in alerts:
        ts = a.created_at.strftime("%Y-%m-%d %H:%M") if isinstance(a.created_at, datetime) else str(a.created_at)
        notified = "&#10003;" if a.notified else "&middot;"
        alert_rows += f"""<tr>
<td>{ts}</td>
<td><span class="badge">{a.category}</span></td>
<td>{a.relevance_score:.1f}</td>
<td>{_esc(a.summary[:150])}</td>
<td class="center">{notified}</td>
</tr>"""

    admin_actions = ""
    if user.role == "admin":
        admin_actions = """
<div class="actions">
  <a class="btn" href="/admin/run?issue_count=3&notify=false" onclick="this.textContent='Running...'">Run Pipeline (dry)</a>
  <a class="btn btn-outline" href="/admin/subscribers">Manage Subscribers</a>
</div>"""

    # Get recent notices for the dashboard
    result = await session.execute(
        select(FARNotice).order_by(FARNotice.fetched_at.desc()).limit(15)
    )
    recent_notices = result.scalars().all()

    notice_rows = ""
    for n in recent_notices:
        section_badge = f'<span class="badge" style="background:#6366f1">{_esc(n.section_name[:25])}</span>'
        notice_rows += f"""<tr>
<td class="date-cell">{_esc(n.publish_date)}</td>
<td>{section_badge}</td>
<td style="font-family:monospace;font-size:0.8rem">{_esc(n.agency_code)}</td>
<td><a href="/notices/{n.notice_id}" title="View details">{_esc(n.description[:100])}</a></td>
<td><a href="{_esc(n.url)}" target="_blank" rel="noopener" style="font-size:0.75rem">FAR&nbsp;&#8599;</a></td>
</tr>"""

    # Get issues for the dashboard
    result = await session.execute(
        select(FARIssue).order_by(FARIssue.fetched_at.desc()).limit(10)
    )
    recent_issues = result.scalars().all()

    issue_rows = ""
    for i in recent_issues:
        issue_rows += f"""<tr>
<td>{_esc(i.publish_date)}</td>
<td>{_esc(i.volume)}</td>
<td><a href="/issues/{i.iid}">View notices</a></td>
</tr>"""

    content = f"""
<div class="stats">
  <div class="stat"><a href="#alerts" style="text-decoration:none;color:inherit"><div class="num">{alert_count}</div><div class="lbl">Alerts</div></a></div>
  <div class="stat"><a href="#notices" style="text-decoration:none;color:inherit"><div class="num">{notice_count}</div><div class="lbl">Notices</div></a></div>
  <div class="stat"><a href="#issues" style="text-decoration:none;color:inherit"><div class="num">{issue_count}</div><div class="lbl">Issues</div></a></div>
  <div class="stat"><div class="num">{sub_count}</div><div class="lbl">Subscribers</div></div>
</div>
{admin_actions}
<div class="section-header" id="alerts">
  <h2>Recent Alerts</h2>
</div>
<div class="table-wrap">
<table>
  <thead><tr><th>Date</th><th>Category</th><th>Score</th><th>Summary</th><th>Sent</th></tr></thead>
  <tbody>
    {alert_rows if alert_rows else '<tr><td colspan="5" class="empty">No alerts yet. The system is monitoring.</td></tr>'}
  </tbody>
</table>
</div>

<div class="section-header" id="notices">
  <h2>Recent Notices</h2>
  <span style="font-size:0.8rem;color:#94a3b8">{notice_count} total &middot; <a href="/notices">View all</a></span>
</div>
<div class="table-wrap">
<table>
  <thead><tr><th>Published</th><th>Section</th><th>Agency</th><th>Description</th><th>Source</th></tr></thead>
  <tbody>
    {notice_rows if notice_rows else '<tr><td colspan="5" class="empty">No notices scanned yet.</td></tr>'}
  </tbody>
</table>
</div>

<div class="section-header" id="issues">
  <h2>Scanned Issues</h2>
</div>
<div class="table-wrap">
<table>
  <thead><tr><th>Published</th><th>Volume</th><th>Details</th></tr></thead>
  <tbody>
    {issue_rows if issue_rows else '<tr><td colspan="3" class="empty">No issues scanned yet.</td></tr>'}
  </tbody>
</table>
</div>
"""
    return HTMLResponse(content=_page("Dashboard", content, user=user))


# ── Detail pages ─────────────────────────────────────

@app.get("/notices", response_class=HTMLResponse)
async def notices_list(
    request: Request,
    page: int = Query(1, ge=1),
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    per_page = 50
    offset = (page - 1) * per_page
    total = await session.scalar(select(func.count(FARNotice.id))) or 0

    result = await session.execute(
        select(FARNotice).order_by(FARNotice.fetched_at.desc()).offset(offset).limit(per_page)
    )
    notices = result.scalars().all()

    rows = ""
    for n in notices:
        section_badge = f'<span class="badge" style="background:#6366f1">{_esc(n.section_name[:25])}</span>'
        rows += f"""<tr>
<td class="date-cell">{_esc(n.publish_date)}</td>
<td>{section_badge}</td>
<td style="font-family:monospace;font-size:0.8rem">{_esc(n.agency_code)}</td>
<td><a href="/notices/{n.notice_id}">{_esc(n.description[:120])}</a></td>
<td><a href="{_esc(n.url)}" target="_blank" rel="noopener" style="font-size:0.75rem">FAR&nbsp;&#8599;</a></td>
</tr>"""

    total_pages = max(1, (total + per_page - 1) // per_page)
    pagination = '<div style="margin-top:1rem;display:flex;gap:0.5rem;align-items:center">'
    if page > 1:
        pagination += f'<a class="btn btn-outline" href="/notices?page={page - 1}">&larr; Previous</a>'
    pagination += f'<span style="color:#94a3b8;font-size:0.85rem">Page {page} of {total_pages} ({total} total)</span>'
    if page < total_pages:
        pagination += f'<a class="btn btn-outline" href="/notices?page={page + 1}">Next &rarr;</a>'
    pagination += '</div>'

    content = f"""
<div class="section-header">
  <h2>All Notices</h2>
  <a class="btn btn-outline" href="/" style="font-size:0.8rem">&larr; Dashboard</a>
</div>
<div class="table-wrap">
<table>
  <thead><tr><th>Published</th><th>Section</th><th>Agency</th><th>Description</th><th>Source</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
{pagination}
"""
    return HTMLResponse(content=_page("All Notices", content, user=user))


@app.get("/notices/{notice_id}", response_class=HTMLResponse)
async def notice_detail(
    request: Request,
    notice_id: int,
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(FARNotice).where(FARNotice.notice_id == notice_id)
    )
    notice = result.scalar_one_or_none()
    if not notice:
        return HTMLResponse(content=_page("Not Found", '<div class="empty-state"><h3>Notice not found</h3><a class="btn" href="/">Dashboard</a></div>', user=user), status_code=404)

    # Check if this notice triggered any alerts
    result = await session.execute(
        select(Alert).where(Alert.notice_id == notice_id)
    )
    alert = result.scalar_one_or_none()

    alert_html = ""
    if alert:
        alert_html = f"""
<div style="background:#fef2f2;border:1px solid #ef4444;border-radius:8px;padding:1rem;margin-bottom:1rem">
  <strong style="color:#dc2626">Alert triggered</strong> (score: {alert.relevance_score:.1f})<br>
  <span style="font-size:0.85rem;color:#7f1d1d">{_esc(alert.summary)}</span>
</div>"""

    # Format the full text with line breaks
    full_text = _esc(notice.full_text).replace("\n", "<br>") if notice.full_text else "<em style='color:#94a3b8'>Full text not available</em>"

    content = f"""
<div class="section-header">
  <h2>Notice Detail</h2>
  <a class="btn btn-outline" href="javascript:history.back()" style="font-size:0.8rem">&larr; Back</a>
</div>
{alert_html}
<div class="card" style="max-width:100%">
  <div style="display:grid;grid-template-columns:120px 1fr;gap:0.5rem 1rem;margin-bottom:1rem">
    <span style="font-weight:600;color:#64748b;font-size:0.8rem">Notice ID</span>
    <span>{notice.notice_id}</span>
    <span style="font-weight:600;color:#64748b;font-size:0.8rem">Published</span>
    <span>{_esc(notice.publish_date)}</span>
    <span style="font-weight:600;color:#64748b;font-size:0.8rem">Section</span>
    <span><span class="badge" style="background:#6366f1">{_esc(notice.section_name)}</span></span>
    <span style="font-weight:600;color:#64748b;font-size:0.8rem">Agency</span>
    <span style="font-family:monospace">{_esc(notice.agency_code)}</span>
    <span style="font-weight:600;color:#64748b;font-size:0.8rem">Description</span>
    <span>{_esc(notice.description)}</span>
    <span style="font-weight:600;color:#64748b;font-size:0.8rem">Source</span>
    <span><a href="{_esc(notice.url)}" target="_blank" rel="noopener">View on flrules.org &#8599;</a></span>
  </div>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:1rem 0">
  <h3 style="font-size:0.9rem;margin-bottom:0.75rem">Full Text</h3>
  <div style="font-size:0.85rem;color:#475569;line-height:1.7;max-height:600px;overflow-y:auto;padding-right:0.5rem">
    {full_text}
  </div>
</div>
"""
    return HTMLResponse(content=_page(f"Notice {notice_id}", content, user=user))


@app.get("/issues/{iid}", response_class=HTMLResponse)
async def issue_detail(
    request: Request,
    iid: int,
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(FARIssue).where(FARIssue.iid == iid))
    issue = result.scalar_one_or_none()
    if not issue:
        return HTMLResponse(content=_page("Not Found", '<div class="empty-state"><h3>Issue not found</h3><a class="btn" href="/">Dashboard</a></div>', user=user), status_code=404)

    result = await session.execute(
        select(FARNotice).where(FARNotice.issue_iid == iid).order_by(FARNotice.section_number)
    )
    notices = result.scalars().all()

    rows = ""
    for n in notices:
        section_badge = f'<span class="badge" style="background:#6366f1">{_esc(n.section_name[:25])}</span>'
        rows += f"""<tr>
<td>{section_badge}</td>
<td style="font-family:monospace;font-size:0.8rem">{_esc(n.agency_code)}</td>
<td><a href="/notices/{n.notice_id}">{_esc(n.description[:120])}</a></td>
<td><a href="{_esc(n.url)}" target="_blank" rel="noopener" style="font-size:0.75rem">FAR&nbsp;&#8599;</a></td>
</tr>"""

    content = f"""
<div class="section-header">
  <h2>FAR Issue — {_esc(issue.publish_date)}</h2>
  <a class="btn btn-outline" href="/" style="font-size:0.8rem">&larr; Dashboard</a>
</div>
<div class="card" style="max-width:100%;margin-bottom:1rem">
  <div style="display:flex;gap:2rem;font-size:0.875rem">
    <div><span style="color:#64748b">Volume:</span> {_esc(issue.volume or 'N/A')}</div>
    <div><span style="color:#64748b">Published:</span> {_esc(issue.publish_date)}</div>
    <div><span style="color:#64748b">Notices:</span> {len(notices)}</div>
    <div><span style="color:#64748b">Issue ID:</span> {iid}</div>
  </div>
</div>
<div class="table-wrap">
<table>
  <thead><tr><th>Section</th><th>Agency</th><th>Description</th><th>Source</th></tr></thead>
  <tbody>
    {rows if rows else '<tr><td colspan="4" class="empty">No notices found for this issue.</td></tr>'}
  </tbody>
</table>
</div>
"""
    return HTMLResponse(content=_page(f"Issue {issue.publish_date}", content, user=user))


# ── Member pages ─────────────────────────────────────

@app.get("/my/settings", response_class=HTMLResponse)
async def my_settings(
    request: Request,
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.email == user.email)
    )
    sub = result.scalar_one_or_none()

    if sub:
        status_html = f"""
<div class="card">
  <h3>Your Subscription</h3>
  <p>Status: <span class="badge" style="background:#10b981">Active</span></p>
  <p>Email notifications: {"On" if sub.notify_email else "Off"}</p>
  <p>SMS notifications: {"On" if sub.notify_sms else "Off"}</p>
  <p>Phone: {sub.phone or "Not set"}</p>
  <p>Categories: {sub.categories}</p>
  <form method="post" action="/my/unsubscribe" style="margin-top:1rem">
    <button type="submit" class="btn btn-danger">Unsubscribe</button>
  </form>
</div>"""
    else:
        status_html = """
<div class="card">
  <h3>Not Subscribed</h3>
  <p>You are not currently receiving alerts.</p>
  <form method="post" action="/my/subscribe">
    <div class="form-group">
      <label>Phone (for SMS alerts, optional)</label>
      <input type="tel" name="phone" placeholder="+1XXXXXXXXXX" class="input">
    </div>
    <div class="form-group">
      <label>Categories</label>
      <select name="categories" class="input">
        <option value="all">All categories</option>
        <option value="domestic_terrorism">Domestic Terrorism</option>
        <option value="religious_freedom">Religious Freedom</option>
        <option value="immigration">Immigration</option>
        <option value="civil_rights">Civil Rights</option>
        <option value="surveillance">Surveillance</option>
        <option value="cabinet_meeting">Cabinet Meetings</option>
        <option value="education">Education</option>
      </select>
    </div>
    <button type="submit" class="btn">Subscribe to Alerts</button>
  </form>
</div>"""

    return HTMLResponse(content=_page("My Settings", status_html, user=user))


@app.post("/my/subscribe")
async def my_subscribe(
    request: Request,
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    phone = form.get("phone", "")
    categories = form.get("categories", "all")

    existing = await session.execute(
        select(Subscriber).where(Subscriber.email == user.email)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url="/my/settings", status_code=302)

    sub = Subscriber(
        email=user.email,
        phone=phone or None,
        name=user.name,
        categories=categories,
        notify_email=True,
        notify_sms=bool(phone),
        unsubscribe_token=_secrets.token_urlsafe(16),
    )
    session.add(sub)
    await session.commit()

    from flrules.notifier import send_opt_in_confirmation, send_opt_in_email

    await send_opt_in_email(user.email, user.name)

    if phone:
        await send_opt_in_confirmation(phone)

    return RedirectResponse(url="/my/settings", status_code=302)


@app.post("/my/unsubscribe")
async def my_unsubscribe(
    request: Request,
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.email == user.email)
    )
    sub = result.scalar_one_or_none()
    if sub:
        sub.active = False
        await session.commit()
    return RedirectResponse(url="/my/settings", status_code=302)


# ── Admin pages ──────────────────────────────────────

@app.get("/admin/subscribers", response_class=HTMLResponse)
async def admin_subscribers(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).order_by(Subscriber.created_at.desc())
    )
    subs = result.scalars().all()

    rows = ""
    for s in subs:
        status = '<span class="badge" style="background:#10b981">Active</span>' if s.active else '<span class="badge" style="background:#94a3b8">Inactive</span>'
        toggle_label = "Deactivate" if s.active else "Activate"
        toggle_color = "#94a3b8" if s.active else "#10b981"
        channels = []
        if s.notify_email:
            channels.append("Email")
        if s.notify_sms:
            channels.append("SMS")
        channel_str = ", ".join(channels) if channels else "None"
        rows += f"""<tr>
<td>{_esc(s.name)}</td>
<td>{_esc(s.email or "")}</td>
<td>{_esc(s.phone or "")}</td>
<td>{status}</td>
<td>{s.categories}</td>
<td>{channel_str}</td>
<td style="white-space:nowrap">
  <a href="/admin/subscribers/{s.id}/edit" class="btn btn-outline" style="padding:2px 8px;font-size:0.75rem">Edit</a>
  <form method="post" action="/admin/subscribers/{s.id}/toggle" style="display:inline">
    <button type="submit" class="btn" style="padding:2px 8px;font-size:0.75rem;background:{toggle_color}">{toggle_label}</button>
  </form>
  <form method="post" action="/admin/subscribers/{s.id}/delete" style="display:inline" onsubmit="return confirm('Delete {_esc(s.name or s.email or "")}?')">
    <button type="submit" class="btn btn-danger" style="padding:2px 8px;font-size:0.75rem">Delete</button>
  </form>
</td>
</tr>"""

    content = f"""
<div class="section-header" style="display:flex;align-items:center;justify-content:space-between">
  <h2>Subscribers ({len(subs)})</h2>
  <a class="btn" href="/admin/subscribers/add">+ Add Subscriber</a>
</div>
<div class="table-wrap">
<table>
  <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Status</th><th>Categories</th><th>Channels</th><th>Actions</th></tr></thead>
  <tbody>
    {rows if rows else '<tr><td colspan="7" class="empty">No subscribers yet.</td></tr>'}
  </tbody>
</table>
</div>
"""
    return HTMLResponse(content=_page("Manage Subscribers", content, user=user))


@app.get("/admin/subscribers/add", response_class=HTMLResponse)
async def admin_add_subscriber_form(
    request: Request,
    user: User = Depends(require_admin),
):
    content = """
<div class="section-header">
  <h2>Add Subscriber</h2>
  <a class="btn btn-outline" href="/admin/subscribers" style="font-size:0.8rem">&larr; Back</a>
</div>
<div class="card">
  <form method="post" action="/admin/subscribers/add">
    <div class="form-group">
      <label>Name</label>
      <input type="text" name="name" class="input" placeholder="Full name">
    </div>
    <div class="form-group">
      <label>Email</label>
      <input type="email" name="email" class="input" placeholder="user@example.com">
    </div>
    <div class="form-group">
      <label>Phone (for SMS)</label>
      <input type="tel" name="phone" class="input" placeholder="+1XXXXXXXXXX">
    </div>
    <div class="form-group">
      <label>Categories</label>
      <select name="categories" class="input">
        <option value="all">All categories</option>
        <option value="domestic_terrorism">Domestic Terrorism</option>
        <option value="religious_freedom">Religious Freedom</option>
        <option value="immigration">Immigration</option>
        <option value="civil_rights">Civil Rights</option>
        <option value="surveillance">Surveillance</option>
        <option value="cabinet_meeting">Cabinet Meetings</option>
        <option value="education">Education</option>
        <option value="nonprofit_regulation">Nonprofit Regulation</option>
      </select>
    </div>
    <div class="form-group" style="display:flex;gap:1.5rem">
      <label><input type="checkbox" name="notify_email" checked> Email alerts</label>
      <label><input type="checkbox" name="notify_sms"> SMS alerts</label>
    </div>
    <button type="submit" class="btn">Add Subscriber</button>
  </form>
</div>
"""
    return HTMLResponse(content=_page("Add Subscriber", content, user=user))


@app.post("/admin/subscribers/add")
async def admin_add_subscriber(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    email = form.get("email", "").strip() or None
    phone = form.get("phone", "").strip() or None
    name = form.get("name", "").strip()
    categories = form.get("categories", "all")
    notify_email = "notify_email" in form
    notify_sms = "notify_sms" in form

    sub = Subscriber(
        email=email,
        phone=phone,
        name=name,
        categories=categories,
        notify_email=notify_email,
        notify_sms=notify_sms,
        unsubscribe_token=_secrets.token_urlsafe(16),
    )
    session.add(sub)
    await session.commit()

    from flrules.notifier import send_opt_in_confirmation, send_opt_in_email

    if email and notify_email:
        await send_opt_in_email(email, name)

    if phone and notify_sms:
        await send_opt_in_confirmation(phone)

    return RedirectResponse(url="/admin/subscribers", status_code=302)


@app.get("/admin/subscribers/{sub_id}/edit", response_class=HTMLResponse)
async def admin_edit_subscriber_form(
    request: Request,
    sub_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Subscriber).where(Subscriber.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        return RedirectResponse(url="/admin/subscribers", status_code=302)

    category_options = ""
    for val, label in [
        ("all", "All categories"),
        ("domestic_terrorism", "Domestic Terrorism"),
        ("religious_freedom", "Religious Freedom"),
        ("immigration", "Immigration"),
        ("civil_rights", "Civil Rights"),
        ("surveillance", "Surveillance"),
        ("cabinet_meeting", "Cabinet Meetings"),
        ("education", "Education"),
        ("nonprofit_regulation", "Nonprofit Regulation"),
    ]:
        selected = "selected" if sub.categories == val else ""
        category_options += f'<option value="{val}" {selected}>{label}</option>'

    content = f"""
<div class="section-header">
  <h2>Edit Subscriber</h2>
  <a class="btn btn-outline" href="/admin/subscribers" style="font-size:0.8rem">&larr; Back</a>
</div>
<div class="card">
  <form method="post" action="/admin/subscribers/{sub.id}/edit">
    <div class="form-group">
      <label>Name</label>
      <input type="text" name="name" class="input" value="{_esc(sub.name)}">
    </div>
    <div class="form-group">
      <label>Email</label>
      <input type="email" name="email" class="input" value="{_esc(sub.email or "")}">
    </div>
    <div class="form-group">
      <label>Phone (for SMS)</label>
      <input type="tel" name="phone" class="input" value="{_esc(sub.phone or "")}">
    </div>
    <div class="form-group">
      <label>Categories</label>
      <select name="categories" class="input">{category_options}</select>
    </div>
    <div class="form-group" style="display:flex;gap:1.5rem">
      <label><input type="checkbox" name="notify_email" {"checked" if sub.notify_email else ""}> Email alerts</label>
      <label><input type="checkbox" name="notify_sms" {"checked" if sub.notify_sms else ""}> SMS alerts</label>
    </div>
    <button type="submit" class="btn">Save Changes</button>
  </form>
</div>
"""
    return HTMLResponse(content=_page("Edit Subscriber", content, user=user))


@app.post("/admin/subscribers/{sub_id}/edit")
async def admin_edit_subscriber(
    request: Request,
    sub_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Subscriber).where(Subscriber.id == sub_id))
    sub = result.scalar_one_or_none()
    if not sub:
        return RedirectResponse(url="/admin/subscribers", status_code=302)

    form = await request.form()
    sub.name = form.get("name", "").strip()
    sub.email = form.get("email", "").strip() or None
    sub.phone = form.get("phone", "").strip() or None
    sub.categories = form.get("categories", "all")
    sub.notify_email = "notify_email" in form
    sub.notify_sms = "notify_sms" in form

    await session.commit()
    return RedirectResponse(url="/admin/subscribers", status_code=302)


@app.post("/admin/subscribers/{sub_id}/toggle")
async def admin_toggle_subscriber(
    request: Request,
    sub_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Subscriber).where(Subscriber.id == sub_id))
    sub = result.scalar_one_or_none()
    if sub:
        sub.active = not sub.active
        await session.commit()
    return RedirectResponse(url="/admin/subscribers", status_code=302)


@app.post("/admin/subscribers/{sub_id}/delete")
async def admin_delete_subscriber(
    request: Request,
    sub_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Subscriber).where(Subscriber.id == sub_id))
    sub = result.scalar_one_or_none()
    if sub:
        await session.delete(sub)
        await session.commit()
    return RedirectResponse(url="/admin/subscribers", status_code=302)


@app.get("/admin/run", response_class=HTMLResponse)
async def admin_run_pipeline(
    request: Request,
    issue_count: int = Query(3, le=10),
    notify: bool = Query(False),
    user: User = Depends(require_admin),
):
    from flrules.pipeline import run_pipeline

    stats = await run_pipeline(issue_count=issue_count, notify=notify)

    stat_rows = ""
    labels = {
        "issues_checked": "Issues Checked",
        "issues_new": "New Issues Found",
        "notices_scraped": "Notices Scraped",
        "notices_new": "New Notices Stored",
        "alerts_generated": "Alerts Generated",
        "notifications_sent": "Notifications Sent",
    }
    for key, label in labels.items():
        val = stats.get(key, 0)
        highlight = ' style="color:#10b981;font-weight:700"' if val > 0 and key in ("alerts_generated", "notifications_sent") else ""
        stat_rows += f"<tr><td>{label}</td><td{highlight}>{val}</td></tr>"

    alert_msg = ""
    if stats.get("alerts_generated", 0) > 0:
        alert_msg = f'<div style="background:#ecfdf5;border:1px solid #10b981;border-radius:8px;padding:1rem;margin-bottom:1rem;color:#065f46"><strong>{stats["alerts_generated"]} alert(s) generated!</strong> Check the dashboard to review.</div>'
    elif stats.get("notices_new", 0) > 0:
        alert_msg = f'<div style="background:#eff6ff;border:1px solid #2563eb;border-radius:8px;padding:1rem;margin-bottom:1rem;color:#1e40af">Scanned {stats["notices_new"]} new notices. No relevant content found this time.</div>'
    else:
        alert_msg = '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:1rem;margin-bottom:1rem;color:#475569">No new issues to process. All recent FAR issues have already been scanned.</div>'

    content = f"""
<div class="section-header"><h2>Pipeline Run Complete</h2></div>
{alert_msg}
<div class="card" style="max-width:400px">
  <h3>Run Summary</h3>
  <table style="width:100%">
    {stat_rows}
  </table>
</div>
<div class="actions" style="margin-top:1.5rem">
  <a class="btn" href="/">Back to Dashboard</a>
  <a class="btn btn-outline" href="/admin/run?issue_count=5&notify=false">Run Again (5 issues)</a>
</div>
"""
    return HTMLResponse(content=_page("Pipeline Results", content, user=user))


# ── JSON API (requires login) ───────────────────────

@app.get("/api/alerts")
async def api_alerts(
    limit: int = Query(50, le=200),
    user: User = Depends(require_login),
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


@app.get("/api/me")
async def api_me(user: User = Depends(require_login)):
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "picture": user.picture,
    }


# ── HTML helpers ─────────────────────────────────────

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _page(title: str, content: str, user: User | None = None, logged_in: bool = True) -> str:
    """Wrap content in the app shell with nav bar."""
    nav_items = ""
    user_menu = ""
    if user:
        nav_items = f"""
<a href="/" class="nav-link">Dashboard</a>
<a href="/my/settings" class="nav-link">My Settings</a>
{"<a href='/admin/subscribers' class='nav-link'>Subscribers</a>" if user.role == "admin" else ""}
"""
        user_menu = f"""
<div class="user-menu">
  <img src="{user.picture}" alt="" class="avatar" referrerpolicy="no-referrer">
  <span class="user-name">{_esc(user.name)}</span>
  {"<span class='role-badge'>Admin</span>" if user.role == "admin" else ""}
  <a href="/auth/logout" class="nav-link logout">Sign out</a>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — FLRules Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0;
  --text: #0f172a; --text-secondary: #475569; --text-muted: #94a3b8;
  --accent: #2563eb; --accent-light: #dbeafe;
  --radius: 12px;
  --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}}
.navbar {{
  background: linear-gradient(135deg, #1e293b, #0f172a);
  color: white; padding: 0 2rem; height: 56px;
  display: flex; align-items: center; justify-content: space-between;
}}
.nav-brand {{
  font-weight: 700; font-size: 1.1rem;
  display: flex; align-items: center; gap: 0.5rem;
  text-decoration: none; color: white;
}}
.nav-links {{ display: flex; align-items: center; gap: 0.25rem; }}
.nav-link {{
  color: #94a3b8; text-decoration: none; font-size: 0.85rem;
  padding: 0.4rem 0.75rem; border-radius: 6px; font-weight: 500;
  transition: background 0.15s, color 0.15s;
}}
.nav-link:hover {{ background: rgba(255,255,255,0.1); color: white; }}
.user-menu {{
  display: flex; align-items: center; gap: 0.5rem;
}}
.avatar {{
  width: 28px; height: 28px; border-radius: 50%;
  border: 2px solid rgba(255,255,255,0.2);
}}
.user-name {{ font-size: 0.8rem; color: #94a3b8; }}
.role-badge {{
  font-size: 0.65rem; background: #2563eb; color: white;
  padding: 1px 6px; border-radius: 999px; font-weight: 600;
}}
.logout {{ color: #64748b !important; font-size: 0.8rem; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 1.5rem 2rem; }}
.stats {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1rem; margin-bottom: 2rem;
}}
.stat {{
  background: var(--surface); border-radius: var(--radius);
  padding: 1.25rem; box-shadow: var(--shadow); border: 1px solid var(--border);
}}
.stat .num {{ font-size: 1.75rem; font-weight: 700; color: var(--accent); }}
.stat .lbl {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }}
.section-header {{ margin: 2rem 0 1rem; }}
.section-header h2 {{ font-size: 1.1rem; font-weight: 600; }}
.table-wrap {{
  background: var(--surface); border-radius: var(--radius);
  box-shadow: var(--shadow); border: 1px solid var(--border); overflow: hidden;
}}
table {{ width: 100%; border-collapse: collapse; }}
th {{
  background: #f1f5f9; padding: 0.7rem 1rem; text-align: left;
  font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--text-secondary);
}}
td {{ padding: 0.7rem 1rem; font-size: 0.85rem; border-top: 1px solid var(--border); }}
tr:hover td {{ background: #f8fafc; }}
.center {{ text-align: center; }}
.empty {{ text-align: center; padding: 2rem; color: var(--text-muted); }}
.badge {{
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 0.7rem; font-weight: 600; color: white; background: var(--accent);
}}
.actions {{ display: flex; gap: 0.75rem; margin-bottom: 1.5rem; flex-wrap: wrap; }}
.btn {{
  display: inline-block; padding: 0.5rem 1rem; background: var(--accent); color: white;
  border: none; border-radius: 8px; cursor: pointer; text-decoration: none;
  font-size: 0.85rem; font-weight: 500; font-family: inherit;
}}
.btn:hover {{ background: #1d4ed8; }}
.btn-outline {{ background: transparent; border: 1px solid var(--accent); color: var(--accent); }}
.btn-outline:hover {{ background: var(--accent-light); }}
.btn-danger {{ background: #ef4444; }}
.btn-danger:hover {{ background: #dc2626; }}
.card {{
  background: var(--surface); border-radius: var(--radius);
  padding: 1.5rem; box-shadow: var(--shadow); border: 1px solid var(--border);
  max-width: 500px;
}}
.card h3 {{ font-size: 1rem; margin-bottom: 0.75rem; }}
.card p {{ font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.35rem; }}
.form-group {{ margin-bottom: 1rem; }}
.form-group label {{ display: block; font-size: 0.8rem; font-weight: 500; margin-bottom: 0.25rem; color: var(--text-secondary); }}
.input {{
  width: 100%; padding: 0.5rem 0.75rem; border: 1px solid var(--border);
  border-radius: 8px; font-size: 0.85rem; font-family: inherit;
}}
.input:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }}
/* Login page */
.login-container {{
  min-height: calc(100vh - 56px); display: flex; align-items: center;
  justify-content: center; background: linear-gradient(135deg, #f8fafc, #e2e8f0);
}}
.login-card {{
  background: white; border-radius: 16px; padding: 3rem 2.5rem;
  box-shadow: 0 10px 40px rgba(0,0,0,0.08); text-align: center;
  max-width: 400px; width: 100%;
}}
.login-logo {{ margin-bottom: 1rem; }}
.login-card h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
.login-subtitle {{ color: var(--text-muted); font-size: 0.875rem; margin-bottom: 2rem; line-height: 1.5; }}
.google-btn {{
  display: inline-flex; align-items: center; gap: 0.75rem;
  padding: 0.75rem 1.5rem; background: white; color: var(--text);
  border: 1px solid var(--border); border-radius: 8px;
  font-size: 0.9rem; font-weight: 500; text-decoration: none;
  font-family: inherit; cursor: pointer;
  transition: box-shadow 0.15s, border-color 0.15s;
}}
.google-btn:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,0.1); border-color: #94a3b8; }}
.login-footer {{ margin-top: 1.5rem; font-size: 0.75rem; color: var(--text-muted); }}
@media (max-width: 640px) {{
  .navbar {{ padding: 0 1rem; }}
  .container {{ padding: 1rem; }}
  .user-name {{ display: none; }}
  .login-card {{ margin: 1rem; padding: 2rem 1.5rem; }}
}}
</style>
</head>
<body>
{"" if not logged_in else f'''
<nav class="navbar">
  <div style="display:flex;align-items:center;gap:1.5rem">
    <a href="/" class="nav-brand">FLRules Monitor</a>
    <div class="nav-links">{nav_items}</div>
  </div>
  {user_menu}
</nav>
'''}
<div class="container">
{content}
</div>
</body>
</html>"""
