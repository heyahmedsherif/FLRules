"""
Notification system — SMS via Twilio, email via Resend (or SMTP fallback).

Email: Resend API (works on Railway, any cloud). Falls back to SMTP if no API key.
SMS: Twilio (pay-as-you-go, requires toll-free verification).
Alerts are also logged to a local JSON file for audit.
"""

import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import resend
import structlog
from twilio.rest import Client as TwilioClient

from flrules.config import settings
from flrules.models import Alert, Subscriber

log = structlog.get_logger()


def _send_email(to: str, subject: str, body: str) -> bool:
    """Send an email via Resend API (primary) or SMTP (fallback)."""
    if settings.resend_api_key:
        try:
            resend.api_key = settings.resend_api_key
            resend.Emails.send({
                "from": settings.from_email,
                "to": [to],
                "reply_to": "contact@gearnerd.io",
                "subject": subject,
                "text": body,
            })
            log.info("email_sent_resend", to=to, subject=subject)
            return True
        except Exception as e:
            log.error("email_failed_resend", to=to, error=str(e))
            return False

    if settings.smtp_host and settings.smtp_user and settings.smtp_password:
        try:
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

ALERTS_LOG = Path(settings.database_url.split("///")[-1]).parent / "alerts_log.json"


def _twilio_client() -> TwilioClient | None:
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    return None


def _build_alert_body(alert: Alert, notice_url: str, unsubscribe_url: str = "") -> str:
    """Build a plain-language alert body."""
    unsub_line = f"\nUnsubscribe: {unsubscribe_url}\n" if unsubscribe_url else ""
    return f"""Florida Administrative Register Alert

Category: {alert.category.replace('_', ' ').title()}
Relevance Score: {alert.relevance_score:.1f}

Summary:
{alert.summary}

Matched Keywords: {alert.matched_keywords}

View the full notice:
{notice_url}

---
What this means: This notice was flagged because it contains language related to
{alert.category.replace('_', ' ')}. We recommend reviewing the full text to assess
whether it may affect your community or requires advocacy action.
{unsub_line}"""


def _build_sms_body(alert: Alert, notice_url: str) -> str:
    """Build a concise SMS alert."""
    cat = alert.category.replace("_", " ").title()
    return (
        f"FL Register Alert [{cat}]: "
        f"{alert.summary[:120]}... "
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


async def send_opt_in_email(email: str, name: str = "", unsubscribe_token: str = "") -> bool:
    """Send a welcome email when a subscriber is added."""
    if not email:
        return False

    greeting = f"Hi {name},\n\n" if name else "Hi,\n\n"
    unsub_line = ""
    if unsubscribe_token:
        unsub_url = f"{settings.app_url}/unsubscribe/{unsubscribe_token}"
        unsub_line = f"\nUnsubscribe: {unsub_url}\n"

    body = (
        f"{greeting}"
        "You've been subscribed to FL Rules Monitor — a civil rights alert system "
        "that monitors the Florida Administrative Register for rule changes, "
        "proposed regulations, emergency rules, and meeting notices.\n\n"
        "What to expect:\n"
        "  - Email alerts when relevant notices are published\n"
        "  - Alert frequency varies (typically a few per week)\n\n"
        "If you did not expect this message or wish to unsubscribe, "
        "click the link below.\n\n"
        "— FLRules Monitor\n"
        f"{unsub_line}"
    )

    return _send_email(email, "Welcome to FL Rules Monitor", body)


async def send_email_alert(
    subscriber: Subscriber, alert: Alert, notice_url: str
) -> bool:
    """Send an alert email via Resend or SMTP."""
    if not subscriber.email:
        return False

    unsub_url = ""
    if subscriber.unsubscribe_token:
        unsub_url = f"{settings.app_url}/unsubscribe/{subscriber.unsubscribe_token}"

    subject = f"FL Register Alert: {alert.category.replace('_', ' ').title()}"
    body = _build_alert_body(alert, notice_url, unsub_url)
    return _send_email(subscriber.email, subject, body)


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
