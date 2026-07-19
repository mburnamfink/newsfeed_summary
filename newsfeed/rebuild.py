"""Re-render digest HTML from the Library, without re-fetching or re-scoring.

Every field the digest template needs already lives in ``articles.db`` after
migration + retag, so an old digest can be regenerated into the current format
(tag chips, star/reaction/read controls) purely from stored state. The one thing
not stored is the original "generated at" time, which is recovered from the
existing digest file so the rebuilt page keeps its authentic timestamp.
"""
import logging
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from . import library
from .library import Article
from .models import Email, ScoredEmail
from .renderer import render_digest

logger = logging.getLogger(__name__)

_GENERATED_RE = re.compile(r"generated\s+(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)


def _to_scored(a: Article) -> ScoredEmail:
    email = Email(
        message_id=a.message_id,
        sender_name=a.sender_name,
        sender_email=a.sender_email,
        subject=a.subject,
        date=datetime.fromisoformat(a.date) if a.date else datetime.min,
        body="",
        archive_path=a.archive_path,
        paywalled=a.paywalled,
    )
    return ScoredEmail(
        email=email,
        interest_score=a.score,
        topic=a.topic,
        one_line=a.one_line,
        summary=a.summary,
        tags=a.tags,
    )


def _original_generated_at(digest_path: Path) -> str | None:
    """Recover the ``generated HH:MM AM`` time from an existing digest page."""
    if not digest_path.exists():
        return None
    m = _GENERATED_RE.search(digest_path.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else None


def rebuild_date(conn: sqlite3.Connection, day: str, digests_dir: Path) -> int:
    """Re-render one day's digest from the Library. Returns items rendered."""
    articles = library.list_by_date(conn, day)
    if not articles:
        return 0
    generated_at = _original_generated_at(digests_dir / f"{day}.html")
    render_digest(
        [_to_scored(a) for a in articles],
        date.fromisoformat(day),
        digests_dir,
        generated_at=generated_at,
    )
    return len(articles)


def rebuild_all(conn: sqlite3.Connection, digests_dir: Path, since: str | None = None) -> int:
    """Re-render every stored date (optionally ``date >= since``). Returns total items."""
    total = 0
    for day in library.all_dates(conn):
        if since and day < since:
            continue
        n = rebuild_date(conn, day, digests_dir)
        if n:
            logger.info(f"Rebuilt {day} ({n} items)")
            total += n
    return total
