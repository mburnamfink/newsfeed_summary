import json

import pytest

from newsfeed import scorer
from newsfeed.llm import LLMBackend
from newsfeed.models import Email, Preferences


def _email(mid, subject="S", sender="Sender"):
    return Email(
        message_id=mid, sender_name=sender, sender_email="a@b.c",
        subject=subject, date=None, body="some body text",  # type: ignore[arg-type]
    )


VOCAB = ["artificial-intelligence", "history", "climate-energy"]


# --- pure helpers -----------------------------------------------------------


def test_validate_tags_drops_off_vocab():
    assert scorer.validate_tags(
        ["artificial-intelligence", "made-up", "history"], VOCAB
    ) == ["artificial-intelligence", "history"]


def test_validate_tags_dedupes_and_preserves_order():
    assert scorer.validate_tags(["history", "history", "climate-energy"], VOCAB) == [
        "history", "climate-energy",
    ]


@pytest.mark.parametrize("bad", [None, "history", 42, [1, 2]])
def test_validate_tags_handles_non_list_and_non_str(bad):
    assert scorer.validate_tags(bad, VOCAB) == []


def test_boost_no_interest_match_leaves_score():
    assert scorer.boost_for_interests(5.0, ["history"], ["artificial-intelligence"]) == 5.0


def test_boost_adds_per_matching_interest_tag():
    got = scorer.boost_for_interests(
        5.0, ["artificial-intelligence", "climate-energy"],
        ["artificial-intelligence", "climate-energy"],
    )
    assert got == 6.0  # 2 * 0.5


def test_boost_is_capped():
    tags = ["a", "b", "c", "d", "e"]
    assert scorer.boost_for_interests(5.0, tags, tags) == 5.0 + scorer.INTEREST_BOOST_MAX


def test_boost_never_exceeds_ten():
    assert scorer.boost_for_interests(9.9, ["a"], ["a"]) == 10.0


# --- batch scoring against a fake backend -----------------------------------


class FakeBackend(LLMBackend):
    def __init__(self, payload):
        super().__init__(max_concurrency=1)
        self._payload = payload

    async def _acomplete(self, system_text, prompt):
        return self._payload


async def test_score_batch_assigns_validated_tags_and_boost():
    payload = json.dumps({"scores": [
        {"message_id": "0", "interest_score": 7.0, "topic": "AI", "one_line": "x",
         "tags": ["artificial-intelligence", "bogus"]},
    ]})
    prefs = Preferences(
        gmail_labels=[], interests=["artificial-intelligence"],
        thresholds={"high": 7, "medium": 4}, tags=VOCAB,
    )
    scored = await scorer.score_emails([_email("m0")], prefs, FakeBackend(payload))
    assert scored[0].tags == ["artificial-intelligence"]   # bogus dropped
    assert scored[0].interest_score == 7.5                  # 7.0 + 0.5 interest boost


async def test_score_batch_survives_bad_json():
    prefs = Preferences(gmail_labels=[], interests=[], thresholds={"high": 7, "medium": 4}, tags=VOCAB)
    scored = await scorer.score_emails([_email("m0", subject="Fallback")], prefs, FakeBackend("not json"))
    assert scored[0].interest_score == 5.0
    assert scored[0].one_line == "Fallback"
    assert scored[0].tags == []
