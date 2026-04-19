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

    # Logging
    log_level: str = "INFO"

    # FAR site
    far_base_url: str = "https://flrules.org"
    user_agent: str = Field(
        default="FLRulesMonitor/0.1 (civic-tech; contact: admin@yourdomain.org)"
    )
    request_delay_seconds: float = 2.0  # polite crawl delay


settings = Settings()
