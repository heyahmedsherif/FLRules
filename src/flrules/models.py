"""Database models — SQLModel (SQLAlchemy + Pydantic hybrid)."""

from datetime import datetime

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    """A user who can log in via Google OAuth."""

    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    google_id: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    name: str = ""
    picture: str = ""
    role: str = "member"  # "admin" or "member"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: datetime = Field(default_factory=datetime.utcnow)


class FARIssue(SQLModel, table=True):
    """A single FAR daily publication."""

    __tablename__ = "far_issues"

    id: int | None = Field(default=None, primary_key=True)
    iid: int = Field(unique=True, index=True, description="flrules.org issue ID")
    volume: str = ""
    publish_date: str = ""
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    # Most recent disappearance-verification check. Used to rate-limit the
    # re-scrape so we don't hammer flrules.org with redundant section reads.
    last_verified_at: datetime | None = None


class FARNotice(SQLModel, table=True):
    """An individual notice within a FAR issue section."""

    __tablename__ = "far_notices"

    id: int | None = Field(default=None, primary_key=True)
    notice_id: int = Field(unique=True, index=True, description="flrules.org notice ID")
    issue_iid: int = Field(index=True)
    section_number: int
    section_name: str = ""
    agency_code: str = ""
    description: str = ""
    full_text: str = ""
    publish_date: str = ""
    url: str = ""
    fetched_at: datetime = Field(default_factory=datetime.utcnow)

    # Independent witness via the Internet Archive Wayback Machine. Empty when the
    # archive feature is disabled or the submission failed — readers must tolerate
    # blank values. tamper_detected is set true only after a successful comparison
    # finds the snapshot's text diverging from what we scraped.
    wayback_url: str = ""
    wayback_timestamp: str = ""
    tamper_detected: bool = False

    # Cryptographic provenance. content_hash is over the substantive observable
    # content of the notice (see provenance.compute_content_hash). chain_hash links
    # this notice to every prior one — tampering with any earlier notice
    # invalidates every subsequent chain_hash. prev_chain_hash points at the
    # chain head as it stood when this notice was inserted.
    content_hash: str = ""
    chain_hash: str = ""
    prev_chain_hash: str = ""


class Alert(SQLModel, table=True):
    """A relevance-matched alert generated from a notice."""

    __tablename__ = "alerts"

    id: int | None = Field(default=None, primary_key=True)
    notice_id: int = Field(index=True)
    matched_keywords: str = ""  # comma-separated
    relevance_score: float = 0.0
    category: str = ""  # e.g. "domestic_terrorism", "cabinet_meeting", "civil_rights"
    summary: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    notified: bool = False


class Subscriber(SQLModel, table=True):
    """A member who has opted in to receive alerts."""

    __tablename__ = "subscribers"

    id: int | None = Field(default=None, primary_key=True)
    email: str | None = None
    phone: str | None = None
    name: str = ""
    notify_email: bool = True
    notify_sms: bool = False
    categories: str = "all"  # comma-separated categories or "all"
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    unsubscribe_token: str = Field(default="")
