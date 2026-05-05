"""Application configuration via environment variables."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'flrules.db'}"

    # Polling
    poll_interval_minutes: int = 60

    # Email — Resend API (primary) or SMTP fallback
    resend_api_key: str = ""  # from resend.com
    from_email: str = "alerts@gearnerd.io"

    # SMTP fallback (used by GitHub Actions where Resend isn't needed)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # SMS — Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    app_url: str = "http://localhost:8000"  # base URL for OAuth redirect
    session_secret: str = "change-me-in-production"  # signs session cookies

    # Admin — comma-separated list of Google emails that get admin role
    admin_emails: str = "Ahmed.Sherif@hey.com"

    # Backup — long random token used by the daily backup workflow to fetch the DB
    backup_token: str = ""

    # Logging
    log_level: str = "INFO"

    # FAR site
    far_base_url: str = "https://flrules.org"
    user_agent: str = Field(
        default="FLRulesMonitor/0.1 (civic-tech; contact: admin@yourdomain.org)"
    )
    request_delay_seconds: float = 2.0  # polite crawl delay

    # Wayback Machine archiving — independent witness for tamper detection.
    # Default off so existing deployments are unchanged; flip to True in .env or
    # the GitHub Actions workflow env to enable.
    archive_enabled: bool = False
    # Cap concurrent archive submissions per pipeline run. Save Page Now allows
    # ~15/min per IP; we stay well under that with a small concurrency limit.
    archive_max_per_run: int = 30

    # Disappearance detection — re-scrape recently-known issues and alert if
    # any previously-stored notice is missing from the live site. Rate-limited
    # by verify_interval_hours so we don't hammer flrules.org. Default off.
    verify_disappearances: bool = False
    verify_recent_issues: int = 1  # how many of the most recent issues to re-check
    verify_interval_hours: int = 24  # don't re-verify the same issue more often


settings = Settings()
