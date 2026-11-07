"""The URL-capture pipeline shared by ``newsfeed add`` and the server's ``/api/add``.

Split in two so the server can run the slow part (fetch, render, archive, LLM
scoring + summary) off its write lock and take the lock only for :func:`store`.
The CLI simply runs both back to back (ADR 0005).
"""
import asyncio
import sqlite3
from datetime import date

from . import ingest, library
from .archiver import archive_email
from .config import Paths
from .feedback import select_examples_from_rows
from .llm import LLMBackend
from .models import Preferences, ScoredEmail
from .scorer import score_emails
from .summarizer import summarize_articles


async def prepare(
    url: str,
    p: Paths,
    preferences: Preferences,
    backend: LLMBackend,
    calibration_rows: list,
) -> tuple[ScoredEmail, date]:
    """Fetch, archive, score, tag and paragraph-summarize ``url``. No DB writes.

    Raises ``ValueError`` when nothing extractable could be fetched.
    """
    email = await ingest.fetch_url(url)
    target_date = email.date.date()
    email.archive_path = await asyncio.to_thread(archive_email, email, target_date, p.archive)

    examples = select_examples_from_rows(calibration_rows)
    scored = (await score_emails([email], preferences, backend, examples))[0]
    await summarize_articles([scored], "paragraph", backend)
    return scored, target_date


def store(conn: sqlite3.Connection, scored: ScoredEmail, target_date: date) -> None:
    """Upsert a prepared capture into the Library, with its body, auto-starred."""
    email = scored.email
    library.upsert_scored(conn, scored, target_date)
    if email.body:
        library.set_body(conn, email.message_id, email.body)
    library.set_star(conn, email.message_id, True)
