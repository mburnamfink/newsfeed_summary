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
