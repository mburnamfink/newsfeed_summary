"""One-time backfill of ``articles.db`` from the pre-Library stores (ADR 0002).

Reconstructs the full history from the artifacts already on disk, in the order
each source can feed the next:

1. ``serve/digests/*.html`` — the richest structured source: every card/row
   carries ``message_id``, subject, sender, score, (sometimes) topic, the
   summary, and the archive link. This is the only place ``(subject, sender)``
   maps to a ``message_id``.
2. ``serve/archive/<date>/<id>/index.html`` — body text, stripped and loaded
   into the FTS index.
3. ``feedback.yaml`` — reactions, matched back to a ``message_id`` on
   ``(subject, sender)``.
4. ``read_state.json`` — already ``message_id``-keyed read flags.

Tagging is a separate LLM pass driven by ``newsfeed retag``, not part of this
offline backfill (which touches no network).
"""
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, Tag

from . import library
from .parser import strip_html

logger = logging.getLogger(__name__)


@dataclass
class MigrationStats:
    articles: int = 0
    bodies: int = 0
    feedback_matched: int = 0
    feedback_unmatched: int = 0
    feedback_collisions: int = 0
    read_flags: int = 0
    read_unmatched: int = 0
    digests: int = 0

    def summary(self) -> str:
        return (
            f"{self.articles} articles from {self.digests} digests, "
            f"{self.bodies} bodies indexed, "
            f"{self.feedback_matched} reactions matched "
            f"({self.feedback_unmatched} unmatched, {self.feedback_collisions} collisions), "
            f"{self.read_flags} read flags ({self.read_unmatched} unmatched)"
        )


@dataclass
class ParsedArticle:
    message_id: str
    date: str
    sender_name: str
    subject: str
    text: str        # visible summary (high/medium) or one-liner (low)
    tier: str        # high | medium | low
    topic: str = ""
    score: float | None = None
    archive_path: str = ""
    paywalled: bool = False

    @property
    def summary(self) -> str:
        return self.text if self.tier in ("high", "medium") else ""

    @property
    def one_line(self) -> str:
        return self.text if self.tier == "low" else ""


def parse_digest_html(html: str, digest_date: str) -> list[ParsedArticle]:
    """Extract one ParsedArticle per card/row in a rendered digest page."""
    soup = BeautifulSoup(html, "html.parser")
    articles: list[ParsedArticle] = []
    # Select the item containers directly rather than [data-msgid]: digests from
    # before the data-msgid era carry the message_id only in their archive link,
    # which _parse_item recovers. New digests put data-msgid on these same nodes.
    for el in soup.select(".card, .medium-list > li, .low-list > li"):
        if not isinstance(el, Tag):
            continue
        parsed = _parse_item(el, digest_date)
        if parsed:
            articles.append(parsed)
    return articles


def _parse_item(el: Tag, digest_date: str) -> ParsedArticle | None:
    archive_path = _archive_href(el)
    # Digests rendered before the data-msgid era carry the message_id only inside
    # their /archive/<date>/<id>/ link, so fall back to that.
    message_id = str(el.get("data-msgid") or "").strip() or _msgid_from_archive(archive_path)
    if not message_id:
        return None
    subject = str(el.get("data-subject") or "").strip()
    sender = str(el.get("data-sender") or "").strip()

    raw_classes = el.get("class")
    classes = raw_classes if isinstance(raw_classes, list) else ([raw_classes] if raw_classes else [])
    if "card" in classes:
        tier, text = "high", _text(el.select_one(".card-summary"))
    elif el.find("span", class_="medium-summary"):
        tier, text = "medium", _text(el.select_one(".medium-summary"))
    else:
        tier, text = "low", _text(el.select_one("a"))

    return ParsedArticle(
        message_id=message_id,
        date=digest_date,
        sender_name=sender,
        subject=subject,
        text=text,
        tier=tier,
        topic=_card_topic(el) if tier == "high" else "",
        score=_badge_score(el),
        archive_path=archive_path,
        paywalled=el.find("span", class_="badge-lock") is not None,
    )


def _text(node: Tag | None) -> str:
    return node.get_text(separator=" ").strip() if node else ""


def _badge_score(el: Tag) -> float | None:
    for cls in ("badge-high", "badge-medium", "badge-low"):
        badge = el.find("span", class_=cls)
        if badge:
            try:
                return float(badge.get_text().strip())
            except ValueError:
                return None
    return None


def _card_topic(el: Tag) -> str:
    """The topic string trailing the score badge in a high card's meta line."""
    meta = el.select_one(".card-meta")
    if not meta:
        return ""
    # Renderer separates the topic with a "·"; take the tail after the last one.
    text = meta.get_text(separator=" ")
    if "·" in text:
        return text.rsplit("·", 1)[1].strip()
    return ""


def _archive_href(el: Tag) -> str:
    """The server-relative archive link, or "" if the digest fell back to a URL."""
    for a in el.find_all("a", href=True):
        href = str(a["href"])
        if href.startswith("/archive/"):
            return href
    return ""


def _msgid_from_archive(archive_path: str) -> str:
    """Pull the message_id out of ``/archive/<date>/<id>/index.html``.

    The ``<id>`` segment is the Gmail message_id; this is the only place it
    survives in pre-data-msgid digests.
    """
    parts = archive_path.split("/archive/", 1)[-1].split("/")
    return parts[1].strip() if len(parts) >= 2 else ""


def load_digests(conn: sqlite3.Connection, digests_dir: Path, stats: MigrationStats) -> None:
    for path in sorted(digests_dir.glob("*.html")):
        digest_date = path.stem
        if not _looks_like_date(digest_date):
            continue
        stats.digests += 1
        for a in parse_digest_html(path.read_text(encoding="utf-8"), digest_date):
            library.upsert_article(
                conn,
                message_id=a.message_id,
                date=a.date,
                sender_name=a.sender_name,
                subject=a.subject,
                one_line=a.one_line,
                summary=a.summary,
                topic=a.topic,
                score=a.score,
                archive_path=a.archive_path,
                paywalled=a.paywalled,
            )
            stats.articles += 1


def load_bodies(conn: sqlite3.Connection, archive_root: Path, stats: MigrationStats) -> None:
    """Index the body text of every archived article that has a DB row."""
    rows = conn.execute(
        "SELECT message_id, archive_path FROM articles WHERE archive_path != ''"
    ).fetchall()
    for row in rows:
        index_html = _archive_index_path(archive_root, row["archive_path"])
        if index_html is None or not index_html.exists():
            continue
        body = strip_html(index_html.read_text(encoding="utf-8", errors="replace"))
        if body:
            library.set_body(conn, row["message_id"], body)
            stats.bodies += 1


def _archive_index_path(archive_root: Path, archive_path: str) -> Path | None:
    """Map a served ``/archive/<date>/<id>/index.html`` URL to a local file."""
    marker = "/archive/"
    if marker not in archive_path:
        return None
    rel = archive_path.split(marker, 1)[1]
    return archive_root / rel


def load_feedback(conn: sqlite3.Connection, feedback_path: Path, stats: MigrationStats) -> None:
    if not feedback_path.exists():
        return
    data = yaml.safe_load(feedback_path.read_text(encoding="utf-8")) or []

    index: dict[tuple[str, str], list[str]] = {}
    for r in conn.execute("SELECT message_id, subject, sender_name FROM articles"):
        index.setdefault((r["subject"], r["sender_name"]), []).append(r["message_id"])

    for entry in data:
        reaction = entry.get("feedback")
        if reaction is None:
            continue
        key = (entry.get("subject"), entry.get("sender"))
        message_ids = index.get(key)
        if not message_ids:
            stats.feedback_unmatched += 1
            continue
        if len(message_ids) > 1:
            stats.feedback_collisions += 1
            logger.debug(f"Feedback key {key!r} maps to {len(message_ids)} articles")
        for message_id in message_ids:
            library.set_feedback(conn, message_id, str(reaction), read=True)
        stats.feedback_matched += 1


def load_read_state(conn: sqlite3.Connection, read_state_path: Path, stats: MigrationStats) -> None:
    if not read_state_path.exists():
        return
    ids = json.loads(read_state_path.read_text(encoding="utf-8"))
    known = {r["message_id"] for r in conn.execute("SELECT message_id FROM articles")}
    for message_id in ids:
        if message_id in known:
            library.set_read(conn, message_id, True)
            stats.read_flags += 1
        else:
            stats.read_unmatched += 1


def run_migration(
    conn: sqlite3.Connection,
    *,
    digests_dir: Path,
    archive_root: Path,
    feedback_path: Path,
    read_state_path: Path,
) -> MigrationStats:
    """Run the full offline backfill (steps 1–4) against an open DB connection."""
    stats = MigrationStats()
    load_digests(conn, digests_dir, stats)
    load_bodies(conn, archive_root, stats)
    load_feedback(conn, feedback_path, stats)
    load_read_state(conn, read_state_path, stats)
    conn.commit()
    logger.info(f"Migration complete: {stats.summary()}")
    return stats


def _looks_like_date(stem: str) -> bool:
    parts = stem.split("-")
    return len(parts) == 3 and all(p.isdigit() for p in parts)
