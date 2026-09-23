"""``articles.db`` — the SQLite source of truth for the Library (ADR 0002).

One row per article, keyed by the stable Gmail ``message_id``. All per-article
state lives here: score, summary, star, read, feedback, ``archive_path``, the
machine-assigned tags, the reader's tag corrections, and the body text (indexed
with FTS5 for keyword search). ``feedback.yaml`` and ``read_state.json`` are
retired as write targets.

Effective tags for an article are **derived, never stored flat**:
``(LLM tags ∪ reader-added) − reader-removed``. The pipeline recomputes the LLM
tags on every run/retag; the reader's add/remove corrections are a durable
overlay re-applied on top, so hand-fixes survive retagging while newly-added
vocabulary terms still flow into old articles.

Functions take an open ``sqlite3.Connection`` so callers control the transaction
boundary (and tests can use ``:memory:``). The server serialises writes under its
own lock; SQLite's own locking guards the CLI/pipeline path.
"""
import sqlite3
from dataclasses import dataclass, field

from .reading import reading_minutes as _reading_minutes
from datetime import date, datetime, timezone
from pathlib import Path

from .models import ScoredEmail

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    message_id   TEXT PRIMARY KEY,
    date         TEXT,
    sender_name  TEXT,
    sender_email TEXT,
    subject      TEXT,
    one_line     TEXT,
    summary      TEXT DEFAULT '',
    topic        TEXT DEFAULT '',
    score        REAL,
    archive_path TEXT DEFAULT '',
    paywalled    INTEGER DEFAULT 0,
    starred      INTEGER DEFAULT 0,
    read         INTEGER DEFAULT 0,
    feedback     TEXT,
    url          TEXT DEFAULT '',
    source       TEXT DEFAULT 'gmail'
);

CREATE INDEX IF NOT EXISTS idx_articles_sender ON articles(sender_name);
CREATE INDEX IF NOT EXISTS idx_articles_date   ON articles(date);

-- Machine-assigned tags, recomputed wholesale on every upsert/retag.
CREATE TABLE IF NOT EXISTS article_llm_tags (
    message_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (message_id, tag)
);

-- The reader's durable tag overlay. op is 'add' or 'remove'; a tag can carry at
-- most one op per article (the primary key enforces it), so add and remove can
-- never contradict each other for the same tag.
CREATE TABLE IF NOT EXISTS article_tag_delta (
    message_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    op         TEXT NOT NULL CHECK (op IN ('add', 'remove')),
    PRIMARY KEY (message_id, tag)
);

-- Effective tags = (llm ∪ delta.add) − delta.remove, derived here so no code
-- path can accidentally persist a stale flattened set.
CREATE VIEW IF NOT EXISTS effective_tags AS
    SELECT message_id, tag FROM article_llm_tags t
    WHERE NOT EXISTS (
        SELECT 1 FROM article_tag_delta d
        WHERE d.message_id = t.message_id AND d.tag = t.tag AND d.op = 'remove'
    )
    UNION
    SELECT message_id, tag FROM article_tag_delta WHERE op = 'add';

-- Standalone FTS index over body text; message_id is carried unindexed so a
-- keyword hit maps straight back to an articles row.
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    body,
    message_id UNINDEXED
);

-- On-demand long-form summaries (ADR 0007). One per article, keyed by message_id;
-- deliberately its own table so a digest re-run's upsert (which overwrites the
-- pipeline-derived articles columns) can never clobber a summary. source records
-- whether the summary was written from the stored body or from full text
-- re-fetched via the article's URL (the teaser case).
CREATE TABLE IF NOT EXISTS article_summaries (
    message_id TEXT PRIMARY KEY,
    summary_md TEXT NOT NULL,
    model      TEXT NOT NULL,
    source     TEXT NOT NULL,
    word_count INTEGER,
    created_at TEXT NOT NULL
);
"""


@dataclass
class Article:
    """A Library row shaped for browse/search rendering.

    ``tags`` is the effective set; ``summary`` falls back to ``one_line`` for
    low-tier articles that were never given a paragraph summary.
    """

    message_id: str
    date: str
    sender_name: str
    sender_email: str
    subject: str
    one_line: str
    summary: str
    topic: str
    score: float
    archive_path: str
    paywalled: bool
    starred: bool
    read: bool
    feedback: str | None
    url: str = ""
    source: str = "gmail"
    tags: list[str] = field(default_factory=list)
    # Whether a long-form summary (ADR 0007) exists for this article. Populated on
    # read so every card can show the right Summarize/Read-summary control.
    has_summary: bool = False
    # Word-count-based reading estimate of the stored body, 0 when no body is held.
    reading_minutes: int = 0
    dismissed: bool = False

    @property
    def display_summary(self) -> str:
        return self.summary or self.one_line

    @property
    def tier(self) -> str:
        if self.score is None:
            return "low"
        if self.score >= 7:
            return "high"
        if self.score >= 4:
            return "medium"
        return "low"


# Columns added to `articles` after the original schema shipped. CREATE TABLE
# IF NOT EXISTS leaves an existing table untouched, so these are applied on open.
_ADDED_COLUMNS = {
    "url": "TEXT DEFAULT ''",
    "source": "TEXT DEFAULT 'gmail'",
    # Reader "Ignore" (ADR 0008): out of the queue without being read or rated.
    "dismissed": "INTEGER DEFAULT 0",
}


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open ``articles.db`` (creating it and the schema if absent)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add columns introduced after the first schema (ALTER-on-open)."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    for name, decl in _ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {decl}")


# --- writes -----------------------------------------------------------------


def upsert_article(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    date: str,
    sender_name: str,
    sender_email: str = "",
    subject: str = "",
    one_line: str = "",
    summary: str = "",
    topic: str = "",
    score: float | None = None,
    archive_path: str = "",
    paywalled: bool = False,
    url: str = "",
    source: str = "gmail",
) -> None:
    """Insert or update the article row, preserving reader-owned state.

    ``starred``, ``read``, ``feedback``, ``dismissed`` and the ``article_tag_delta`` overlay are
    never overwritten here — only the pipeline-derived fields are. Re-running a
    day's digest therefore refreshes scores/summaries without clobbering the
    reader's stars, reactions and tag fixes.
    """
    conn.execute(
        """
        INSERT INTO articles
            (message_id, date, sender_name, sender_email, subject, one_line,
             summary, topic, score, archive_path, paywalled, url, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            date         = excluded.date,
            sender_name  = excluded.sender_name,
            sender_email = excluded.sender_email,
            subject      = excluded.subject,
            one_line     = excluded.one_line,
            summary      = excluded.summary,
            topic        = excluded.topic,
            score        = excluded.score,
            archive_path = excluded.archive_path,
            paywalled    = excluded.paywalled,
            url          = excluded.url,
            source       = excluded.source
        """,
        (
            message_id, date, sender_name, sender_email, subject, one_line,
            summary, topic, score, archive_path, int(paywalled), url, source,
        ),
    )


def upsert_scored(conn: sqlite3.Connection, scored: ScoredEmail, target_date: date) -> None:
    """Upsert one pipeline result and replace its machine tags in a single step."""
    e = scored.email
    upsert_article(
        conn,
        message_id=e.message_id,
        date=target_date.isoformat(),
        sender_name=e.sender_name,
        sender_email=e.sender_email,
        subject=e.subject,
        one_line=scored.one_line,
        summary=scored.summary,
        topic=scored.topic,
        score=scored.interest_score,
        archive_path=e.archive_path,
        paywalled=e.paywalled,
        url=e.url,
        source=e.source,
    )
    set_llm_tags(conn, e.message_id, scored.tags)


def set_llm_tags(conn: sqlite3.Connection, message_id: str, tags: list[str]) -> None:
    """Replace the machine-assigned tag set for an article (delta is untouched)."""
    conn.execute("DELETE FROM article_llm_tags WHERE message_id = ?", (message_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO article_llm_tags (message_id, tag) VALUES (?, ?)",
        [(message_id, tag) for tag in dict.fromkeys(tags)],
    )


def set_body(conn: sqlite3.Connection, message_id: str, body: str) -> None:
    """Set (replace) the FTS body text for an article."""
    conn.execute("DELETE FROM articles_fts WHERE message_id = ?", (message_id,))
    conn.execute(
        "INSERT INTO articles_fts (body, message_id) VALUES (?, ?)",
        (body, message_id),
    )


def set_summary(
    conn: sqlite3.Connection,
    message_id: str,
    summary_md: str,
    *,
    model: str,
    source: str,
    word_count: int,
) -> None:
    """Store (or replace) the long-form summary for an article (ADR 0007)."""
    conn.execute(
        """
        INSERT INTO article_summaries
            (message_id, summary_md, model, source, word_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            summary_md = excluded.summary_md,
            model      = excluded.model,
            source     = excluded.source,
            word_count = excluded.word_count,
            created_at = excluded.created_at
        """,
        (
            message_id, summary_md, model, source, word_count,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def get_summary(conn: sqlite3.Connection, message_id: str) -> dict | None:
    """The stored long-form summary and its provenance, or None."""
    row = conn.execute(
        "SELECT summary_md, model, source, word_count, created_at "
        "FROM article_summaries WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return dict(row) if row else None


def get_body(conn: sqlite3.Connection, message_id: str) -> str:
    """The indexed body text for an article, or empty string if none stored."""
    row = conn.execute(
        "SELECT body FROM articles_fts WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row["body"] if row else ""


def set_star(conn: sqlite3.Connection, message_id: str, starred: bool) -> None:
    conn.execute(
        "UPDATE articles SET starred = ? WHERE message_id = ?",
        (int(starred), message_id),
    )


def set_read(conn: sqlite3.Connection, message_id: str, read: bool) -> None:
    conn.execute(
        "UPDATE articles SET read = ? WHERE message_id = ?",
        (int(read), message_id),
    )


def set_dismissed(conn: sqlite3.Connection, message_ids: list[str], dismissed: bool) -> None:
    """Ignore (or un-ignore) articles in the Reader (ADR 0008).

    Deliberately touches neither ``read`` nor ``feedback``: ignoring is not a
    judgement on the score, so it must never reach scorer calibration.
    """
    conn.executemany(
        "UPDATE articles SET dismissed = ? WHERE message_id = ?",
        [(int(dismissed), mid) for mid in message_ids],
    )


def dismiss_backlog(conn: sqlite3.Connection, before: str, max_score: float) -> list[str]:
    """Ignore every queued article dated before ``before`` scoring under ``max_score``.

    Returns the affected ids so the caller can offer an undo. Unscored rows count
    as low.
    """
    rows = conn.execute(
        f"SELECT message_id FROM articles WHERE {_QUEUED} "
        "AND date < ? AND COALESCE(score, 0) < ?",
        (before, max_score),
    ).fetchall()
    ids = [r["message_id"] for r in rows]
    set_dismissed(conn, ids, True)
    return ids


def set_feedback(
    conn: sqlite3.Connection, message_id: str, feedback: str | None, *, read: bool | None = None
) -> None:
    """Record a reaction; ``read`` (when given) is set in the same statement.

    Mirrors the digest one-tap: any reaction marks the item read, clearing it
    (``feedback=None``) un-reads it.
    """
    if read is None:
        conn.execute(
            "UPDATE articles SET feedback = ? WHERE message_id = ?",
            (feedback, message_id),
        )
    else:
        conn.execute(
            "UPDATE articles SET feedback = ?, read = ? WHERE message_id = ?",
            (feedback, int(read), message_id),
        )


def apply_tag_delta(conn: sqlite3.Connection, message_id: str, tag: str, op: str) -> None:
    """Update the reader's tag overlay for one (article, tag).

    ``op`` is ``add`` | ``remove`` | ``clear``. ``clear`` drops the reader's
    delta for that tag, reverting to whatever the LLM decided.
    """
    if op == "clear":
        conn.execute(
            "DELETE FROM article_tag_delta WHERE message_id = ? AND tag = ?",
            (message_id, tag),
        )
        return
    if op not in ("add", "remove"):
        raise ValueError(f"tag op must be add, remove or clear; got {op!r}")
    conn.execute(
        """
        INSERT INTO article_tag_delta (message_id, tag, op) VALUES (?, ?, ?)
        ON CONFLICT(message_id, tag) DO UPDATE SET op = excluded.op
        """,
        (message_id, tag, op),
    )


# --- reads ------------------------------------------------------------------


def effective_tags(conn: sqlite3.Connection, message_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM effective_tags WHERE message_id = ? ORDER BY tag",
        (message_id,),
    ).fetchall()
    return [r["tag"] for r in rows]


def _tags_by_message(conn: sqlite3.Connection, message_ids: list[str]) -> dict[str, list[str]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT message_id, tag FROM effective_tags "
        f"WHERE message_id IN ({placeholders}) ORDER BY tag",
        message_ids,
    ).fetchall()
    out: dict[str, list[str]] = {mid: [] for mid in message_ids}
    for r in rows:
        out[r["message_id"]].append(r["tag"])
    return out


def _reading_minutes_by_message(
    conn: sqlite3.Connection, message_ids: list[str]
) -> dict[str, int]:
    """Reading estimate per article, derived from the stored FTS body on read so
    it needs no schema column and covers every existing article for free."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT message_id, body FROM articles_fts WHERE message_id IN ({placeholders})",
        message_ids,
    ).fetchall()
    return {r["message_id"]: _reading_minutes(r["body"] or "") for r in rows}


def _summary_ids(conn: sqlite3.Connection, message_ids: list[str]) -> set[str]:
    """The subset of ``message_ids`` that have a stored long-form summary."""
    if not message_ids:
        return set()
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT message_id FROM article_summaries WHERE message_id IN ({placeholders})",
        message_ids,
    ).fetchall()
    return {r["message_id"] for r in rows}


def _rows_to_articles(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Article]:
    ids = [r["message_id"] for r in rows]
    tags = _tags_by_message(conn, ids)
    summarized = _summary_ids(conn, ids)
    minutes = _reading_minutes_by_message(conn, ids)
    return [
        Article(
            message_id=r["message_id"],
            date=r["date"] or "",
            sender_name=r["sender_name"] or "",
            sender_email=r["sender_email"] or "",
            subject=r["subject"] or "",
            one_line=r["one_line"] or "",
            summary=r["summary"] or "",
            topic=r["topic"] or "",
            score=r["score"] if r["score"] is not None else 0.0,
            archive_path=r["archive_path"] or "",
            paywalled=bool(r["paywalled"]),
            starred=bool(r["starred"]),
            read=bool(r["read"]),
            feedback=r["feedback"],
            url=r["url"] or "",
            source=r["source"] or "gmail",
            tags=tags.get(r["message_id"], []),
            has_summary=r["message_id"] in summarized,
            reading_minutes=minutes.get(r["message_id"], 0),
            dismissed=bool(r["dismissed"]),
        )
        for r in rows
    ]


_SELECT = "SELECT * FROM articles"


def get_article(conn: sqlite3.Connection, message_id: str) -> Article | None:
    rows = conn.execute(f"{_SELECT} WHERE message_id = ?", (message_id,)).fetchall()
    articles = _rows_to_articles(conn, rows)
    return articles[0] if articles else None


def list_by_author(conn: sqlite3.Connection, sender_name: str) -> list[Article]:
    rows = conn.execute(
        f"{_SELECT} WHERE sender_name = ? ORDER BY date DESC, score DESC",
        (sender_name,),
    ).fetchall()
    return _rows_to_articles(conn, rows)


def list_by_tag(conn: sqlite3.Connection, tag: str) -> list[Article]:
    rows = conn.execute(
        f"{_SELECT} a JOIN effective_tags et USING (message_id) "
        f"WHERE et.tag = ? ORDER BY a.date DESC, a.score DESC",
        (tag,),
    ).fetchall()
    return _rows_to_articles(conn, rows)


def list_by_date(conn: sqlite3.Connection, day: str) -> list[Article]:
    rows = conn.execute(
        f"{_SELECT} WHERE date = ? ORDER BY score DESC", (day,)
    ).fetchall()
    return _rows_to_articles(conn, rows)


def all_dates(conn: sqlite3.Connection) -> list[str]:
    """Distinct article dates, oldest first."""
    rows = conn.execute(
        "SELECT DISTINCT date FROM articles WHERE date != '' ORDER BY date"
    ).fetchall()
    return [r["date"] for r in rows]


def list_starred(conn: sqlite3.Connection) -> list[Article]:
    rows = conn.execute(
        f"{_SELECT} WHERE starred = 1 ORDER BY date DESC, score DESC"
    ).fetchall()
    return _rows_to_articles(conn, rows)


def list_saved(conn: sqlite3.Connection) -> list[Article]:
    """Articles ingested from a URL (``newsfeed add``), newest first."""
    rows = conn.execute(
        f"{_SELECT} WHERE source = 'url' ORDER BY date DESC, score DESC"
    ).fetchall()
    return _rows_to_articles(conn, rows)


# An article still waiting in the Reader queue.
_QUEUED = "read = 0 AND COALESCE(dismissed, 0) = 0"


def _date_bounds(on_or_after: str | None, before: str | None) -> tuple[str, list[str]]:
    clauses, params = [], []
    if on_or_after is not None:
        clauses.append("date >= ?")
        params.append(on_or_after)
    if before is not None:
        clauses.append("date < ?")
        params.append(before)
    return "".join(f" AND {c}" for c in clauses), params


def list_unread(
    conn: sqlite3.Connection,
    limit: int = 100,
    *,
    on_or_after: str | None = None,
    before: str | None = None,
) -> list[Article]:
    """The Reader queue (ADR 0006/0008): unread, un-ignored articles, newest first.

    Ordered ``date`` desc then ``score`` desc so a day's must-reads lead that day,
    and capped so the whole unread backlog isn't shipped to the phone at once.
    """
    bounds, params = _date_bounds(on_or_after, before)
    rows = conn.execute(
        f"{_SELECT} WHERE {_QUEUED}{bounds} ORDER BY date DESC, score DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return _rows_to_articles(conn, rows)


def count_unread(
    conn: sqlite3.Connection, *, on_or_after: str | None = None, before: str | None = None
) -> int:
    bounds, params = _date_bounds(on_or_after, before)
    return conn.execute(
        f"SELECT COUNT(*) FROM articles WHERE {_QUEUED}{bounds}", params
    ).fetchone()[0]


def latest_digest_date(conn: sqlite3.Connection) -> str | None:
    """Date of the most recent digest — the Reader's "Today" (ADR 0008).

    Saved URLs are dated when captured, so they would otherwise pull "Today"
    forward past the last newsletter run; they only count when there is no
    Gmail article at all.
    """
    for where in ("source = 'gmail' AND date != ''", "date != ''"):
        row = conn.execute(f"SELECT MAX(date) FROM articles WHERE {where}").fetchone()
        if row[0]:
            return row[0]
    return None


def search(conn: sqlite3.Connection, query: str, limit: int = 200) -> list[Article]:
    """Full-text search over bodies; results newest-first, ranked join to rows."""
    match = _fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        f"""
        {_SELECT} a
        JOIN articles_fts f ON f.message_id = a.message_id
        WHERE articles_fts MATCH ?
        ORDER BY a.date DESC, a.score DESC
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    return _rows_to_articles(conn, rows)


def _fts_query(query: str) -> str:
    """Turn raw user text into a safe FTS5 MATCH expression.

    Each whitespace token is quoted so FTS5 punctuation/operators in user input
    can't raise a syntax error; tokens are AND-ed (all must appear).
    """
    tokens = [t.replace('"', "") for t in query.split()]
    return " ".join(f'"{t}"' for t in tokens if t)


def author_facets(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT sender_name, COUNT(*) AS n FROM articles "
        "GROUP BY sender_name ORDER BY n DESC, sender_name"
    ).fetchall()
    return [(r["sender_name"] or "", r["n"]) for r in rows]


def tag_facets(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT tag, COUNT(*) AS n FROM effective_tags "
        "GROUP BY tag ORDER BY n DESC, tag"
    ).fetchall()
    return [(r["tag"], r["n"]) for r in rows]


def all_message_ids(conn: sqlite3.Connection, since: str | None = None) -> list[str]:
    """Message ids for retag selection, optionally limited to ``date >= since``."""
    if since:
        rows = conn.execute(
            "SELECT message_id FROM articles WHERE date >= ? ORDER BY date", (since,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT message_id FROM articles ORDER BY date").fetchall()
    return [r["message_id"] for r in rows]


def message_ids_with_tag(conn: sqlite3.Connection, tag: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT message_id FROM effective_tags WHERE tag = ?", (tag,)
    ).fetchall()
    return [r["message_id"] for r in rows]


def state_map(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-article read/star/feedback for articles that carry any of them.

    The freshly-rendered digest is built from new scores and knows nothing of
    stored reader state, so it fetches this to light up the right controls. Only
    non-default rows are returned to keep the payload small.
    """
    rows = conn.execute(
        "SELECT a.message_id, a.read, a.starred, a.feedback, a.dismissed, "
        "       (s.message_id IS NOT NULL) AS has_summary "
        "FROM articles a "
        "LEFT JOIN article_summaries s ON s.message_id = a.message_id "
        "WHERE a.read = 1 OR a.starred = 1 OR a.feedback IS NOT NULL OR a.dismissed = 1 "
        "   OR s.message_id IS NOT NULL"
    ).fetchall()
    return {
        r["message_id"]: {
            "read": bool(r["read"]),
            "starred": bool(r["starred"]),
            "feedback": r["feedback"],
            "dismissed": bool(r["dismissed"]),
            "summary": bool(r["has_summary"]),
        }
        for r in rows
    }


def calibration_rows(conn: sqlite3.Connection) -> list[dict]:
    """Feedback history in the shape ``feedback.select_examples_from_rows`` consumes."""
    rows = conn.execute(
        "SELECT date, subject, sender_name AS sender, topic, score, feedback "
        "FROM articles"
    ).fetchall()
    return [dict(r) for r in rows]
