import json

import pytest

from newsfeed import library, migrate

DIGEST = """<!DOCTYPE html><html><body>
<div class="card" data-msgid="hi1" data-subject="It&#39;s a Big One" data-sender="Max Read">
  <h3><a href="/archive/2026-05-10/hi1/index.html">It's a Big One</a></h3>
  <div class="card-meta">Max Read
    <span class="badge badge-high">8.5</span>
    <span class="badge badge-lock">🔒 Paywalled</span>
    &nbsp;·&nbsp; internet culture, AI slop
  </div>
  <div class="card-summary">A long paragraph summary about AI slop.</div>
</div>
<ul class="medium-list">
  <li data-msgid="med1" data-subject="Malleable Software" data-sender="Refactoring">
    <span class="medium-sender">Refactoring</span>
    <span class="badge badge-medium">6.5</span>
    <span class="medium-subject"><a href="/archive/2026-05-10/med1/index.html">Malleable Software</a></span>
    <span class="medium-summary">Litt on malleable software.</span>
  </li>
</ul>
<ul class="low-list">
  <li data-msgid="low1" data-subject="Boomer Sway" data-sender="Troy Young">
    <span class="low-sender">Troy Young:</span>
    <a href="https://mail.google.com/mail/u/0/#all/low1">A rambling one-liner about AI.</a>
  </li>
</ul>
</body></html>"""


@pytest.fixture
def conn():
    c = library.connect(":memory:")
    yield c
    c.close()


def test_parse_digest_extracts_all_tiers():
    arts = migrate.parse_digest_html(DIGEST, "2026-05-10")
    by_id = {a.message_id: a for a in arts}
    assert set(by_id) == {"hi1", "med1", "low1"}

    hi = by_id["hi1"]
    assert hi.tier == "high"
    assert hi.subject == "It's a Big One"          # entity decoded
    assert hi.sender_name == "Max Read"
    assert hi.score == 8.5
    assert hi.paywalled is True
    assert hi.topic == "internet culture, AI slop"
    assert hi.summary == "A long paragraph summary about AI slop."
    assert hi.archive_path == "/archive/2026-05-10/hi1/index.html"

    med = by_id["med1"]
    assert med.tier == "medium"
    assert med.score == 6.5
    assert med.summary == "Litt on malleable software."

    low = by_id["low1"]
    assert low.tier == "low"
    assert low.one_line == "A rambling one-liner about AI."
    # non-archive fallback href is not treated as an archive path
    assert low.archive_path == ""


def test_load_digests_upserts_rows(conn, tmp_path):
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-05-10.html").write_text(DIGEST)
    (digests / "index.html").write_text("<html>not a digest</html>")

    stats = migrate.MigrationStats()
    migrate.load_digests(conn, digests, stats)

    assert stats.digests == 1        # index.html skipped
    assert stats.articles == 3
    art = library.get_article(conn, "hi1")
    assert art.display_summary == "A long paragraph summary about AI slop."
    assert library.get_article(conn, "low1").display_summary == "A rambling one-liner about AI."


def test_load_bodies_indexes_fts(conn, tmp_path):
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-05-10.html").write_text(DIGEST)
    archive = tmp_path / "archive"
    body_dir = archive / "2026-05-10" / "hi1"
    body_dir.mkdir(parents=True)
    (body_dir / "index.html").write_text("<html><body><p>transformers and diffusion models</p></body></html>")

    stats = migrate.MigrationStats()
    migrate.load_digests(conn, digests, stats)
    migrate.load_bodies(conn, archive, stats)

    assert stats.bodies == 1
    assert [a.message_id for a in library.search(conn, "diffusion")] == ["hi1"]


def test_load_feedback_matches_on_subject_sender(conn, tmp_path):
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-05-10.html").write_text(DIGEST)
    stats = migrate.MigrationStats()
    migrate.load_digests(conn, digests, stats)

    fb = tmp_path / "feedback.yaml"
    fb.write_text(json.dumps([
        {"subject": "It's a Big One", "sender": "Max Read", "feedback": "up"},
        {"subject": "Malleable Software", "sender": "Refactoring", "feedback": None},
        {"subject": "Ghost", "sender": "Nobody", "feedback": "down"},
    ]))

    migrate.load_feedback(conn, fb, stats)
    assert stats.feedback_matched == 1
    assert stats.feedback_unmatched == 1        # Ghost
    assert library.get_article(conn, "hi1").feedback == "up"
    assert library.get_article(conn, "hi1").read is True   # a reaction marks read
    assert library.get_article(conn, "med1").feedback is None  # null skipped


def test_load_read_state_sets_flags(conn, tmp_path):
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-05-10.html").write_text(DIGEST)
    stats = migrate.MigrationStats()
    migrate.load_digests(conn, digests, stats)

    rs = tmp_path / "read_state.json"
    rs.write_text(json.dumps(["med1", "unknown-id"]))
    migrate.load_read_state(conn, rs, stats)

    assert stats.read_flags == 1
    assert stats.read_unmatched == 1
    assert library.get_article(conn, "med1").read is True
    assert library.get_article(conn, "hi1").read is False


def test_run_migration_end_to_end(conn, tmp_path):
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-05-10.html").write_text(DIGEST)
    archive = tmp_path / "archive"
    (archive / "2026-05-10" / "hi1").mkdir(parents=True)
    (archive / "2026-05-10" / "hi1" / "index.html").write_text("<p>solar power grids</p>")
    fb = tmp_path / "feedback.yaml"
    fb.write_text(json.dumps([{"subject": "It's a Big One", "sender": "Max Read", "feedback": "up"}]))
    rs = tmp_path / "read_state.json"
    rs.write_text(json.dumps(["low1"]))

    stats = migrate.run_migration(
        conn, digests_dir=digests, archive_root=archive,
        feedback_path=fb, read_state_path=rs,
    )
    assert stats.articles == 3
    assert stats.bodies == 1
    assert stats.feedback_matched == 1
    assert stats.read_flags == 1
    assert [a.message_id for a in library.search(conn, "solar")] == ["hi1"]
