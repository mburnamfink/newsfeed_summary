from datetime import date

import pytest

from newsfeed import library
from newsfeed.models import Email, ScoredEmail


@pytest.fixture
def conn():
    c = library.connect(":memory:")
    yield c
    c.close()


def _add(conn, message_id, **kw):
    defaults = dict(
        date="2026-05-10",
        sender_name="Max Read",
        sender_email="max@readmax.com",
        subject="A subject",
        one_line="one line",
        score=8.0,
    )
    defaults.update(kw)
    library.upsert_article(conn, message_id=message_id, **defaults)


# --- effective tags ---------------------------------------------------------


def test_effective_tags_are_llm_tags_by_default(conn):
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", ["ai", "software-engineering"])
    assert library.effective_tags(conn, "m1") == ["ai", "software-engineering"]


def test_reader_add_extends_effective_tags(conn):
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", ["ai"])
    library.apply_tag_delta(conn, "m1", "history", "add")
    assert library.effective_tags(conn, "m1") == ["ai", "history"]


def test_reader_remove_suppresses_llm_tag(conn):
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", ["ai", "history"])
    library.apply_tag_delta(conn, "m1", "history", "remove")
    assert library.effective_tags(conn, "m1") == ["ai"]


def test_add_of_existing_llm_tag_is_noop(conn):
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", ["ai"])
    library.apply_tag_delta(conn, "m1", "ai", "add")
    assert library.effective_tags(conn, "m1") == ["ai"]


def test_clear_reverts_to_llm_decision(conn):
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", ["ai", "history"])
    library.apply_tag_delta(conn, "m1", "history", "remove")
    assert library.effective_tags(conn, "m1") == ["ai"]
    library.apply_tag_delta(conn, "m1", "history", "clear")
    assert library.effective_tags(conn, "m1") == ["ai", "history"]


def test_delta_survives_retag(conn):
    """The reader overlay must re-apply after LLM tags are recomputed."""
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", ["ai"])
    library.apply_tag_delta(conn, "m1", "ai", "remove")
    library.apply_tag_delta(conn, "m1", "history", "add")
    # retag: LLM now proposes a different set
    library.set_llm_tags(conn, "m1", ["ai", "futurism"])
    assert library.effective_tags(conn, "m1") == ["futurism", "history"]


def test_add_op_toggling_to_remove(conn):
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", [])
    library.apply_tag_delta(conn, "m1", "ai", "add")
    assert library.effective_tags(conn, "m1") == ["ai"]
    library.apply_tag_delta(conn, "m1", "ai", "remove")
    assert library.effective_tags(conn, "m1") == []


def test_invalid_tag_op_raises(conn):
    _add(conn, "m1")
    with pytest.raises(ValueError):
        library.apply_tag_delta(conn, "m1", "ai", "bogus")


# --- upsert preserves reader state ------------------------------------------


def test_upsert_preserves_star_read_feedback_and_delta(conn):
    _add(conn, "m1", score=5.0, summary="v1")
    library.set_llm_tags(conn, "m1", ["ai"])
    library.set_star(conn, "m1", True)
    library.set_feedback(conn, "m1", "up", read=True)
    library.apply_tag_delta(conn, "m1", "history", "add")

    # a re-run refreshes pipeline-derived fields only
    _add(conn, "m1", score=9.0, summary="v2")

    art = library.get_article(conn, "m1")
    assert art.score == 9.0
    assert art.summary == "v2"
    assert art.starred is True
    assert art.read is True
    assert art.feedback == "up"
    assert "history" in art.tags


def test_upsert_scored_writes_row_and_tags(conn):
    email = Email(
        message_id="m9",
        sender_name="Karl Schroeder",
        sender_email="karl@example.com",
        subject="Futures",
        date=None,  # type: ignore[arg-type]
        body="body",
        archive_path="/archive/2026-05-10/m9/index.html",
    )
    scored = ScoredEmail(
        email=email, interest_score=7.5, topic="futurism", one_line="a line",
        summary="a summary", tags=["futurism", "science-fiction"],
    )
    library.upsert_scored(conn, scored, date(2026, 5, 10))
    art = library.get_article(conn, "m9")
    assert art.sender_name == "Karl Schroeder"
    assert art.archive_path == "/archive/2026-05-10/m9/index.html"
    assert art.tags == ["futurism", "science-fiction"]
    assert art.tier == "high"


# --- browse + search --------------------------------------------------------


def test_list_by_author_newest_first(conn):
    _add(conn, "m1", date="2026-05-10", sender_name="Max Read")
    _add(conn, "m2", date="2026-05-12", sender_name="Max Read")
    _add(conn, "m3", date="2026-05-11", sender_name="Someone Else")
    ids = [a.message_id for a in library.list_by_author(conn, "Max Read")]
    assert ids == ["m2", "m1"]


def test_list_by_tag_uses_effective_set(conn):
    _add(conn, "m1")
    _add(conn, "m2")
    library.set_llm_tags(conn, "m1", ["ai"])
    library.set_llm_tags(conn, "m2", ["history"])
    library.apply_tag_delta(conn, "m2", "ai", "add")
    ids = sorted(a.message_id for a in library.list_by_tag(conn, "ai"))
    assert ids == ["m1", "m2"]


def test_facets_count_effective_tags(conn):
    _add(conn, "m1")
    _add(conn, "m2")
    library.set_llm_tags(conn, "m1", ["ai", "history"])
    library.set_llm_tags(conn, "m2", ["ai"])
    assert library.tag_facets(conn) == [("ai", 2), ("history", 1)]
    assert library.author_facets(conn) == [("Max Read", 2)]


def test_search_finds_body_terms(conn):
    _add(conn, "m1", subject="AI piece")
    _add(conn, "m2", subject="climate piece")
    library.set_body(conn, "m1", "a discussion of transformer neural networks")
    library.set_body(conn, "m2", "a discussion of solar and wind power")
    ids = [a.message_id for a in library.search(conn, "transformer")]
    assert ids == ["m1"]
    # multi-term is AND
    assert [a.message_id for a in library.search(conn, "solar wind")] == ["m2"]
    assert library.search(conn, "nonexistentword") == []


def test_search_tolerates_punctuation(conn):
    _add(conn, "m1")
    library.set_body(conn, "m1", "the C++ language and its quirks")
    # bare punctuation must not raise an FTS syntax error
    assert [a.message_id for a in library.search(conn, "language")] == ["m1"]
    assert library.search(conn, '"') == []


def test_set_body_replaces_not_duplicates(conn):
    _add(conn, "m1")
    library.set_body(conn, "m1", "first version alpha")
    library.set_body(conn, "m1", "second version beta")
    assert library.search(conn, "alpha") == []
    assert [a.message_id for a in library.search(conn, "beta")] == ["m1"]


def test_starred_filter(conn):
    _add(conn, "m1")
    _add(conn, "m2")
    library.set_star(conn, "m2", True)
    assert [a.message_id for a in library.list_starred(conn)] == ["m2"]


def test_feedback_clear_unreads(conn):
    _add(conn, "m1")
    library.set_feedback(conn, "m1", "down", read=True)
    assert library.get_article(conn, "m1").read is True
    library.set_feedback(conn, "m1", None, read=False)
    art = library.get_article(conn, "m1")
    assert art.feedback is None
    assert art.read is False


def test_display_summary_falls_back_to_one_line(conn):
    _add(conn, "m1", one_line="just a line", summary="")
    assert library.get_article(conn, "m1").display_summary == "just a line"


def test_saved_articles_carry_url_and_source(conn):
    _add(conn, "m1", url="https://example.com/x", source="url")
    art = library.get_article(conn, "m1")
    assert art.source == "url"
    assert art.url == "https://example.com/x"


def test_gmail_rows_default_source(conn):
    _add(conn, "m1")
    art = library.get_article(conn, "m1")
    assert art.source == "gmail"
    assert art.url == ""


def test_list_saved_returns_only_url_rows(conn):
    _add(conn, "gm", source="gmail")
    _add(conn, "u1", date="2026-05-10", source="url", url="https://a.com")
    _add(conn, "u2", date="2026-05-12", source="url", url="https://b.com")
    ids = [a.message_id for a in library.list_saved(conn)]
    assert ids == ["u2", "u1"]  # newest first, gmail row excluded


def test_upsert_scored_persists_source_and_url(conn):
    email = Email(
        message_id="url-abc", sender_name="Jane Doe", sender_email="",
        subject="Post", date=None, body="body",  # type: ignore[arg-type]
        url="https://example.com/post", source="url",
    )
    scored = ScoredEmail(email=email, interest_score=6.0, topic="t", one_line="l", summary="s")
    library.upsert_scored(conn, scored, date(2026, 5, 10))
    art = library.get_article(conn, "url-abc")
    assert art.source == "url"
    assert art.url == "https://example.com/post"


def test_connect_adds_columns_to_legacy_db(tmp_path):
    """A DB created before url/source existed gains them on the next connect()."""
    import sqlite3

    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    # The original ADR-0002 articles schema, before url/source were added.
    legacy.execute(
        "CREATE TABLE articles (message_id TEXT PRIMARY KEY, date TEXT, "
        "sender_name TEXT, sender_email TEXT, subject TEXT, one_line TEXT, "
        "summary TEXT DEFAULT '', topic TEXT DEFAULT '', score REAL, "
        "archive_path TEXT DEFAULT '', paywalled INTEGER DEFAULT 0, "
        "starred INTEGER DEFAULT 0, read INTEGER DEFAULT 0, feedback TEXT)"
    )
    legacy.execute("INSERT INTO articles (message_id, date) VALUES ('m1', '2026-05-10')")
    legacy.commit()
    legacy.close()

    conn = library.connect(db)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
        assert {"url", "source"} <= cols
        art = library.get_article(conn, "m1")
        assert art.source == "gmail"  # column default backfills the old row
    finally:
        conn.close()


def test_state_map_only_returns_nondefault_rows(conn):
    _add(conn, "plain")
    _add(conn, "starred")
    _add(conn, "reacted")
    library.set_star(conn, "starred", True)
    library.set_feedback(conn, "reacted", "up", read=True)
    state = library.state_map(conn)
    assert set(state) == {"starred", "reacted"}       # plain row omitted
    assert state["starred"]["starred"] is True
    assert state["reacted"]["feedback"] == "up"
    assert state["reacted"]["read"] is True


# --- long-form summaries (ADR 0007) -----------------------------------------


def test_set_and_get_summary_roundtrip(conn):
    _add(conn, "m1")
    library.set_summary(conn, "m1", "## Long summary\n\nBody.",
                        model="claude-opus-4-8", source="body", word_count=3)
    s = library.get_summary(conn, "m1")
    assert s["summary_md"] == "## Long summary\n\nBody."
    assert s["model"] == "claude-opus-4-8"
    assert s["source"] == "body"
    assert s["word_count"] == 3
    assert s["created_at"]  # timestamp stamped


def test_get_summary_absent_is_none(conn):
    _add(conn, "m1")
    assert library.get_summary(conn, "m1") is None


def test_set_summary_replaces_existing(conn):
    _add(conn, "m1")
    library.set_summary(conn, "m1", "first", model="a", source="body", word_count=1)
    library.set_summary(conn, "m1", "second", model="b", source="url", word_count=1)
    s = library.get_summary(conn, "m1")
    assert s["summary_md"] == "second"
    assert s["source"] == "url"


def test_has_summary_flag_on_article(conn):
    _add(conn, "m1")
    _add(conn, "m2")
    library.set_summary(conn, "m1", "s", model="a", source="body", word_count=1)
    assert library.get_article(conn, "m1").has_summary is True
    assert library.get_article(conn, "m2").has_summary is False


def test_summary_survives_reupsert(conn):
    """A digest re-run's upsert must not clobber a stored summary (own table)."""
    _add(conn, "m1")
    library.set_summary(conn, "m1", "keep me", model="a", source="body", word_count=2)
    _add(conn, "m1", subject="Re-scored subject", score=3.0)  # simulate re-run
    assert library.get_summary(conn, "m1")["summary_md"] == "keep me"


def test_state_map_reports_summary_flag(conn):
    _add(conn, "only_summary")
    library.set_summary(conn, "only_summary", "s", model="a", source="body", word_count=1)
    state = library.state_map(conn)
    assert state["only_summary"]["summary"] is True  # summarized-only rows appear


def test_get_body_roundtrip(conn):
    _add(conn, "m1")
    library.set_body(conn, "m1", "the full article text")
    assert library.get_body(conn, "m1") == "the full article text"
    assert library.get_body(conn, "absent") == ""


def test_reading_minutes_derived_from_body(conn):
    _add(conn, "m1")
    library.set_body(conn, "m1", " ".join(["word"] * 1200))  # 1200 / 200 wpm
    assert library.get_article(conn, "m1").reading_minutes == 6


def test_reading_minutes_zero_without_body(conn):
    _add(conn, "m1")
    assert library.get_article(conn, "m1").reading_minutes == 0


# --- triage: dismissed state and today/backlog split (ADR 0008) ---------------


def test_dismiss_removes_from_unread_without_marking_read(conn):
    _add(conn, "m1")
    _add(conn, "m2")
    library.set_dismissed(conn, ["m1"], True)
    assert [a.message_id for a in library.list_unread(conn)] == ["m2"]
    m1 = library.get_article(conn, "m1")
    assert m1.dismissed is True
    assert m1.read is False
    assert m1.feedback is None


def test_undismiss_restores_to_unread(conn):
    _add(conn, "m1")
    library.set_dismissed(conn, ["m1"], True)
    library.set_dismissed(conn, ["m1"], False)
    assert [a.message_id for a in library.list_unread(conn)] == ["m1"]


def test_upsert_preserves_dismissed(conn):
    _add(conn, "m1")
    library.set_dismissed(conn, ["m1"], True)
    _add(conn, "m1", score=9.0)
    assert library.get_article(conn, "m1").dismissed is True


def test_latest_digest_date_prefers_gmail(conn):
    _add(conn, "g1", date="2026-05-10")
    _add(conn, "u1", date="2026-05-12", source="url")
    assert library.latest_digest_date(conn) == "2026-05-10"


def test_latest_digest_date_falls_back_to_any_source(conn):
    assert library.latest_digest_date(conn) is None
    _add(conn, "u1", date="2026-05-12", source="url")
    assert library.latest_digest_date(conn) == "2026-05-12"


def test_list_unread_date_bounds(conn):
    _add(conn, "old", date="2026-05-09")
    _add(conn, "new", date="2026-05-10")
    assert [a.message_id for a in library.list_unread(conn, on_or_after="2026-05-10")] == ["new"]
    assert [a.message_id for a in library.list_unread(conn, before="2026-05-10")] == ["old"]


def test_count_unread_before(conn):
    _add(conn, "a", date="2026-05-08")
    _add(conn, "b", date="2026-05-09")
    _add(conn, "c", date="2026-05-10")
    library.set_dismissed(conn, ["a"], True)
    assert library.count_unread(conn, before="2026-05-10") == 1


def test_dismiss_backlog_only_touches_low_scored_unread_backlog(conn):
    _add(conn, "low_old", date="2026-05-09", score=3.0)
    _add(conn, "null_old", date="2026-05-09", score=None)
    _add(conn, "high_old", date="2026-05-09", score=8.0)
    _add(conn, "low_read", date="2026-05-09", score=2.0)
    library.set_read(conn, "low_read", True)
    _add(conn, "low_today", date="2026-05-10", score=1.0)
    ids = library.dismiss_backlog(conn, before="2026-05-10", max_score=5.0)
    assert sorted(ids) == ["low_old", "null_old"]
    assert library.get_article(conn, "high_old").dismissed is False
    assert library.get_article(conn, "low_read").dismissed is False
    assert library.get_article(conn, "low_today").dismissed is False


def test_state_map_reports_dismissed(conn):
    _add(conn, "m1")
    library.set_dismissed(conn, ["m1"], True)
    assert library.state_map(conn)["m1"]["dismissed"] is True
