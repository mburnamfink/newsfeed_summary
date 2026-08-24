import json

import pytest

from newsfeed import backup, library
from newsfeed.backup import BackupConfig
from newsfeed.config import Paths


@pytest.fixture
def conn():
    c = library.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def calls(monkeypatch):
    """Record every rclone invocation instead of shelling out."""
    recorded: list[list[str]] = []

    def fake_run(args):
        recorded.append(args)
        return True

    monkeypatch.setattr(backup, "_run_rclone", fake_run)
    monkeypatch.setattr(backup, "_rclone_available", lambda: True)
    return recorded


CFG = BackupConfig(remote="gdrive:", path="newsfeed_summary", enabled=True)


def _star(conn, message_id, archive_path, **kw):
    library.upsert_article(
        conn,
        message_id=message_id,
        date=kw.get("date", "2026-05-10"),
        sender_name=kw.get("sender_name", "Max Read"),
        subject=kw.get("subject", "A subject"),
        one_line=kw.get("one_line", "one line"),
        archive_path=archive_path,
    )
    library.set_star(conn, message_id, True)


def _make_archive(paths: Paths, date: str, message_id: str) -> None:
    d = paths.serve / "archive" / date / message_id
    (d / "images").mkdir(parents=True)
    (d / "index.html").write_text("<html>hi</html>", encoding="utf-8")
    (d / "images" / "a.png").write_bytes(b"x")


# --- path helpers -----------------------------------------------------------


def test_join_keeps_colon_attached_and_no_double_slash():
    assert backup._join("gdrive:", "newsfeed_summary") == "gdrive:newsfeed_summary"
    assert backup._join("gdrive:", "newsfeed_summary", "state") == "gdrive:newsfeed_summary/state"
    assert backup._join("gdrive:nf/", "/starred", "2026-05-10") == "gdrive:nf/starred/2026-05-10"


def test_base_property():
    assert CFG.base == "gdrive:newsfeed_summary"


def test_starred_source_dir_maps_archive_path(tmp_path):
    paths = Paths(tmp_path)
    src = backup.starred_source_dir(paths, "/archive/2026-05-10/m1/index.html")
    assert src == tmp_path / "serve" / "archive" / "2026-05-10" / "m1"


def test_starred_source_dir_none_when_no_path(tmp_path):
    assert backup.starred_source_dir(Paths(tmp_path), "") is None


# --- state backup -----------------------------------------------------------


def test_backup_state_copies_present_files_only(tmp_path, calls):
    paths = Paths(tmp_path)
    paths.feedback.write_text("fb", encoding="utf-8")
    paths.preferences.write_text("pref", encoding="utf-8")
    # articles.db intentionally absent

    backup.backup_state(paths, CFG)

    dests = [args[2] for args in calls if args[0] == "copyto"]
    assert "gdrive:newsfeed_summary/state/feedback.yaml" in dests
    assert "gdrive:newsfeed_summary/state/preferences.yaml" in dests
    assert not any("articles.db" in d for d in dests)


# --- starred backup ---------------------------------------------------------


def test_backup_starred_copies_existing_archives_and_writes_manifest(tmp_path, conn, calls):
    paths = Paths(tmp_path)
    _star(conn, "m1", "/archive/2026-05-10/m1/index.html")
    _make_archive(paths, "2026-05-10", "m1")
    # starred but its archive is missing on disk -> skipped for copy, still in manifest
    _star(conn, "m2", "/archive/2026-05-11/m2/index.html", date="2026-05-11")

    copied = backup.backup_starred(conn, paths, CFG)

    assert copied == 1
    copy_calls = [args for args in calls if args[0] == "copy"]
    assert copy_calls == [
        ["copy", str(tmp_path / "serve/archive/2026-05-10/m1"),
         "gdrive:newsfeed_summary/starred/2026-05-10/m1"]
    ]
    assert any(args[0] == "copyto" and args[2].endswith("starred/manifest.csv") for args in calls)


def test_backup_starred_ignores_unstarred(tmp_path, conn, calls):
    paths = Paths(tmp_path)
    library.upsert_article(conn, message_id="plain", date="2026-05-10", sender_name="X",
                           archive_path="/archive/2026-05-10/plain/index.html")
    _make_archive(paths, "2026-05-10", "plain")

    assert backup.backup_starred(conn, paths, CFG) == 0
    assert not any(args[0] == "copy" for args in calls)


# --- top-level guards -------------------------------------------------------


def test_run_backup_disabled_makes_no_calls(tmp_path, conn, calls):
    backup.run_backup(conn, Paths(tmp_path), BackupConfig(enabled=False))
    assert calls == []


def test_run_backup_skips_when_rclone_missing(tmp_path, conn, calls, monkeypatch):
    monkeypatch.setattr(backup, "_rclone_available", lambda: False)
    backup.run_backup(conn, Paths(tmp_path), CFG)
    assert calls == []


# --- DB snapshot + sanity gate ----------------------------------------------


def _db_conn(tmp_path, n_articles):
    """A real on-disk DB (VACUUM INTO needs a file, not :memory:) with N articles."""
    conn = library.connect(tmp_path / "articles.db")
    for i in range(n_articles):
        library.upsert_article(conn, message_id=f"a{i}", date="2026-05-10", sender_name="X")
    conn.commit()
    return conn


def test_snapshot_validates_and_reports_count(tmp_path):
    conn = _db_conn(tmp_path, 3)
    result = backup._snapshot_and_validate(conn, Paths(tmp_path))
    conn.close()
    assert result is not None
    snap_path, count = result
    assert count == 3
    backup._cleanup(snap_path)


def test_snapshot_refuses_empty_db(tmp_path):
    conn = _db_conn(tmp_path, 0)
    assert backup._snapshot_and_validate(conn, Paths(tmp_path)) is None
    conn.close()


def test_snapshot_refuses_big_drop_vs_last_backup(tmp_path):
    paths = Paths(tmp_path)
    backup._write_backup_state(paths, 100)
    conn = _db_conn(tmp_path, 5)  # 5 << 80% of 100
    assert backup._snapshot_and_validate(conn, paths) is None
    conn.close()


def test_force_overrides_drop_refusal(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    backup._write_backup_state(paths, 100)
    monkeypatch.setenv("NEWSFEED_BACKUP_FORCE", "1")
    conn = _db_conn(tmp_path, 5)
    assert backup._snapshot_and_validate(conn, paths) is not None
    conn.close()


def test_run_backup_uploads_snapshot_and_history_and_records_state(tmp_path, calls):
    paths = Paths(tmp_path)
    conn = _db_conn(tmp_path, 4)
    try:
        backup.run_backup(conn, paths, CFG)
    finally:
        conn.close()

    dests = [args[2] for args in calls if args[0] == "copyto"]
    assert "gdrive:newsfeed_summary/state/articles.db" in dests
    assert any("/state/history/articles-" in d and d.endswith(".db") for d in dests)
    assert any("/state/history/monthly/articles-" in d for d in dests)
    assert backup._read_backup_state(paths)["last_count"] == 4


def test_run_backup_refused_db_still_syncs_yaml_but_not_db(tmp_path, calls):
    paths = Paths(tmp_path)
    paths.feedback.write_text("fb", encoding="utf-8")
    backup._write_backup_state(paths, 100)
    conn = _db_conn(tmp_path, 2)  # collapse -> refused
    try:
        backup.run_backup(conn, paths, CFG)
    finally:
        conn.close()

    dests = [args[2] for args in calls if args[0] == "copyto"]
    assert "gdrive:newsfeed_summary/state/feedback.yaml" in dests
    assert not any("articles.db" in d for d in dests)
    # last-good count is preserved, not overwritten by the refused run
    assert backup._read_backup_state(paths)["last_count"] == 100


def test_staleness_message_none_when_no_state(tmp_path):
    assert backup.staleness_message(Paths(tmp_path)) is None


def test_staleness_message_none_when_fresh(tmp_path):
    backup._write_backup_state(Paths(tmp_path), 10)  # records now()
    assert backup.staleness_message(Paths(tmp_path)) is None


def test_staleness_message_warns_when_stale(tmp_path):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=backup.STALE_AFTER_DAYS + 2)).isoformat()
    (tmp_path / ".backup_state.json").write_text(
        json.dumps({"last_success": old, "last_count": 10}), encoding="utf-8"
    )
    msg = backup.staleness_message(Paths(tmp_path))
    assert msg is not None
    assert "day(s) ago" in msg


def test_prune_daily_history_keeps_newest(tmp_path, monkeypatch):
    names = [f"articles-2026-05-{d:02d}.db" for d in range(1, 20)]  # 19 dailies
    monkeypatch.setattr(backup, "_rclone_lines", lambda args: names)
    deleted = []
    monkeypatch.setattr(backup, "_run_rclone", lambda args: deleted.append(args) or True)

    backup._prune_daily_history("gdrive:newsfeed_summary/state/history")

    assert len(deleted) == 19 - backup.DAILY_HISTORY_KEEP
    assert deleted[0][1].endswith("articles-2026-05-01.db")
    assert not any("2026-05-19" in a[1] for a in deleted)
