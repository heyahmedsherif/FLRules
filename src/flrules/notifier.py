"""
Notification system — SMS via Twilio, email via free SMTP.

POC: Alerts are also logged to console and written to a local JSON file.
     Email via Gmail app password (free). SMS via Twilio (pay-as-you-go).
"""

import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import structlog
from twilio.rest import Client as TwilioClient

from flrules.config import settings
from flrules.models import Alert, Subscriber

log = structlog.get_logger()

ALERTS_LOG = Path(settings.database_url.split("///")[-1]).parent / "alerts_log.json"


def _twilio_client() -> TwilioClient | None:
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    return None


def _build_alert_body(alert: Alert, notice_url: str) -> str:
    """Build a plain-language alert body."""
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
"""


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


async def send_email_alert(
    subscriber: Subscriber, alert: Alert, notice_url: str
) -> bool:
    """Send email via free SMTP (Gmail app password) or log to console."""
    if not subscriber.email:
        return False

    subject = f"FL Register Alert: {alert.category.replace('_', ' ').title()}"
    body = _build_alert_body(alert, notice_url)

    if settings.smtp_host and settings.smtp_user and settings.smtp_password:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = settings.from_email
            msg["To"] = subscriber.email

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)

            log.info("email_sent", to=subscriber.email, alert_id=alert.id)
            return True
        except Exception as e:
            log.error("email_failed", to=subscriber.email, error=str(e))
            return False

    # Fallback: log to console
    log.info(
        "poc_email_alert",
        to=subscriber.email,
        subject=subject,
        category=alert.category,
        score=alert.relevance_score,
        url=notice_url,
    )
    return True


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
