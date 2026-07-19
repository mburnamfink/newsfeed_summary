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
"""
import csv
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass

from .config import Paths, backup_config

logger = logging.getLogger(__name__)


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


def backup_state(paths: Paths, cfg: BackupConfig) -> None:
    """Mirror the three small state files to ``<base>/state/``.

    articles.db is copied as-is; the daily-run caller has already committed and
    closed its connection, so the file reflects committed state.
    """
    dest = _join(cfg.base, "state")
    for src in (paths.feedback, paths.preferences, paths.db):
        if not src.exists():
            logger.warning("backup: %s not found, skipping", src.name)
            continue
        _run_rclone(["copyto", str(src), _join(dest, src.name)])


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
    """Back up state + starred articles. Best-effort; never raises."""
    cfg = cfg or BackupConfig.from_config()
    if not cfg.enabled:
        logger.info("backup: disabled in preferences, skipping")
        return
    if not _rclone_available():
        logger.warning("backup: rclone not found on PATH, skipping")
        return
    backup_state(paths, cfg)
    n = backup_starred(conn, paths, cfg)
    logger.info("backup: synced state + %d starred article(s) to %s", n, cfg.base)
