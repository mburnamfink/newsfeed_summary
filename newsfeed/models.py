from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Email:
    message_id: str
    sender_name: str
    sender_email: str
    subject: str
    date: datetime
    body: str
    url: str = ""
    # Full newsletter HTML, kept only long enough to build the Archive.
    raw_html: str = ""
    # Server-root-relative URL of the built Archive (e.g. /archive/2026-06-01/<id>/index.html).
    archive_path: str = ""


@dataclass
class ScoredEmail:
    email: Email
    interest_score: float
    topic: str
    one_line: str
    summary: str = ""

    @property
    def tier(self) -> str:
        if self.interest_score >= 7:
            return "high"
        elif self.interest_score >= 4:
            return "medium"
        return "low"


@dataclass
class Preferences:
    gmail_labels: list[str]
    interests: list[str]
    thresholds: dict[str, int]
    boost_sources: list[str] = field(default_factory=list)
    mute_sources: list[str] = field(default_factory=list)
    boost_keywords: list[str] = field(default_factory=list)
    mute_keywords: list[str] = field(default_factory=list)

    @property
    def high_threshold(self) -> int:
        return self.thresholds.get("high", 7)

    @property
    def medium_threshold(self) -> int:
        return self.thresholds.get("medium", 4)
