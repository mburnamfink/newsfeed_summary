"""Layered backup of Library state + starred articles to a cloud remote (ADR 0003).

The codebase is backed up by git and the 609 MB ``serve/archive/`` is left to its
external sources of truth (Gmail + the originating blogs). This module carries only
the two things that would otherwise be lost with the machine:

- **State** — ``feedback.yaml``, ``preferences.yaml``, ``articles.db``. Tiny and
  irreplaceable (``articles.db`` holds stars/read-state/feedback that can't be
  regenerated). Each is copied to ``<base>/state/``.
- **Starred articles** — each ``starred=1`` row's self-contained archive directory
  (``index.html`` + ``images/``) is copied to ``<base>/starred/<DATE>/<msgid>/``,
  with a browsable ``manifest.csv`` alongside.

``rclone`` is the transport, talking to the configured remote directly (not the
fuse mount, which may not be present). Everything here is best-effort: a missing
``rclone`` or a failing transfer logs a warning and never raises, so a backup
hiccup can't fail the daily digest run.

Because the remote copy is the *only* off-machine copy of ``articles.db``, and
the naive ``copyto`` that mirrors it is an unconditional overwrite, this module
guards against a bad local DB destroying months of accumulated stars/ratings:

- the DB is snapshotted with ``VACUUM INTO`` (a consistent copy, immune to a
  concurrent server write) and validated with ``PRAGMA integrity_check`` before
  anything is uploaded;
- a **sanity gate** refuses to overwrite the remote when the snapshot has zero
  articles or its article count has collapsed (>20%) versus the last good backup
  — the signature of a wrong-directory run or a truncated DB. Set
  ``NEWSFEED_BACKUP_FORCE=1`` to override after an intentional bulk delete;
- the validated snapshot is also kept as **dated history** under
  ``state/history/`` — the last 14 daily snapshots plus one permanent
  ``monthly/articles-YYYY-MM.db`` per month — so even a bad push that slips
  through leaves an untouched prior copy to restore from;
- the last successful backup's timestamp and article count are recorded locally
  in ``.backup_state.json``; a run warns if the previous success is stale.
"""
import csv
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass

from .config import Paths, backup_config

logger = logging.getLogger(__name__)

# Keep the last N daily snapshots under state/history/; monthly snapshots are kept
# forever (never pruned).
DAILY_HISTORY_KEEP = 14
# Refuse to overwrite the remote DB when the new article count is below this
# fraction of the last good backup's count — a collapse that big is far more
# likely a wrong-directory or truncated-DB accident than a real edit.
DROP_REFUSE_RATIO = 0.8
# Warn when the previous successful backup is older than this.
STALE_AFTER_DAYS = 3


@dataclass(frozen=True)
class BackupConfig:
    remote: str = "gdrive:"
    path: str = "newsfeed_summary"
    enabled: bool = True

    @classmethod
    def from_config(cls) -> "BackupConfig":
        d = backup_config()
        return cls(
            remote=str(d.get("remote", "gdrive:")),
            path=str(d.get("path", "newsfeed_summary")),
            enabled=bool(d.get("enabled", True)),
        )

    @property
    def base(self) -> str:
        return _join(self.remote, self.path)


def _join(*parts: str) -> str:
    """Join rclone path segments with ``/``, keeping ``remote:`` colon-attached.

    ``_join("gdrive:", "newsfeed_summary", "state")`` → ``gdrive:newsfeed_summary/state``
    (no stray slash after the colon, no doubled slashes).
    """
    out = ""
    for p in parts:
        if not p:
            continue
        if not out:
            out = p
        elif out.endswith(":") or out.endswith("/"):
            out += p.lstrip("/")
        else:
            out += "/" + p.lstrip("/")
    return out


def starred_source_dir(paths: Paths, archive_path: str) -> "os.PathLike | None":
    """On-disk archive directory for an article, from its DB ``archive_path``.

    ``archive_path`` is server-relative (``/archive/<DATE>/<msgid>/index.html``);
    the files live under ``serve/`` and the directory is the parent of that file.
    Returns ``None`` when the article has no archive path recorded.
    """
    if not archive_path:
        return None
    return (paths.serve / archive_path.lstrip("/")).parent


def _rclone_available() -> bool:
    return shutil.which("rclone") is not None


def _run_rclone(args: list[str]) -> bool:
    """Run ``rclone <args>``; return True on success, False on failure (logged)."""
    try:
        subprocess.run(["rclone", *args], check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() or str(e)
        logger.warning("rclone %s failed: %s", args[0] if args else "", detail)
        return False


def _rclone_lines(args: list[str]) -> list[str]:
    """Run a read-only ``rclone <args>`` and return stdout lines (empty on error)."""
    try:
        r = subprocess.run(["rclone", *args], check=True, capture_output=True, text=True)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() or str(e)
        logger.warning("rclone %s failed: %s", args[0] if args else "", detail)
        return []


# --- local backup-health state ----------------------------------------------


def _state_path(paths: Paths) -> "os.PathLike":
    return paths.root / ".backup_state.json"


def _read_backup_state(paths: Paths) -> dict:
    try:
        with open(_state_path(paths), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_backup_state(paths: Paths, count: int) -> None:
    data = {
        "last_success": datetime.now(timezone.utc).isoformat(),
        "last_count": count,
    }
    try:
        with open(_state_path(paths), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.warning("backup: could not record backup state: %s", e)


def staleness_message(paths: Paths) -> "str | None":
    """A one-line warning if the last successful backup is stale, else ``None``.

    Returns ``None`` when no backup has ever succeeded (no state file yet) so a
    fresh install doesn't cry wolf. The CLI surfaces this on the terminal; see the
    note in ``run_backup`` about why it can't be logged from the backup step.
    """
    last = _read_backup_state(paths).get("last_success")
    if not last:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return None
    if age > timedelta(days=STALE_AFTER_DAYS):
        return (
            f"last successful DB backup was {age.days} day(s) ago ({last}); the "
            "off-machine copy of your stars/ratings is going stale — check that "
            "rclone can still reach the remote"
        )
    return None


# --- validated DB snapshot ---------------------------------------------------


def _snapshot_and_validate(conn: sqlite3.Connection, paths: Paths) -> "tuple[str, int] | None":
    """Return ``(snapshot_path, article_count)`` safe to upload, else ``None``.

    ``VACUUM INTO`` writes a transactionally-consistent copy even while another
    process (the archive server) holds the live DB open, so we never upload a torn
    file. The snapshot is then integrity-checked and passed through the sanity gate
    (non-empty; count not collapsed vs. the last good backup). The caller must
    delete the returned path.
    """
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn.execute("VACUUM INTO ?", (tmp,))
    except sqlite3.Error as e:
        logger.error("backup: could not snapshot articles.db (%s); NOT overwriting remote", e)
        _cleanup(tmp)
        return None

    snap = sqlite3.connect(tmp)
    try:
        integrity = snap.execute("PRAGMA integrity_check").fetchone()[0]
        count = snap.execute("SELECT count(*) FROM articles").fetchone()[0]
    except sqlite3.Error as e:
        logger.error("backup: snapshot unreadable (%s); NOT overwriting remote", e)
        snap.close()
        _cleanup(tmp)
        return None
    snap.close()

    if integrity != "ok":
        logger.error(
            "backup: snapshot failed integrity_check (%s); NOT overwriting remote", integrity
        )
        _cleanup(tmp)
        return None
    if count == 0:
        logger.error(
            "backup: snapshot has 0 articles (wrong NEWSFEED_HOME or empty DB?); "
            "NOT overwriting remote"
        )
        _cleanup(tmp)
        return None

    prior = _read_backup_state(paths).get("last_count")
    if (
        isinstance(prior, int)
        and prior > 0
        and count < prior * DROP_REFUSE_RATIO
        and not os.environ.get("NEWSFEED_BACKUP_FORCE")
    ):
        logger.error(
            "backup: article count dropped %d → %d (>%.0f%%); refusing to overwrite "
            "remote. If this is an intentional bulk delete, re-run with "
            "NEWSFEED_BACKUP_FORCE=1.",
            prior, count, (1 - DROP_REFUSE_RATIO) * 100,
        )
        _cleanup(tmp)
        return None

    return tmp, count


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# --- state + history ---------------------------------------------------------


def backup_state(paths: Paths, cfg: BackupConfig, db_snapshot: "str | None" = None) -> None:
    """Mirror the small state files to ``<base>/state/``.

    ``feedback.yaml`` / ``preferences.yaml`` are copied as-is (an empty file is
    skipped rather than allowed to clobber a good remote copy). ``articles.db`` is
    uploaded only from a pre-validated ``db_snapshot`` — never the live file — so a
    failed sanity gate leaves the remote DB untouched.
    """
    dest = _join(cfg.base, "state")
    for src in (paths.feedback, paths.preferences):
        if not src.exists():
            logger.warning("backup: %s not found, skipping", src.name)
            continue
        if src.stat().st_size == 0:
            logger.warning("backup: %s is empty, skipping (not clobbering remote)", src.name)
            continue
        _run_rclone(["copyto", str(src), _join(dest, src.name)])
    if db_snapshot:
        _run_rclone(["copyto", db_snapshot, _join(dest, "articles.db")])


def backup_db_history(db_snapshot: str, cfg: BackupConfig, day: date) -> None:
    """Keep dated copies of the validated DB snapshot under ``state/history/``.

    A daily ``articles-YYYY-MM-DD.db`` (last ``DAILY_HISTORY_KEEP`` retained) plus a
    permanent ``monthly/articles-YYYY-MM.db`` (overwritten within a month, so it
    settles on that month's final state, and never pruned).
    """
    hist = _join(cfg.base, "state", "history")
    _run_rclone(["copyto", db_snapshot, _join(hist, f"articles-{day.isoformat()}.db")])
    _run_rclone(["copyto", db_snapshot, _join(hist, "monthly", f"articles-{day:%Y-%m}.db")])
    _prune_daily_history(hist)


def _prune_daily_history(hist: str) -> None:
    """Delete all but the newest ``DAILY_HISTORY_KEEP`` daily snapshots.

    ``lsf --files-only`` is non-recursive, so the ``monthly/`` subdirectory is never
    listed and its permanent snapshots are never pruned. ``articles-YYYY-MM-DD.db``
    names sort lexicographically in date order.
    """
    names = sorted(
        n for n in _rclone_lines(["lsf", "--files-only", "--include", "articles-*.db", hist])
    )
    for stale in names[:-DAILY_HISTORY_KEEP] if len(names) > DAILY_HISTORY_KEEP else []:
        _run_rclone(["deletefile", _join(hist, stale)])


# --- starred articles --------------------------------------------------------


def backup_starred(conn: sqlite3.Connection, paths: Paths, cfg: BackupConfig) -> int:
    """Copy each starred article's archive directory to ``<base>/starred/``.

    Additive and idempotent (rclone skips unchanged files). Writes a manifest of
    all starred rows regardless of whether their archive is still on disk. Returns
    the number of article directories copied.
    """
    rows = conn.execute(
        "SELECT date, message_id, archive_path, sender_name, subject, one_line "
        "FROM articles WHERE starred = 1 ORDER BY date, message_id"
    ).fetchall()

    base_starred = _join(cfg.base, "starred")
    copied = 0
    for r in rows:
        src = starred_source_dir(paths, r["archive_path"])
        if src is None or not os.path.isdir(src):
            logger.warning(
                "backup: no archive on disk for starred %s; skipping", r["message_id"]
            )
            continue
        dest = _join(base_starred, r["date"] or "undated", r["message_id"])
        if _run_rclone(["copy", str(src), dest]):
            copied += 1

    _write_manifest(rows, base_starred)
    return copied


def _write_manifest(rows: list[sqlite3.Row], base_starred: str) -> None:
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["message_id", "date", "sender_name", "subject", "one_line"])
            for r in rows:
                w.writerow(
                    [r["message_id"], r["date"], r["sender_name"], r["subject"], r["one_line"]]
                )
        _run_rclone(["copyto", tmp, _join(base_starred, "manifest.csv")])
    finally:
        os.unlink(tmp)


def run_backup(conn: sqlite3.Connection, paths: Paths, cfg: BackupConfig | None = None) -> None:
    """Back up state + starred articles. Best-effort; never raises.

    The DB is only uploaded from a validated snapshot; if the sanity gate refuses
    it, the feedback/preferences and starred archives are still synced but the
    remote ``articles.db`` and its history are left untouched.

    Backup staleness is *not* warned about here: a digest run detaches to a
    background logfile before this executes, so a warning logged now would never
    reach the user. The CLI checks ``staleness_message`` in the parent process,
    before detaching, so the alert lands on the terminal instead.
    """
    cfg = cfg or BackupConfig.from_config()
    if not cfg.enabled:
        logger.info("backup: disabled in preferences, skipping")
        return
    if not _rclone_available():
        logger.warning("backup: rclone not found on PATH, skipping")
        return

    snap = _snapshot_and_validate(conn, paths)
    db_snapshot = snap[0] if snap else None
    try:
        backup_state(paths, cfg, db_snapshot)
        if snap:
            backup_db_history(db_snapshot, cfg, date.today())
        n = backup_starred(conn, paths, cfg)
    finally:
        if db_snapshot:
            _cleanup(db_snapshot)

    if snap:
        _write_backup_state(paths, snap[1])
        logger.info("backup: synced state + %d starred article(s) to %s", n, cfg.base)
    else:
        logger.error(
            "backup: articles.db was NOT backed up (see above); feedback/preferences "
            "and %d starred article(s) still synced to %s", n, cfg.base
        )
