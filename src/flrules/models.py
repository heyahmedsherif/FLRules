"""Database models — SQLModel (SQLAlchemy + Pydantic hybrid)."""

from datetime import datetime

from sqlmodel import Field, SQLModel


class FARIssue(SQLModel, table=True):
    """A single FAR daily publication."""

    __tablename__ = "far_issues"

    id: int | None = Field(default=None, primary_key=True)
    iid: int = Field(unique=True, index=True, description="flrules.org issue ID")
    volume: str = ""
    publish_date: str = ""
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


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
