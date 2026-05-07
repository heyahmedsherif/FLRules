"""
Notification system — SMS via Twilio, email via Resend (or SMTP fallback).

Email: Resend API (works on Railway, any cloud). Falls back to SMTP if no API key.
SMS: Twilio (pay-as-you-go, requires toll-free verification).
Alerts are also logged to a local JSON file for audit.
"""

import json
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import resend
import structlog
from twilio.rest import Client as TwilioClient

from flrules.config import settings
from flrules.models import Alert, Subscriber

log = structlog.get_logger()


def _send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    """Send an email via Resend API (primary) or SMTP (fallback)."""
    if settings.resend_api_key:
        try:
            resend.api_key = settings.resend_api_key
            payload = {
                "from": settings.from_email,
                "to": [to],
                "reply_to": "contact@gearnerd.io",
                "subject": subject,
                "text": body,
            }
            if html:
                payload["html"] = html
            resend.Emails.send(payload)
            log.info("email_sent_resend", to=to, subject=subject)
            return True
        except Exception as e:
            log.error("email_failed_resend", to=to, error=str(e))
            return False

    if settings.smtp_host and settings.smtp_user and settings.smtp_password:
        try:
            if html:
                from email.mime.multipart import MIMEMultipart
                msg = MIMEMultipart("alternative")
                msg.attach(MIMEText(body, "plain"))
                msg.attach(MIMEText(html, "html"))
            else:
                msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = settings.from_email
            msg["To"] = to
            msg["Reply-To"] = "contact@gearnerd.io"

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)

            log.info("email_sent_smtp", to=to, subject=subject)
            return True
        except Exception as e:
            log.error("email_failed_smtp", to=to, error=str(e))
            return False

    log.warning("email_skipped", reason="no Resend API key or SMTP config", to=to)
    return False


def _score_tier(score: float) -> tuple[str, str, str]:
    """Return (tier_label, color_hex, sms_prefix) for a relevance score."""
    if score >= 5.0:
        return ("High Priority", "#dc2626", "[HIGH]")
    if score >= 2.5:
        return ("Alert", "#ea580c", "[ALERT]")
    return ("Watch", "#f59e0b", "[WATCH]")

_data_dir = os.environ.get("DATA_DIR")
if _data_dir:
    ALERTS_LOG = Path(_data_dir) / "alerts_log.json"
else:
    ALERTS_LOG = Path(settings.database_url.split("///")[-1]).parent / "alerts_log.json"


def _twilio_client() -> TwilioClient | None:
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    return None


def _display_keywords(stored: str) -> str:
    """Render alert.matched_keywords for display, humanizing any raw regex
    that might be present in legacy rows. Idempotent for already-clean data."""
    from flrules.relevance import humanize_pattern
    parts = [humanize_pattern(p.strip()) for p in (stored or "").split(",") if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ", ".join(out)


def _display_summary(stored: str) -> str:
    """Strip the most common regex metachar leakage from legacy summary text.

    New alerts store the actual notice description, but pre-existing rows may
    still hold the old "Matched categories: X. Keywords: \\s+..." format. This
    is a narrow safety net that doesn't risk damaging legitimate content
    (e.g., parens or dots in notice descriptions stay intact).
    """
    if not stored:
        return ""
    return (
        stored.replace(r"\s+", " ")
        .replace(r"\s*", " ")
        .replace(r"\b", "")
        .replace(r"\.", ".")
    )


def _build_alert_body(
    alert: Alert,
    notice_url: str,
    unsubscribe_url: str = "",
    manage_url: str = "",
) -> str:
    """Build a plain-text alert body (used as email fallback)."""
    tier_label, _, _ = _score_tier(alert.relevance_score)
    links_block = ""
    if manage_url or unsubscribe_url:
        parts = []
        if manage_url:
            parts.append(f"Manage your preferences: {manage_url}")
        if unsubscribe_url:
            parts.append(f"Unsubscribe: {unsubscribe_url}")
        links_block = "\n" + "\n".join(parts) + "\n"
    return f"""[{tier_label}] Florida Administrative Register Alert

Category: {alert.category.replace('_', ' ').title()}
Severity: {tier_label} (score {alert.relevance_score:.1f})

Summary:
{_display_summary(alert.summary)}

Matched Keywords: {_display_keywords(alert.matched_keywords)}

View the full notice:
{notice_url}

---
About relevance scoring:
  Watch (1.0–2.4): Worth monitoring — single-category match.
  Alert (2.5–4.9): Notable — overlapping concerns.
  High Priority (5.0+): Multiple high-weight categories — review promptly.

Learn more: {settings.app_url}/about

What this means: This notice was flagged because it contains language related to
{alert.category.replace('_', ' ')}. We recommend reviewing the full text to assess
whether it may affect your community or requires advocacy action.
{links_block}"""


def _build_alert_html(
    alert: Alert,
    notice_url: str,
    unsubscribe_url: str = "",
    manage_url: str = "",
) -> str:
    """Build an HTML alert email with color-coded severity banner."""
    tier_label, color, _ = _score_tier(alert.relevance_score)
    cat_label = alert.category.replace("_", " ").title()
    about_url = f"{settings.app_url}/about"
    footer_links = []
    if manage_url:
        footer_links.append(f'<a href="{manage_url}" style="color:#94a3b8">Manage preferences</a>')
    if unsubscribe_url:
        footer_links.append(f'<a href="{unsubscribe_url}" style="color:#94a3b8">Unsubscribe</a>')
    footer_html = (
        f'<p style="font-size:12px;color:#94a3b8;margin-top:24px">{" &middot; ".join(footer_links)}</p>'
        if footer_links else ""
    )
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#f8fafc;margin:0;padding:24px;color:#0f172a">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
    <div style="background:{color};color:#ffffff;padding:16px 24px">
      <div style="font-size:12px;letter-spacing:0.05em;text-transform:uppercase;opacity:0.9">{tier_label}</div>
      <div style="font-size:20px;font-weight:700;margin-top:4px">FL Rules Monitor — {cat_label}</div>
    </div>
    <div style="padding:24px">
      <table style="width:100%;font-size:14px;margin-bottom:16px">
        <tr><td style="color:#64748b;width:120px">Category</td><td style="font-weight:600">{cat_label}</td></tr>
        <tr><td style="color:#64748b">Severity</td><td style="font-weight:600;color:{color}">{tier_label} (score {alert.relevance_score:.1f})</td></tr>
        <tr><td style="color:#64748b">Keywords</td><td style="font-family:monospace;font-size:12px;color:#475569">{_display_keywords(alert.matched_keywords)}</td></tr>
      </table>
      <h3 style="font-size:14px;color:#64748b;margin:0 0 8px;text-transform:uppercase;letter-spacing:0.05em">Summary</h3>
      <p style="font-size:15px;line-height:1.6;margin:0 0 24px">{_display_summary(alert.summary)}</p>
      <a href="{notice_url}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:10px 20px;border-radius:6px;font-weight:600;font-size:14px">View Full Notice &rarr;</a>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:14px;margin-top:24px;font-size:13px;line-height:1.6">
        <strong style="color:#0f172a">What does this score mean?</strong><br>
        <span style="color:#475569">
          <span style="color:#f59e0b">&#9679;</span> <strong>Watch</strong> (1.0–2.4): single-category match worth monitoring.<br>
          <span style="color:#ea580c">&#9679;</span> <strong>Alert</strong> (2.5–4.9): notable, overlapping concerns.<br>
          <span style="color:#dc2626">&#9679;</span> <strong>High Priority</strong> (5.0+): multiple high-weight categories — review promptly.
        </span><br>
        <a href="{about_url}" style="color:#2563eb;font-size:12px">Learn how relevance is scored &rarr;</a>
      </div>
      <p style="font-size:12px;color:#94a3b8;line-height:1.6;margin-top:20px">
        This notice was flagged because it contains language related to {cat_label.lower()}.
        We recommend reviewing the full text to assess whether it may affect your community or requires advocacy action.
      </p>
      {footer_html}
    </div>
  </div>
</body></html>"""


def _build_sms_body(alert: Alert, notice_url: str) -> str:
    """Build a concise SMS alert with severity prefix."""
    cat = alert.category.replace("_", " ").title()
    _, _, prefix = _score_tier(alert.relevance_score)
    return (
        f"{prefix} FL Rules Monitor: {cat} — "
        f"{_display_summary(alert.summary)[:110]}... "
        f"Details: {notice_url}\n"
        f"Reply STOP to unsubscribe."
    )


def _log_alert_to_file(alert: Alert, notice_url: str):
    """Append alert to a local JSON log file for POC review."""
    entry = {
        "notice_id": alert.notice_id,
        "category": alert.category,
        "score": alert.relevance_score,
        "summary": alert.summary,
        "keywords": alert.matched_keywords,
        "url": notice_url,
        "created_at": str(alert.created_at),
    }

    existing = []
    if ALERTS_LOG.exists():
        try:
            existing = json.loads(ALERTS_LOG.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    existing.append(entry)
    ALERTS_LOG.write_text(json.dumps(existing, indent=2))
    log.info("alert_logged_to_file", path=str(ALERTS_LOG), notice_id=alert.notice_id)


async def send_manage_link_email(email: str, name: str, manage_url: str) -> bool:
    """Send a magic-link email so an existing subscriber can access their settings."""
    if not email:
        return False
    greeting = f"Hi {name},\n\n" if name else "Hi,\n\n"
    body = (
        f"{greeting}"
        "You requested a link to manage your FL Rules Monitor subscription.\n\n"
        f"Manage your preferences: {manage_url}\n\n"
        "From this page you can:\n"
        "  - Change your alert categories\n"
        "  - Switch between email and SMS\n"
        "  - Update your phone number\n"
        "  - Unsubscribe entirely\n\n"
        "If you did not request this email, you can safely ignore it.\n\n"
        "— FL Rules Monitor"
    )
    return _send_email(email, "Manage your FL Rules Monitor subscription", body)


async def send_opt_in_email(email: str, name: str = "", unsubscribe_token: str = "") -> bool:
    """Send a welcome email when a subscriber is added."""
    if not email:
        return False

    greeting = f"Hi {name},\n\n" if name else "Hi,\n\n"
    links_block = ""
    if unsubscribe_token:
        manage_url = f"{settings.app_url}/manage/{unsubscribe_token}"
        unsub_url = f"{settings.app_url}/unsubscribe/{unsubscribe_token}"
        links_block = (
            f"\nManage your preferences (categories, email/SMS): {manage_url}\n"
            f"Unsubscribe from all alerts: {unsub_url}\n"
        )

    body = (
        f"{greeting}"
        "You've been subscribed to FL Rules Monitor — a civil rights alert system "
        "that monitors the Florida Administrative Register for rule changes, "
        "proposed regulations, emergency rules, and meeting notices.\n\n"
        "What to expect:\n"
        "  - Email alerts when relevant notices are published\n"
        "  - Alert frequency varies (typically a few per week)\n\n"
        "You can update your alert categories or switch between email/SMS at "
        "any time using the Manage link below. To stop receiving alerts entirely, "
        "use the Unsubscribe link.\n\n"
        "— FLRules Monitor\n"
        f"{links_block}"
    )

    return _send_email(email, "Welcome to FL Rules Monitor", body)


async def send_email_alert(
    subscriber: Subscriber, alert: Alert, notice_url: str
) -> bool:
    """Send an alert email via Resend or SMTP."""
    if not subscriber.email:
        return False

    unsub_url = ""
    manage_url = ""
    if subscriber.unsubscribe_token:
        unsub_url = f"{settings.app_url}/unsubscribe/{subscriber.unsubscribe_token}"
        manage_url = f"{settings.app_url}/manage/{subscriber.unsubscribe_token}"

    tier_label, _, _ = _score_tier(alert.relevance_score)
    subject = f"[{tier_label}] FL Register Alert: {alert.category.replace('_', ' ').title()}"
    body = _build_alert_body(alert, notice_url, unsub_url, manage_url)
    html = _build_alert_html(alert, notice_url, unsub_url, manage_url)
    return _send_email(subscriber.email, subject, body, html=html)


async def send_sms_alert(
    subscriber: Subscriber, alert: Alert, notice_url: str
) -> bool:
    """Send an SMS alert via Twilio."""
    client = _twilio_client()
    if not client or not subscriber.phone:
        log.warning(
            "sms_skipped",
            reason="no Twilio config or phone",
            subscriber_id=subscriber.id,
        )
        return False

    try:
        client.messages.create(
            body=_build_sms_body(alert, notice_url),
            from_=settings.twilio_from_number,
            to=subscriber.phone,
        )
        log.info("sms_sent", to=subscriber.phone, alert_id=alert.id)
        return True
    except Exception as e:
        log.error("sms_failed", to=subscriber.phone, error=str(e))
        return False


async def send_opt_in_confirmation(phone: str) -> bool:
    """Send opt-in confirmation SMS when a subscriber signs up.

    Required by Twilio/TCPA compliance: subscribers must receive a
    confirmation message that identifies the sender, describes the
    service, and explains how to opt out.
    """
    client = _twilio_client()
    if not client:
        log.warning("opt_in_sms_skipped", reason="no Twilio config")
        return False

    try:
        client.messages.create(
            body=(
                "FL Rules Monitor: You've opted in to receive FL Administrative "
                "Register alerts via SMS. Msg frequency varies. Msg & data rates "
                "may apply. Reply HELP for help, STOP to cancel."
            ),
            from_=settings.twilio_from_number,
            to=phone,
        )
        log.info("opt_in_sms_sent", to=phone)
        return True
    except Exception as e:
        log.error("opt_in_sms_failed", to=phone, error=str(e))
        return False


async def notify_admins_new_subscriber(subscriber: Subscriber) -> dict:
    """Notify configured operators each time a new subscriber signs up.

    Off unless ADMIN_NOTIFY_EMAILS and/or ADMIN_NOTIFY_PHONES is set. Best-effort:
    delivery failures log and proceed; this is observability, not a critical path.
    """
    stats = {"email_sent": 0, "sms_sent": 0}

    contact = subscriber.email or subscriber.phone or "unknown"
    name_part = f" ({subscriber.name})" if subscriber.name else ""
    channels = []
    if subscriber.notify_email and subscriber.email:
        channels.append(f"email -> {subscriber.email}")
    if subscriber.notify_sms and subscriber.phone:
        channels.append(f"sms -> {subscriber.phone}")
    channels_str = ", ".join(channels) or "none"

    subject = f"[FL Rules Monitor] New subscriber: {contact}"
    body = (
        "A new subscriber just signed up to FL Rules Monitor.\n\n"
        f"Contact: {contact}{name_part}\n"
        f"Channels: {channels_str}\n"
        f"Categories: {subscriber.categories}\n"
    )

    for raw in (settings.admin_notify_emails or "").split(","):
        email = raw.strip()
        if email and _send_email(email, subject, body):
            stats["email_sent"] += 1

    sms_body = (
        f"FL Rules Monitor: New subscriber {contact}{name_part}, "
        f"categories: {subscriber.categories}"
    )[:320]
    client = _twilio_client()
    if client and settings.admin_notify_phones:
        for raw in settings.admin_notify_phones.split(","):
            phone = raw.strip()
            if not phone:
                continue
            try:
                client.messages.create(
                    body=sms_body,
                    from_=settings.twilio_from_number,
                    to=phone,
                )
                stats["sms_sent"] += 1
                log.info("admin_notify_sms_sent", to=phone)
            except Exception as e:
                log.error("admin_notify_sms_failed", to=phone, error=str(e))

    log.info("admin_notify_new_subscriber", contact=contact, stats=stats)
    return stats


async def notify_subscribers(
    subscribers: list[Subscriber], alert: Alert, notice_url: str
) -> dict:
    """Send alert to all matching subscribers. Returns delivery stats."""
    stats = {
        "email_sent": 0,
        "email_failed": 0,
        "sms_sent": 0,
        "sms_failed": 0,
    }

    # Always log to file for POC review
    _log_alert_to_file(alert, notice_url)

    alert_categories = set(alert.category.split(","))

    for sub in subscribers:
        if not sub.active:
            continue

        sub_categories = set(sub.categories.split(","))
        if "all" not in sub_categories and not sub_categories & alert_categories:
            continue

        if sub.notify_email and sub.email:
            ok = await send_email_alert(sub, alert, notice_url)
            stats["email_sent" if ok else "email_failed"] += 1

        if sub.notify_sms and sub.phone:
            ok = await send_sms_alert(sub, alert, notice_url)
            stats["sms_sent" if ok else "sms_failed"] += 1

    log.info("notification_batch_complete", alert_id=alert.id, stats=stats)
    return stats
