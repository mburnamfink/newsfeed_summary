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
