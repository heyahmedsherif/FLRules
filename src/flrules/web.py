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

import asyncio
import os
import secrets as _secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
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


# ── In-process scheduler ─────────────────────────────
# We schedule the FAR pipeline inside the web process via APScheduler so it
# writes to the same persistent volume as the dashboard. This replaces the
# previous GitHub Actions cron, which had its own ephemeral DB and never
# notified real production subscribers. Disable in dev with SCHEDULER=off.
import logging as _logging  # noqa: E402

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

_scheduler_log = _logging.getLogger("flrules.scheduler")
_scheduler: AsyncIOScheduler | None = None


async def _scheduled_pipeline_run():
    """Run the FAR pipeline once, log stats. Called by APScheduler on schedule."""
    from flrules.pipeline import run_pipeline
    try:
        stats = await run_pipeline(issue_count=5, notify=True)
        _scheduler_log.info("scheduled_pipeline_complete stats=%s", stats)
    except Exception as e:
        _scheduler_log.exception("scheduled_pipeline_failed error=%s", e)


@app.on_event("startup")
async def startup():
    await init_db()

    # Skip scheduler if explicitly disabled (e.g., in tests or one-shot runs)
    if os.environ.get("SCHEDULER", "on").lower() in {"off", "false", "0"}:
        _scheduler_log.info("scheduler_disabled via SCHEDULER env var")
        return

    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="America/New_York")
    # Mon-Fri, every hour from 8am to 6pm ET (matches prior GH Actions schedule)
    _scheduler.add_job(
        _scheduled_pipeline_run,
        CronTrigger(day_of_week="mon-fri", hour="8-18", minute=0),
        id="far_pipeline_business_hours",
        max_instances=1,
        coalesce=True,
    )
    # Sat/Sun at 10am and 6pm ET
    _scheduler.add_job(
        _scheduled_pipeline_run,
        CronTrigger(day_of_week="sat,sun", hour="10,18", minute=0),
        id="far_pipeline_weekend",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    _scheduler_log.info("scheduler_started jobs=%s", [j.id for j in _scheduler.get_jobs()])


@app.on_event("shutdown")
async def shutdown():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler_log.info("scheduler_stopped")


# ── Public pages (no login required) ────────────────

@app.get("/signup", response_class=HTMLResponse)
async def public_signup_page():
    """Public SMS opt-in page — visible to Twilio reviewers and the public."""
    category_picker = _category_checkboxes("all")
    html = _page("Subscribe to Alerts", f"""
<div style="max-width:600px;margin:2rem auto">
  <div class="card" style="max-width:100%">
    <div style="text-align:center;margin-bottom:1.5rem">
      <svg width="48" height="48" viewBox="0 0 36 36" fill="none">
        <rect x="4" y="2" width="20" height="28" rx="3" fill="#1e40af" opacity="0.15"/>
        <rect x="6" y="4" width="20" height="28" rx="3" fill="#2563eb" opacity="0.3"/>
        <rect x="8" y="6" width="20" height="28" rx="3" fill="white"/>
        <circle cx="27" cy="27" r="9" fill="#2563eb"/>
        <path d="M27 21.5c-3 0-5.5 1.5-5.5 1.5s0 5 1.5 7c1.5 2 4 3 4 3s2.5-1 4-3c1.5-2 1.5-7 1.5-7s-2.5-1.5-5.5-1.5z" fill="white" opacity="0.95"/>
        <path d="M25 27l1.5 1.5 3-3" stroke="#2563eb" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <h2 style="margin-top:0.5rem">FL Rules Monitor</h2>
      <p style="color:#64748b;font-size:0.9rem">Florida Administrative Register Alert System</p>
    </div>

    <p style="font-size:0.9rem;color:#475569;line-height:1.6;margin-bottom:1.5rem">
      Subscribe to receive <strong>text message alerts</strong> when the Florida Administrative Register
      publishes rule changes, proposed regulations, emergency rules, or meeting notices relevant to
      civil rights. This is a free public service alert system.
    </p>

    <form method="post" action="/signup">
      <div class="form-group">
        <label>Full Name <span style="color:#ef4444">*</span></label>
        <input type="text" name="name" class="input" placeholder="Your full name" required>
      </div>
      <div class="form-group">
        <label>Email Address <span style="color:#ef4444">*</span></label>
        <input type="email" name="email" class="input" placeholder="you@example.com" required>
      </div>
      <div class="form-group">
        <label>Mobile Phone Number (for SMS alerts)</label>
        <input type="tel" name="phone" class="input" placeholder="+1XXXXXXXXXX">
        <span style="font-size:0.75rem;color:#94a3b8">US numbers only. Include country code (+1).</span>
      </div>
      <div class="form-group">
        <label>Alert Categories</label>
        {category_picker}
      </div>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:1rem;margin:1rem 0">
        <div style="margin-bottom:0.75rem">
          <label style="font-size:0.85rem;display:flex;align-items:flex-start;gap:0.5rem;cursor:pointer">
            <input type="checkbox" name="consent_email" style="margin-top:3px">
            <span>I consent to receive <strong>email alerts</strong> from FL Rules Monitor about Florida Administrative Register updates.</span>
          </label>
        </div>
        <div>
          <label style="font-size:0.85rem;display:flex;align-items:flex-start;gap:0.5rem;cursor:pointer">
            <input type="checkbox" name="consent_sms" style="margin-top:3px">
            <span>I consent to receive <strong>SMS/text message alerts</strong> from FL Rules Monitor (operated by Intuitive Dataframe, LLC) at the mobile number provided above. Message frequency varies (typically a few per week). Message and data rates may apply. Reply STOP to unsubscribe or HELP for help at any time.</span>
          </label>
        </div>
      </div>

      <button type="submit" class="btn" style="width:100%;padding:0.75rem;font-size:1rem">Subscribe to Alerts</button>
    </form>

    <p style="text-align:center;margin-top:1rem;font-size:0.85rem;color:#64748b">
      Already subscribed? <a href="/login" style="color:#2563eb">Manage your preferences &rarr;</a>
    </p>

    <div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0">
      <p style="font-size:0.75rem;color:#94a3b8;line-height:1.5">
        <strong>Program:</strong> FL Rules Monitor by Intuitive Dataframe, LLC<br>
        <strong>Purpose:</strong> Automated alerts when Florida state agencies publish rules, regulations, or meeting notices relevant to civil rights.<br>
        <strong>Message frequency:</strong> Varies; typically a few alerts per week.<br>
        <strong>Message and data rates may apply.</strong><br>
        <strong>To opt out:</strong> Reply STOP to any text message, or click the unsubscribe link in any email.<br>
        <strong>For help:</strong> Reply HELP to any text message, or email contact@gearnerd.io.<br>
        <strong>Privacy:</strong> Your phone number and email are stored securely and never shared with third parties.
        We only use your contact information to deliver alerts from this service.<br>
        <a href="/about" style="color:#2563eb">How It Works</a> &middot; <a href="/privacy" style="color:#2563eb">Privacy Policy</a> &middot; <a href="/terms" style="color:#2563eb">Terms of Service</a>
      </p>
    </div>
  </div>
</div>
""", logged_in=False)
    return HTMLResponse(content=html)


@app.post("/signup")
async def public_signup_submit(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    phone = form.get("phone", "").strip() or None
    categories = _parse_categories_form(form.getlist("categories"))
    consent_email = "consent_email" in form
    consent_sms = "consent_sms" in form

    if not email:
        return RedirectResponse(url="/signup", status_code=302)

    # Check if already subscribed (case-insensitive against any legacy mixed-case rows)
    existing = await session.execute(
        select(Subscriber).where(func.lower(Subscriber.email) == email)
    )
    if existing.scalar_one_or_none():
        return HTMLResponse(content=_page("Already Subscribed", """
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>Already Subscribed</h3>
  <p>This email address is already subscribed to FL Rules Monitor alerts.</p>
  <p style="font-size:0.9rem;color:#475569;margin-top:1rem">Want to change your settings or unsubscribe? We can email you a secure link to manage your preferences.</p>
  <a class="btn" href="/login" style="margin-top:0.5rem">Email me a manage link</a>
  <a class="btn btn-outline" href="/signup" style="margin-left:0.5rem">Back</a>
</div>""", logged_in=False))

    sub = Subscriber(
        email=email,
        phone=phone if consent_sms else None,
        name=name,
        categories=categories,
        notify_email=consent_email,
        notify_sms=consent_sms and bool(phone),
        unsubscribe_token=_secrets.token_urlsafe(16),
    )
    session.add(sub)
    await session.commit()

    from flrules.notifier import send_opt_in_confirmation, send_opt_in_email

    if consent_email:
        asyncio.create_task(send_opt_in_email(email, name, sub.unsubscribe_token))

    if consent_sms and phone:
        asyncio.create_task(send_opt_in_confirmation(phone))

    return HTMLResponse(content=_page("Subscribed", f"""
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>You're Subscribed!</h3>
  <p>Thank you, {_esc(name)}. You will receive FL Rules Monitor alerts at <strong>{_esc(email)}</strong>
  {"and via SMS at <strong>" + _esc(phone) + "</strong>" if consent_sms and phone else ""}.</p>
  <p style="color:#64748b;font-size:0.85rem;margin-top:1rem">A confirmation message has been sent. You can unsubscribe at any time.</p>
</div>""", logged_in=False))


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """Public privacy policy page."""
    return HTMLResponse(content=_page("Privacy Policy", """
<div style="max-width:700px;margin:2rem auto">
  <h2>Privacy Policy</h2>
  <p style="color:#64748b;font-size:0.85rem;margin-bottom:1.5rem">Last updated: April 2026</p>

  <div style="font-size:0.9rem;color:#475569;line-height:1.7">
    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">What We Collect</h3>
    <p>When you subscribe to FL Rules Monitor, we collect your name, email address, and optionally your
    mobile phone number. This information is used solely to deliver alerts about the Florida Administrative Register.</p>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">How We Use Your Information</h3>
    <p>We use your contact information exclusively to send you alerts about Florida Administrative Register
    publications that match your selected categories. We do not use your information for marketing,
    advertising, or any other purpose.</p>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Data Sharing</h3>
    <p>We do not sell, rent, or share your personal information with any third parties for marketing
    or advertising purposes. Your contact details are only used by our service delivery infrastructure
    to send your alerts. We do not share your opt-in consent or phone number with any affiliates,
    partners, or other organizations.</p>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Data Storage</h3>
    <p>Your information is stored securely in our database. We use industry-standard security practices
    to protect your data.</p>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Opting Out</h3>
    <p>You can unsubscribe at any time by:</p>
    <ul style="margin:0.5rem 0 0.5rem 1.5rem">
      <li>Clicking the unsubscribe link in any email</li>
      <li>Replying STOP to any text message</li>
      <li>Emailing contact@gearnerd.io</li>
    </ul>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">SMS/Text Messages</h3>
    <p>If you opt in to SMS alerts:</p>
    <ul style="margin:0.5rem 0 0.5rem 1.5rem">
      <li>Message frequency varies (typically a few per week)</li>
      <li>Message and data rates may apply</li>
      <li>Reply STOP to cancel at any time</li>
      <li>Reply HELP for assistance</li>
      <li>Carriers are not liable for delayed or undelivered messages</li>
    </ul>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Contact</h3>
    <p>For questions about this privacy policy or your data, contact us at
    <a href="mailto:contact@gearnerd.io">contact@gearnerd.io</a>.</p>

    <p style="margin-top:1.5rem"><strong>Operated by:</strong> Intuitive Dataframe, LLC</p>
  </div>
</div>
""", logged_in=False))


@app.get("/terms", response_class=HTMLResponse)
async def terms_of_service():
    """Public terms of service page."""
    return HTMLResponse(content=_page("Terms of Service", """
<div style="max-width:700px;margin:2rem auto">
  <h2>Terms of Service</h2>
  <p style="color:#64748b;font-size:0.85rem;margin-bottom:1.5rem">Last updated: April 2026</p>

  <div style="font-size:0.9rem;color:#475569;line-height:1.7">
    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Service Description</h3>
    <p>FL Rules Monitor is a free public service alert system that monitors the Florida Administrative
    Register for rule changes, proposed regulations, emergency rules, and meeting notices relevant to
    civil rights. Alerts are delivered via email and optionally via SMS text messages.</p>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Consent to Receive Messages</h3>
    <p>By subscribing to FL Rules Monitor, you consent to receive automated alert messages at the email
    address and/or mobile phone number you provide. For SMS alerts:</p>
    <ul style="margin:0.5rem 0 0.5rem 1.5rem">
      <li>You confirm that you are the owner or authorized user of the phone number provided</li>
      <li>You understand that message frequency varies</li>
      <li>You understand that message and data rates may apply</li>
      <li>You may opt out at any time by replying STOP</li>
      <li>You may get help by replying HELP or emailing contact@gearnerd.io</li>
    </ul>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">No Guarantee</h3>
    <p>While we strive to provide timely and accurate alerts, we make no guarantee that all relevant
    notices will be detected or that alerts will be delivered without delay. This service supplements,
    but does not replace, direct monitoring of the Florida Administrative Register.</p>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Modifications</h3>
    <p>We reserve the right to modify or discontinue this service at any time. We will make reasonable
    efforts to notify subscribers of significant changes.</p>

    <h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Contact</h3>
    <p>For questions, contact <a href="mailto:contact@gearnerd.io">contact@gearnerd.io</a>.</p>

    <p style="margin-top:1.5rem"><strong>Operated by:</strong> Intuitive Dataframe, LLC</p>
  </div>
</div>
""", logged_in=False))


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    """Public page explaining how relevance scoring works."""
    user = await get_current_user(request)
    return HTMLResponse(content=_page("How It Works", """
<div style="max-width:760px;margin:2rem auto">
  <h2>How FL Rules Monitor Works</h2>
  <p style="color:#64748b;font-size:0.9rem;margin-bottom:1.5rem">Understanding alerts and relevance scoring</p>

  <div style="font-size:0.95rem;color:#334155;line-height:1.7">
    <h3 style="font-size:1.1rem;margin:1.5rem 0 0.5rem">What we monitor</h3>
    <p>FL Rules Monitor scans the <a href="https://flrules.org/bigdoc/default.asp" target="_blank" rel="noopener">Florida Administrative Register</a>
    automatically — hourly during weekday business hours, twice daily on weekends.
    The Register is where all Florida state agencies publish proposed rules, emergency rules, meeting notices,
    and other regulatory actions.</p>

    <h3 style="font-size:1.1rem;margin:1.5rem 0 0.5rem">How alerts are scored</h3>
    <p>Every notice is scanned for keywords across <strong>9 civil-rights-relevant categories</strong>.
    Each category has a weight reflecting its importance. A notice's <em>relevance score</em>
    is the sum of weights for every category it matches. Notices with a score of <strong>1.0 or higher</strong> trigger an alert.</p>

    <table style="width:100%;border-collapse:collapse;margin:1rem 0;font-size:0.9rem">
      <thead>
        <tr style="background:#f1f5f9">
          <th style="text-align:left;padding:0.5rem;border:1px solid #e2e8f0">Category</th>
          <th style="text-align:left;padding:0.5rem;border:1px solid #e2e8f0">Weight</th>
          <th style="text-align:left;padding:0.5rem;border:1px solid #e2e8f0">Triggers on</th>
        </tr>
      </thead>
      <tbody>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Domestic Terrorism</td><td style="padding:0.5rem;border:1px solid #e2e8f0">3.0</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Organization designations, material support rules</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Religious Freedom</td><td style="padding:0.5rem;border:1px solid #e2e8f0">3.0</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Mosque, Islamic, hijab, halal, religious discrimination</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Surveillance</td><td style="padding:0.5rem;border:1px solid #e2e8f0">2.5</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Fusion centers, biometrics, watchlists, CVE programs</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Cabinet Meetings</td><td style="padding:0.5rem;border:1px solid #e2e8f0">2.0</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Governor/Cabinet agendas, executive orders</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Immigration</td><td style="padding:0.5rem;border:1px solid #e2e8f0">2.0</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Refugee, asylum, travel ban, ICE cooperation</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Civil Rights</td><td style="padding:0.5rem;border:1px solid #e2e8f0">2.0</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Hate crimes, profiling, anti-discrimination</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Education</td><td style="padding:0.5rem;border:1px solid #e2e8f0">1.5</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Curriculum bans, DEI restrictions, book bans</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Nonprofit Regulation</td><td style="padding:0.5rem;border:1px solid #e2e8f0">1.5</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Charity registration, foreign agent rules</td></tr>
        <tr><td style="padding:0.5rem;border:1px solid #e2e8f0">Policy General</td><td style="padding:0.5rem;border:1px solid #e2e8f0">0.5</td><td style="padding:0.5rem;border:1px solid #e2e8f0">Emergency rules, public comment periods (only flags when combined)</td></tr>
      </tbody>
    </table>

    <h3 style="font-size:1.1rem;margin:1.5rem 0 0.5rem">Severity tiers</h3>
    <p>Each alert is colored and labeled by its score:</p>
    <ul style="list-style:none;padding:0;margin:0.5rem 0">
      <li style="padding:0.5rem;border-left:4px solid #f59e0b;background:#fef3c7;margin-bottom:0.5rem;border-radius:4px">
        <strong style="color:#92400e">Watch (1.0&ndash;2.4)</strong> &mdash; Single-category match worth monitoring.
      </li>
      <li style="padding:0.5rem;border-left:4px solid #ea580c;background:#ffedd5;margin-bottom:0.5rem;border-radius:4px">
        <strong style="color:#9a3412">Alert (2.5&ndash;4.9)</strong> &mdash; Notable; overlapping concerns or higher-weight category.
      </li>
      <li style="padding:0.5rem;border-left:4px solid #dc2626;background:#fee2e2;border-radius:4px">
        <strong style="color:#991b1b">High Priority (5.0+)</strong> &mdash; Multiple high-weight categories; review promptly.
      </li>
    </ul>

    <h3 style="font-size:1.1rem;margin:1.5rem 0 0.5rem">Why scoring matters</h3>
    <p>Each category's weight contributes only <em>once</em> per notice, even if multiple keywords from
    the same category appear. This prevents single notices from being over-scored just because they repeat
    a theme. Higher scores indicate that <em>multiple distinct concerns</em> overlap in a single notice
    &mdash; those tend to deserve the most attention.</p>

    <h3 style="font-size:1.1rem;margin:1.5rem 0 0.5rem">Limitations</h3>
    <p>This is a keyword-pattern matcher, not a substitute for human review. It may miss notices that use
    unusual phrasing or flag notices that are not actually relevant. We recommend treating alerts as a
    starting point for further review by your team.</p>

    <h3 style="font-size:1.1rem;margin:1.5rem 0 0.5rem">Customizing your alerts</h3>
    <p>Subscribers can choose to receive all categories or pick specific ones from their
    <a href="/my/settings">settings page</a>. You only get alerts that overlap with your selected categories.</p>

    <h3 style="font-size:1.1rem;margin:1.5rem 0 0.5rem">Source code</h3>
    <p>FL Rules Monitor is open source. The scoring rules live in
    <code style="background:#f1f5f9;padding:2px 6px;border-radius:3px;font-size:0.85rem">relevance.py</code>
    and are reviewed periodically by the operating team.</p>

    <p style="margin-top:2rem;font-size:0.85rem;color:#64748b">
      Operated by Intuitive Dataframe, LLC for civil rights advocacy.
      Questions? <a href="mailto:contact@gearnerd.io">contact@gearnerd.io</a>
    </p>
  </div>
</div>
""", user=user, logged_in=user is not None))


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


# ── Public landing page / Dashboard ─────────────────

@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await get_current_user(request)
    if not user:
        # Public landing page — visible to Twilio reviewers and unauthenticated visitors
        landing_html = _page("FL Rules Monitor", """
<div style="max-width:800px;margin:2rem auto">
  <div style="text-align:center;margin-bottom:2rem">
    <svg width="64" height="64" viewBox="0 0 36 36" fill="none">
      <rect x="4" y="2" width="20" height="28" rx="3" fill="#1e40af" opacity="0.15"/>
      <rect x="6" y="4" width="20" height="28" rx="3" fill="#2563eb" opacity="0.3"/>
      <rect x="8" y="6" width="20" height="28" rx="3" fill="white"/>
      <circle cx="27" cy="27" r="9" fill="#2563eb"/>
      <path d="M27 21.5c-3 0-5.5 1.5-5.5 1.5s0 5 1.5 7c1.5 2 4 3 4 3s2.5-1 4-3c1.5-2 1.5-7 1.5-7s-2.5-1.5-5.5-1.5z" fill="white" opacity="0.95"/>
      <path d="M25 27l1.5 1.5 3-3" stroke="#2563eb" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <h1 style="margin-top:1rem;font-size:2rem">FL Rules Monitor</h1>
    <p style="color:#64748b;font-size:1.1rem;margin-top:0.5rem">Florida Administrative Register Civil Rights Alert System</p>
  </div>

  <div class="card" style="max-width:100%;margin-bottom:1.5rem">
    <h2 style="font-size:1.2rem;margin-bottom:0.75rem">About This Service</h2>
    <p style="color:#475569;line-height:1.7;margin-bottom:0.75rem">
      <strong>FL Rules Monitor</strong> is a free public service alert system that automatically monitors the
      <a href="https://flrules.org" target="_blank" rel="noopener" style="color:#2563eb">Florida Administrative Register</a>
      for rule changes, proposed regulations, emergency rules, and meeting notices relevant to civil rights.
    </p>
    <p style="color:#475569;line-height:1.7;margin-bottom:0.75rem">
      When the system detects relevant content — such as domestic terrorism designations, religious freedom rules,
      surveillance programs, immigration policies, or civil rights notices — it sends alerts to opted-in subscribers
      via email and SMS.
    </p>
    <p style="color:#475569;line-height:1.7">
      This service is operated by <strong>Intuitive Dataframe, LLC</strong>, a Florida-based civic technology company.
    </p>
  </div>

  <div class="card" style="max-width:100%;margin-bottom:1.5rem">
    <h2 style="font-size:1.2rem;margin-bottom:0.75rem">What We Monitor</h2>
    <ul style="color:#475569;line-height:1.8;margin-left:1.5rem">
      <li><strong>Domestic Terrorism Designations</strong> — Organization designations and material support rules</li>
      <li><strong>Religious Freedom</strong> — Mosque, Islamic organization, and faith-based policies</li>
      <li><strong>Surveillance</strong> — Fusion centers, biometrics, and watchlists</li>
      <li><strong>Cabinet Meetings</strong> — Governor and Cabinet agendas and executive orders</li>
      <li><strong>Immigration</strong> — Refugee, asylum, and ICE cooperation policies</li>
      <li><strong>Civil Rights</strong> — Hate crimes, profiling, and discrimination rules</li>
      <li><strong>Education</strong> — Curriculum bans and DEI restrictions</li>
      <li><strong>Nonprofit Regulation</strong> — Charity registration and foreign agent rules</li>
    </ul>
  </div>

  <div class="card" style="max-width:100%;margin-bottom:1.5rem;text-align:center;background:#f0f9ff;border-color:#bae6fd">
    <h2 style="font-size:1.2rem;margin-bottom:0.5rem">Subscribe to Alerts</h2>
    <p style="color:#475569;margin-bottom:1rem">Get notified by email and/or SMS when relevant notices are published.</p>
    <a class="btn" href="/signup" style="padding:0.75rem 1.5rem;font-size:1rem">Subscribe Now</a>
    <p style="margin-top:1rem;font-size:0.85rem;color:#64748b">Already subscribed? <a href="/login" style="color:#2563eb">Manage your preferences &rarr;</a></p>
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1rem;margin-bottom:2rem">
    <a href="/about" style="text-decoration:none;color:#475569;text-align:center;padding:1rem;border:1px solid #e2e8f0;border-radius:8px">How It Works</a>
    <a href="/privacy" style="text-decoration:none;color:#475569;text-align:center;padding:1rem;border:1px solid #e2e8f0;border-radius:8px">Privacy Policy</a>
    <a href="/terms" style="text-decoration:none;color:#475569;text-align:center;padding:1rem;border:1px solid #e2e8f0;border-radius:8px">Terms of Service</a>
    <a href="/auth/login" style="text-decoration:none;color:#475569;text-align:center;padding:1rem;border:1px solid #e2e8f0;border-radius:8px">Staff Login</a>
  </div>

  <div style="text-align:center;color:#94a3b8;font-size:0.85rem;padding:1rem;border-top:1px solid #e2e8f0">
    <p style="margin-bottom:0.25rem"><strong>Operated by:</strong> Intuitive Dataframe, LLC</p>
    <p style="margin-bottom:0.25rem"><strong>Contact:</strong> <a href="mailto:contact@gearnerd.io" style="color:#2563eb">contact@gearnerd.io</a></p>
    <p>Tampa, Florida, USA</p>
  </div>
</div>
""", logged_in=False)
        return HTMLResponse(content=landing_html)

    # Logged-in dashboard
    return await _dashboard(request, user, session)


async def _dashboard(
    request: Request,
    user: User,
    session: AsyncSession,
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
    is_admin = user.role == "admin"
    for a in alerts:
        ts = a.created_at.strftime("%Y-%m-%d %H:%M") if isinstance(a.created_at, datetime) else str(a.created_at)
        notified = "&#10003;" if a.notified else "&middot;"
        action_cell = ""
        if is_admin:
            label = "Resend" if a.notified else "Send"
            action_cell = (
                f'<td class="center"><form method="post" action="/admin/alerts/{a.id}/send" style="display:inline" '
                f'onsubmit="return confirm(\'Send this alert to all matching subscribers?\')">'
                f'<button type="submit" class="btn btn-outline" style="padding:2px 8px;font-size:0.75rem">{label}</button>'
                f'</form></td>'
            )
        alert_rows += f"""<tr>
<td>{ts}</td>
<td><span class="badge">{a.category}</span></td>
<td>{a.relevance_score:.1f}</td>
<td>{_esc(a.summary[:150])}</td>
<td class="center">{notified}</td>
{action_cell}
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
  <thead><tr><th>Date</th><th>Category</th><th>Score</th><th>Summary</th><th>Sent</th>{'<th></th>' if is_admin else ''}</tr></thead>
  <tbody>
    {alert_rows if alert_rows else f'<tr><td colspan="{6 if is_admin else 5}" class="empty">No alerts yet. The system is monitoring.</td></tr>'}
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
        category_picker = _category_checkboxes(sub.categories or "all")
        email_chk = "checked" if sub.notify_email else ""
        sms_chk = "checked" if sub.notify_sms else ""
        status_html = f"""
<div class="card">
  <h3>Your Subscription</h3>
  <p>Status: <span class="badge" style="background:#10b981">Active</span></p>
  <form method="post" action="/my/settings/update">
    <div class="form-group">
      <label>Phone (for SMS alerts)</label>
      <input type="tel" name="phone" class="input" value="{_esc(sub.phone or "")}" placeholder="+1XXXXXXXXXX">
    </div>
    <div class="form-group">
      <label>Alert Categories</label>
      {category_picker}
    </div>
    <div class="form-group" style="display:flex;gap:1.5rem">
      <label><input type="checkbox" name="notify_email" {email_chk}> Email alerts</label>
      <label><input type="checkbox" name="notify_sms" {sms_chk}> SMS alerts</label>
    </div>
    <button type="submit" class="btn">Save Changes</button>
  </form>
  <form method="post" action="/my/unsubscribe" style="margin-top:1rem">
    <button type="submit" class="btn btn-danger">Unsubscribe</button>
  </form>
</div>"""
    else:
        category_picker = _category_checkboxes("all")
        status_html = f"""
<div class="card">
  <h3>Not Subscribed</h3>
  <p>You are not currently receiving alerts.</p>
  <form method="post" action="/my/subscribe">
    <div class="form-group">
      <label>Phone (for SMS alerts, optional)</label>
      <input type="tel" name="phone" placeholder="+1XXXXXXXXXX" class="input">
    </div>
    <div class="form-group">
      <label>Alert Categories</label>
      {category_picker}
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
    categories = _parse_categories_form(form.getlist("categories"))

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

    asyncio.create_task(send_opt_in_email(user.email, user.name, sub.unsubscribe_token))

    if phone:
        asyncio.create_task(send_opt_in_confirmation(phone))

    return RedirectResponse(url="/my/settings", status_code=302)


@app.post("/my/settings/update")
async def my_settings_update(
    request: Request,
    user: User = Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.email == user.email)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return RedirectResponse(url="/my/settings", status_code=302)

    form = await request.form()
    sub.phone = form.get("phone", "").strip() or None
    sub.categories = _parse_categories_form(form.getlist("categories"))
    sub.notify_email = "notify_email" in form
    sub.notify_sms = "notify_sms" in form
    await session.commit()
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


# ── Public manage / unsubscribe (no login required) ──

@app.get("/login", response_class=HTMLResponse)
async def public_login_request():
    """Public page for existing subscribers to request a manage-link email."""
    return HTMLResponse(content=_page("Access Your Subscription", """
<div style="max-width:520px;margin:3rem auto">
  <div class="card" style="max-width:100%">
    <h2 style="margin-top:0">Access Your Subscription</h2>
    <p style="color:#475569;font-size:0.9rem;line-height:1.6">
      Already subscribed? Enter your email and we'll send you a secure link to manage
      your alert categories, switch between email and SMS, or unsubscribe.
    </p>
    <form method="post" action="/login" style="margin-top:1.5rem">
      <div class="form-group">
        <label>Email address</label>
        <input type="email" name="email" class="input" placeholder="you@example.com" required>
      </div>
      <button type="submit" class="btn" style="width:100%;padding:0.75rem">Email me a manage link</button>
    </form>
    <p style="margin-top:1.5rem;font-size:0.8rem;color:#94a3b8;text-align:center">
      Not subscribed yet? <a href="/signup" style="color:#2563eb">Sign up here</a>
    </p>
  </div>
</div>""", logged_in=False))


@app.post("/login")
async def public_login_send(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Look up subscriber by email and send them their /manage link.

    Always shows the same confirmation regardless of whether the email exists,
    to avoid leaking subscriber list information.
    """
    form = await request.form()
    email = form.get("email", "").strip().lower()

    if email:
        result = await session.execute(
            select(Subscriber).where(func.lower(Subscriber.email) == email)
        )
        sub = result.scalar_one_or_none()
        if sub and sub.active and sub.unsubscribe_token:
            from flrules.notifier import send_manage_link_email
            manage_url = f"{settings.app_url}/manage/{sub.unsubscribe_token}"
            asyncio.create_task(send_manage_link_email(sub.email, sub.name, manage_url))

    return HTMLResponse(content=_page("Check Your Email", """
<div class="card" style="text-align:center;max-width:520px;margin:3rem auto">
  <h3>Check Your Email</h3>
  <p>If that email is subscribed to FL Rules Monitor, we just sent you a secure link to manage your preferences.</p>
  <p style="font-size:0.85rem;color:#94a3b8;margin-top:1rem">The email may take a minute to arrive. Check your spam folder if you don't see it.</p>
</div>""", logged_in=False))


@app.get("/manage/{token}", response_class=HTMLResponse)
async def public_manage(
    token: str,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == token)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        return HTMLResponse(content=_page("Manage Subscription", """
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>Invalid Link</h3>
  <p>This subscription link is not valid.</p>
</div>""", logged_in=False))

    if not sub.active:
        return HTMLResponse(content=_page("Manage Subscription", """
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>Subscription Inactive</h3>
  <p>This subscription has been deactivated. Contact your administrator to re-subscribe.</p>
</div>""", logged_in=False))

    category_picker = _category_checkboxes(sub.categories or "all")
    email_chk = "checked" if sub.notify_email else ""
    sms_chk = "checked" if sub.notify_sms else ""

    return HTMLResponse(content=_page("Manage Subscription", f"""
<div style="max-width:600px;margin:2rem auto">
  <div class="card" style="max-width:100%">
    <h2 style="margin-top:0">Manage Your Subscription</h2>
    <p style="color:#64748b;font-size:0.9rem">
      Update your alert preferences below. Changes save when you click "Save Changes".
    </p>
    <form method="post" action="/manage/{token}">
      <div class="form-group">
        <label>Email</label>
        <input type="email" class="input" value="{_esc(sub.email or "")}" disabled style="background:#f1f5f9">
        <span style="font-size:0.75rem;color:#94a3b8">To change your email, contact your administrator.</span>
      </div>
      <div class="form-group">
        <label>Mobile Phone (for SMS alerts)</label>
        <input type="tel" name="phone" class="input" value="{_esc(sub.phone or "")}" placeholder="+1XXXXXXXXXX">
        <span style="font-size:0.75rem;color:#94a3b8">US numbers only. Include country code (+1).</span>
      </div>
      <div class="form-group">
        <label>Alert Categories</label>
        {category_picker}
      </div>
      <div class="form-group" style="display:flex;gap:1.5rem">
        <label><input type="checkbox" name="notify_email" {email_chk}> Email alerts</label>
        <label><input type="checkbox" name="notify_sms" {sms_chk}> SMS alerts</label>
      </div>
      <button type="submit" class="btn" style="width:100%;padding:0.75rem">Save Changes</button>
    </form>
    <div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;text-align:center">
      <a href="/unsubscribe/{token}" style="color:#dc2626;font-size:0.85rem">Unsubscribe from all alerts</a>
    </div>
  </div>
</div>""", logged_in=False))


@app.post("/manage/{token}")
async def public_manage_save(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == token)
    )
    sub = result.scalar_one_or_none()
    if not sub or not sub.active:
        return RedirectResponse(url=f"/manage/{token}", status_code=302)

    form = await request.form()
    sub.phone = form.get("phone", "").strip() or None
    sub.categories = _parse_categories_form(form.getlist("categories"))
    sub.notify_email = "notify_email" in form
    sub.notify_sms = "notify_sms" in form
    await session.commit()

    return HTMLResponse(content=_page("Subscription Updated", f"""
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>Settings Saved</h3>
  <p>Your subscription preferences have been updated.</p>
  <a class="btn btn-outline" href="/manage/{token}" style="margin-top:1rem">Back to Settings</a>
</div>""", logged_in=False))


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def public_unsubscribe(
    token: str,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == token)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        return HTMLResponse(content=_page("Unsubscribe", """
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>Invalid Link</h3>
  <p>This unsubscribe link is not valid or has already been used.</p>
</div>""", logged_in=False))

    if not sub.active:
        return HTMLResponse(content=_page("Unsubscribe", """
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>Already Unsubscribed</h3>
  <p>You have already been unsubscribed from FL Rules Monitor alerts.</p>
</div>""", logged_in=False))

    return HTMLResponse(content=_page("Unsubscribe", f"""
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>Unsubscribe from FL Rules Monitor</h3>
  <p>Are you sure you want to stop receiving alerts at <strong>{_esc(sub.email or sub.phone or "")}</strong>?</p>
  <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:1rem;margin:1rem 0;text-align:left">
    <p style="margin:0 0 0.5rem;font-weight:600;color:#0c4a6e">Want fewer alerts instead of unsubscribing?</p>
    <p style="margin:0 0 0.75rem;font-size:0.85rem;color:#0369a1">You can pick specific categories, turn off SMS, or turn off email — without unsubscribing entirely.</p>
    <a class="btn btn-outline" href="/manage/{token}" style="font-size:0.85rem">Manage my preferences</a>
  </div>
  <form method="post" action="/unsubscribe/{token}" style="margin-top:1rem">
    <button type="submit" class="btn btn-danger">Yes, unsubscribe me from everything</button>
  </form>
</div>""", logged_in=False))


@app.post("/unsubscribe/{token}")
async def public_unsubscribe_confirm(
    token: str,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == token)
    )
    sub = result.scalar_one_or_none()
    if sub:
        sub.active = False
        await session.commit()

    return HTMLResponse(content=_page("Unsubscribed", """
<div class="card" style="text-align:center;max-width:500px;margin:3rem auto">
  <h3>You've been unsubscribed</h3>
  <p>You will no longer receive FL Rules Monitor alerts. If this was a mistake, please contact your administrator.</p>
</div>""", logged_in=False))


# ── Operational backup endpoint (token-protected) ───

@app.get("/_backup")
async def backup_db(token: str = Query("")):
    """Stream an atomic snapshot of the SQLite DB.

    Protected by a long random `BACKUP_TOKEN` env var. Hit by the daily backup
    workflow which stores the result as a GitHub Actions artifact (free, 30-day
    retention). Uses SQLite's `VACUUM INTO` for an atomic, consistent copy
    even while the app is serving live traffic.
    """
    expected = settings.backup_token
    if not expected or not _secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid backup token")

    db_url = settings.database_url
    data_dir = os.environ.get("DATA_DIR")
    if data_dir and "sqlite" in db_url:
        db_url = f"sqlite+aiosqlite:///{data_dir.rstrip('/')}/flrules.db"
    if "sqlite" not in db_url:
        raise HTTPException(status_code=400, detail="non-sqlite backup not supported")

    db_path = db_url.split("///")[-1]
    if not Path(db_path).exists():
        raise HTTPException(status_code=404, detail="database file not found")

    # Atomic snapshot via VACUUM INTO — safer than a raw file copy under load
    fd, snapshot_path = tempfile.mkstemp(prefix="flrules-backup-", suffix=".db")
    os.close(fd)
    Path(snapshot_path).unlink(missing_ok=True)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"VACUUM INTO '{snapshot_path}'")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    cleanup = BackgroundTask(lambda: Path(snapshot_path).unlink(missing_ok=True))
    return FileResponse(
        snapshot_path,
        media_type="application/octet-stream",
        filename=f"flrules-{timestamp}.db",
        background=cleanup,
    )


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
<td style="font-size:0.85rem">{_esc(_format_categories(s.categories))}</td>
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
    category_picker = _category_checkboxes("all")
    content = f"""
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
      {category_picker}
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
    email = (form.get("email", "").strip().lower()) or None
    phone = form.get("phone", "").strip() or None
    name = form.get("name", "").strip()
    categories = _parse_categories_form(form.getlist("categories"))
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
        asyncio.create_task(send_opt_in_email(email, name, sub.unsubscribe_token))

    if phone and notify_sms:
        asyncio.create_task(send_opt_in_confirmation(phone))

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

    category_picker = _category_checkboxes(sub.categories or "all")

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
      {category_picker}
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
    sub.email = (form.get("email", "").strip().lower()) or None
    sub.phone = form.get("phone", "").strip() or None
    sub.categories = _parse_categories_form(form.getlist("categories"))
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


@app.post("/admin/alerts/{alert_id}/send")
async def admin_resend_alert(
    alert_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Send an existing alert to all matching subscribers (idempotent re-send).

    Useful for testing the notification path when the pipeline already
    de-duped against this alert, or for re-sending if a previous delivery
    failed. Marks `notified=True` once dispatched.
    """
    result = await session.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        return RedirectResponse(url="/", status_code=302)

    notice_result = await session.execute(
        select(FARNotice).where(FARNotice.notice_id == alert.notice_id)
    )
    notice = notice_result.scalar_one_or_none()
    notice_url = notice.url if notice else ""

    # Self-heal legacy alerts whose summary was stored as the old meta-text
    # format ("Matched categories: ..."). Pull the actual notice description
    # so subscribers see what the notice is about, not why it matched.
    if notice and notice.description and alert.summary.startswith("Matched categories"):
        desc = notice.description.strip()
        if len(desc) > 280:
            desc = desc[:277].rstrip() + "..."
        if desc:
            alert.summary = desc

    sub_result = await session.execute(select(Subscriber))
    subscribers = list(sub_result.scalars().all())

    from flrules.notifier import notify_subscribers
    await notify_subscribers(subscribers, alert, notice_url)

    alert.notified = True
    await session.commit()
    return RedirectResponse(url="/", status_code=302)


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

CATEGORY_OPTIONS = [
    ("domestic_terrorism", "Domestic Terrorism"),
    ("religious_freedom", "Religious Freedom"),
    ("immigration", "Immigration"),
    ("civil_rights", "Civil Rights"),
    ("surveillance", "Surveillance"),
    ("cabinet_meeting", "Cabinet Meetings"),
    ("education", "Education"),
    ("nonprofit_regulation", "Nonprofit Regulation"),
]


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _category_checkboxes(selected: str = "all") -> str:
    """Render the multi-select category picker. `selected` is comma-separated."""
    selected_set = {s.strip() for s in (selected or "all").split(",") if s.strip()}
    is_all = "all" in selected_set or not selected_set

    all_checked = "checked" if is_all else ""
    items = ""
    for val, label in CATEGORY_OPTIONS:
        chk = "checked" if (val in selected_set and not is_all) else ""
        items += (
            f'<label style="display:flex;align-items:center;gap:0.5rem;cursor:pointer;font-size:0.9rem;padding:0.25rem">'
            f'<input type="checkbox" name="categories" value="{val}" {chk} class="cat-individual" '
            f'onchange="document.getElementById(\'cat-all\').checked=false">'
            f'<span>{label}</span></label>'
        )

    return f"""
<div style="border:1px solid #e2e8f0;border-radius:6px;padding:0.75rem;background:#fafafa">
  <label style="display:flex;align-items:center;gap:0.5rem;cursor:pointer;font-weight:600;padding-bottom:0.5rem;border-bottom:1px solid #e2e8f0;margin-bottom:0.5rem">
    <input type="checkbox" id="cat-all" name="categories" value="all" {all_checked}
      onchange="if(this.checked){{document.querySelectorAll('.cat-individual').forEach(c=>c.checked=false)}}">
    <span>All categories (recommended)</span>
  </label>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.25rem">
    {items}
  </div>
  <p style="font-size:0.75rem;color:#94a3b8;margin:0.5rem 0 0">Choose "All" or pick specific categories. Picking individual categories overrides "All".</p>
</div>"""


def _parse_categories_form(values: list[str]) -> str:
    """Convert form-submitted category values into the stored comma-separated string."""
    vals = [v for v in values if v]
    if not vals or "all" in vals:
        return "all"
    valid = {v for v, _ in CATEGORY_OPTIONS}
    selected = [v for v in vals if v in valid]
    return ",".join(selected) if selected else "all"


def _format_categories(cats: str) -> str:
    """Display a stored category string in human-readable form."""
    if not cats or cats == "all":
        return "All"
    labels = {v: label for v, label in CATEGORY_OPTIONS}
    return ", ".join(labels.get(c.strip(), c.strip()) for c in cats.split(","))


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
