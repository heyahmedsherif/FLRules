"""
Notification system — sends email and SMS alerts to opted-in subscribers.

Uses:
  - Resend (https://resend.com) for email — generous free tier, simple API
  - Twilio for SMS — pay-as-you-go, reliable
"""

import resend
import structlog
from twilio.rest import Client as TwilioClient

from flrules.config import settings
from flrules.models import Alert, Subscriber

log = structlog.get_logger()


def _init_resend():
    if settings.resend_api_key:
        resend.api_key = settings.resend_api_key


def _twilio_client() -> TwilioClient | None:
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    return None


def _build_email_body(alert: Alert, notice_url: str) -> str:
    """Build a plain-language email body for an alert."""
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

---
You are receiving this because you opted in to FLRules Monitor alerts.
To unsubscribe, reply STOP or contact your organization admin.
"""


def _build_sms_body(alert: Alert, notice_url: str) -> str:
    """Build a concise SMS alert."""
    cat = alert.category.replace("_", " ").title()
    return (
        f"FL Admin Register Alert [{cat}]: "
        f"{alert.summary[:120]}... "
        f"Details: {notice_url}"
    )


async def send_email_alert(subscriber: Subscriber, alert: Alert, notice_url: str) -> bool:
    """Send an email alert to a single subscriber."""
    _init_resend()
    if not settings.resend_api_key or not subscriber.email:
        log.warning("email_skipped", reason="no API key or email", subscriber_id=subscriber.id)
        return False

    try:
        resend.Emails.send(
            {
                "from": settings.from_email,
                "to": subscriber.email,
                "subject": f"FL Register Alert: {alert.category.replace('_', ' ').title()}",
                "text": _build_email_body(alert, notice_url),
            }
        )
        log.info("email_sent", to=subscriber.email, alert_id=alert.id)
        return True
    except Exception as e:
        log.error("email_failed", to=subscriber.email, error=str(e))
        return False


async def send_sms_alert(subscriber: Subscriber, alert: Alert, notice_url: str) -> bool:
    """Send an SMS alert to a single subscriber."""
    client = _twilio_client()
    if not client or not subscriber.phone:
        log.warning("sms_skipped", reason="no Twilio config or phone", subscriber_id=subscriber.id)
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


async def notify_subscribers(
    subscribers: list[Subscriber], alert: Alert, notice_url: str
) -> dict:
    """Send alert to all matching subscribers. Returns delivery stats."""
    stats = {"email_sent": 0, "email_failed": 0, "sms_sent": 0, "sms_failed": 0}

    alert_categories = set(alert.category.split(","))

    for sub in subscribers:
        if not sub.active:
            continue

        # Check category match
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
