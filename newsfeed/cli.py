import argparse
import asyncio
import logging
import os
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import backup, ingest, library, migrate, rebuild, tagger
from .archiver import archive_email
from .config import paths, server_base_url
from .feedback import select_examples_from_rows
from .gmail_client import authenticate, fetch_newsletter_emails
from .llm import build_backend
from .models import Preferences
from .renderer import render_digest, render_index
from .scorer import DEFAULT_LOW_SCORE, score_emails
from .summarizer import summarize_articles, summarize_emails

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def _load_labels(gmail_config: dict) -> list[str]:
    raw = gmail_config.get("labels", gmail_config.get("label", "Newsletters"))
    if isinstance(raw, list):
        return raw
    return [raw]


def load_llm_config(path: Path) -> dict:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return data.get("llm", {}) or {}


def load_preferences(path: Path) -> Preferences:
    with path.open() as f:
        data = yaml.safe_load(f)
    return Preferences(
        gmail_labels=_load_labels(data.get("gmail", {})),
        interests=data.get("interests", []),
        thresholds=data.get("thresholds", {"high": 7, "medium": 4}),
        tags=data.get("tags", []),
        boost_sources=data.get("boost_sources", []),
        mute_sources=data.get("mute_sources", []),
        boost_keywords=data.get("boost_keywords", []),
        mute_keywords=data.get("mute_keywords", []),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail newsletter digest + Library.")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=1),
        metavar="YYYY-MM-DD",
        help="Date to fetch newsletters for (default: yesterday)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the digest in a browser when done (implies --foreground)",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run attached to the terminal instead of detaching to the background",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("migrate", help="Backfill articles.db from digests/archives/feedback")

    add_p = sub.add_parser("add", help="Save an article from a URL into the Library")
    add_p.add_argument("url", help="Article URL to fetch, archive, score and star")
    add_p.add_argument(
        "--foreground",
        action="store_true",
        help="Run attached to the terminal instead of detaching to the background",
    )

    rebuild_p = sub.add_parser(
        "rebuild", help="Re-render stored digests into the current format (no fetch/re-score)"
    )
    rebuild_p.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                           help="Only rebuild digests dated on or after this day")

    sub.add_parser("backup", help="Back up Library state + starred articles to the cloud remote")

    retag_p = sub.add_parser("retag", help="Recompute article tags against the current vocabulary")
    scope = retag_p.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="Re-tag every stored article (default)")
    scope.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                       help="Re-tag articles dated on or after this day")
    scope.add_argument("--tag", metavar="TAG", help="Re-tag only articles that currently carry TAG")

    args = parser.parse_args()

    try:
        p = paths()  # fail fast with a clear message if NEWSFEED_HOME is unset
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(2)

    # Surface a stale-backup warning here, in the parent, before a digest/add run
    # detaches and redirects its logs to a file where the user won't see them.
    stale = backup.staleness_message(p)
    if stale:
        print(f"\n⚠  Backup warning: {stale}\n", file=sys.stderr)

    if args.command == "migrate":
        _run_migration()
        return
    if args.command == "add":
        # Fetch + render + LLM scoring take a while; detach so the terminal returns
        # immediately and progress lands in a per-capture log. --foreground stays
        # attached (and is the fallback where fork isn't available).
        logfile = paths().logs / f"newsfeed-add-{datetime.now():%Y%m%d-%H%M%S}.log"
        if not args.foreground and _detach_to_background(
            logfile, f"Saving {args.url} in the background — logs: {logfile}"
        ):
            return
        asyncio.run(_run_add(args))
        return
    if args.command == "rebuild":
        _run_rebuild(args)
        return
    if args.command == "retag":
        asyncio.run(_run_retag(args))
        return
    if args.command == "backup":
        _run_backup()
        return

    # A normal interactive run detaches so the terminal comes straight back and a
    # browser tab pops up when the digest is ready. Cron / headless runs (--no-open)
    # and --foreground stay attached so their exit status and logs are visible.
    if not args.foreground and not args.no_open:
        logfile = paths().logs / f"newsfeed-{args.date.isoformat()}.log"
        url = f"{server_base_url()}/digests/{args.date.isoformat()}.html"
        message = (
            f"Generating digest in the background — logs: {logfile}\n"
            f"It will open at {url} when ready."
        )
        if _detach_to_background(logfile, message):
            return

    asyncio.run(_generate_digest(args))


def _run_migration() -> None:
    p = paths()
    conn = library.connect(p.db)
    try:
        stats = migrate.run_migration(
            conn,
            digests_dir=p.digests,
            archive_root=p.archive,
            feedback_path=p.feedback,
            read_state_path=p.serve / "read_state.json",
        )
    finally:
        conn.close()
    logger.info(f"Backfill written to {p.db} — {stats.summary()}")
    logger.info("Next: `newsfeed retag --all` to tag the backlog against your vocabulary.")


async def _run_add(args: argparse.Namespace) -> None:
    """Capture a URL as a Saved Article: archive, score, tag, summarize, star.

    Reuses the whole newsletter pipeline — the only new step is fetching the URL
    into an Email (ingest.fetch_url). The result is auto-starred so it lands on
    the curated shelf and, via ADR 0003, is backed up to the cloud remote.
    """
    p = paths()
    preferences = load_preferences(p.preferences)
    backend = build_backend(load_llm_config(p.preferences))

    logger.info(f"Fetching {args.url}")
    try:
        email = await ingest.fetch_url(args.url)
    except ValueError as e:
        logger.error(f"Could not save {args.url}: {e}")
        sys.exit(1)
    target_date = email.date.date()
    email.archive_path = await asyncio.to_thread(archive_email, email, target_date, p.archive)

    conn = library.connect(p.db)
    try:
        examples = select_examples_from_rows(library.calibration_rows(conn))
        scored = (await score_emails([email], preferences, backend, examples))[0]
        await summarize_articles([scored], "paragraph", backend)

        library.upsert_scored(conn, scored, target_date)
        if email.body:
            library.set_body(conn, email.message_id, email.body)
        library.set_star(conn, email.message_id, True)
        conn.commit()
    finally:
        conn.close()

    try:
        _run_backup()
    except Exception as e:
        logger.warning(f"backup failed: {e}")

    base = server_base_url()
    tags = ", ".join(scored.tags) or "—"
    print(f"Saved: {email.subject!r}")
    print(f"  {email.sender_name} · score {scored.interest_score:.1f} · tags: {tags}")
    if email.archive_path:
        print(f"  archive: {base}{email.archive_path}")
    print(f"  library: {base}/library?source=url")


def _run_rebuild(args: argparse.Namespace) -> None:
    p = paths()
    conn = library.connect(p.db)
    try:
        since = args.since.isoformat() if args.since else None
        total = rebuild.rebuild_all(conn, p.digests, since=since)
    finally:
        conn.close()
    render_index(p.digests, p.serve)
    logger.info(f"Rebuilt {total} items across stored digests.")


def _run_backup() -> None:
    p = paths()
    conn = library.connect(p.db)
    try:
        backup.run_backup(conn, p)
    finally:
        conn.close()


async def _run_retag(args: argparse.Namespace) -> None:
    p = paths()
    preferences = load_preferences(p.preferences)
    if not preferences.tags:
        logger.error("No `tags:` vocabulary in preferences.yaml; nothing to tag against.")
        return
    backend = build_backend(load_llm_config(p.preferences))
    conn = library.connect(p.db)
    try:
        if args.since:
            ids = library.all_message_ids(conn, since=args.since.isoformat())
        elif args.tag:
            ids = library.message_ids_with_tag(conn, args.tag)
        else:
            ids = library.all_message_ids(conn)
        articles = [a for a in (library.get_article(conn, i) for i in ids) if a]
        if not articles:
            logger.warning("No matching articles to re-tag.")
            return
        n = await tagger.retag(conn, articles, preferences.tags, backend)
        logger.info(f"Re-tagged {n} articles against {len(preferences.tags)} vocabulary terms.")
    finally:
        conn.close()


def _detach_to_background(logfile: Path, parent_message: str) -> bool:
    """Daemonise via double-fork; return True in the parent, False in the worker.

    The worker's stdout/stderr are redirected to ``logfile``. The parent prints
    ``parent_message`` and returns. POSIX only — when fork isn't available the
    caller falls through to a normal foreground run.
    """
    if not hasattr(os, "fork"):
        return False

    logfile.parent.mkdir(parents=True, exist_ok=True)

    if os.fork() > 0:
        print(parent_message)
        return True

    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    log_fd = os.open(str(logfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    return False


# Bound concurrent archive builds: each archives one newsletter (downloading its
# images sequentially), so a handful in flight saturates the network without
# opening hundreds of connections at once.
ARCHIVE_CONCURRENCY = 4


async def _archive_all(emails, target_date: date, archive_root: Path) -> None:
    semaphore = asyncio.Semaphore(ARCHIVE_CONCURRENCY)

    async def _one(email) -> None:
        async with semaphore:
            email.archive_path = await asyncio.to_thread(archive_email, email, target_date, archive_root)

    await asyncio.gather(*(_one(email) for email in emails))


async def _generate_digest(args: argparse.Namespace) -> None:
    target_date: date = args.date

    logger.info(f"Generating digest for {target_date}")

    p = paths()
    preferences = load_preferences(p.preferences)
    backend = build_backend(load_llm_config(p.preferences))
    creds = authenticate(p.credentials, p.token)
    emails = fetch_newsletter_emails(creds, preferences.gmail_labels, target_date)

    if not emails:
        logger.warning(f"No newsletter emails found for {target_date}. Check your label name and date.")
        return

    conn = library.connect(p.db)
    try:
        examples = select_examples_from_rows(library.calibration_rows(conn))
        if examples:
            logger.info(f"Loaded {len(examples)} feedback examples for scoring calibration")

        # Archiving touches only raw_html/archive_path while scoring reads the body,
        # so the two run concurrently without contending over the emails.
        scored, _ = await asyncio.gather(
            score_emails(emails, preferences, backend, examples),
            _archive_all(emails, target_date, p.archive),
        )
        scored = await summarize_emails(scored, preferences, backend)

        scoring_failures = sum(1 for s in scored if s.scoring_failed)
        if scoring_failures:
            logger.error(
                f"⚠️  SCORING ALERT: {scoring_failures}/{len(scored)} article(s) could not be "
                f"scored and fell back to {DEFAULT_LOW_SCORE}. Ranking for this digest is "
                f"unreliable — check the log above for the malformed response."
            )

        # articles.db is now the source of truth: upsert each result (preserving any
        # existing star/read/feedback/tag corrections) and index its body for search.
        for s in scored:
            library.upsert_scored(conn, s, target_date)
            if s.email.body:
                library.set_body(conn, s.email.message_id, s.email.body)
        conn.commit()
    finally:
        conn.close()

    output_path = render_digest(scored, target_date, p.digests, scoring_failures=scoring_failures)
    render_index(p.digests, p.serve)
    logger.info(f"Digest saved to {output_path}")

    # State is now fully current (db committed, digests + index rendered); snapshot
    # it off-machine. Best-effort — a backup failure must not fail the digest run.
    try:
        _run_backup()
    except Exception as e:
        logger.warning(f"backup failed: {e}")

    # Open through the Archive Server, not the file:// path — the feedback and
    # read-tracking buttons POST to the server and are inert on a local file.
    if not args.no_open:
        digest_url = f"{server_base_url()}/digests/{target_date.isoformat()}.html"
        webbrowser.open(digest_url)


if __name__ == "__main__":
    main()
