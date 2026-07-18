import json

from newsfeed import library, tagger
from newsfeed.llm import LLMBackend

VOCAB = ["artificial-intelligence", "history", "climate-energy"]


class FakeBackend(LLMBackend):
    def __init__(self, mapping):
        super().__init__(max_concurrency=1)
        self._mapping = mapping

    async def _acomplete(self, system_text, prompt):
        # Echo back the requested ids with canned tags from the mapping.
        payload = json.loads(prompt.split("\n", 1)[1])
        return json.dumps({"tags": [
            {"id": item["id"], "tags": self._mapping.get(item["id"], [])}
            for item in payload
        ]})


def _add(conn, mid, subject="S"):
    library.upsert_article(
        conn, message_id=mid, date="2026-05-10", sender_name="Author",
        subject=subject, summary="a summary", topic="t", score=6.0,
    )


async def test_tag_articles_validates_against_vocab():
    conn = library.connect(":memory:")
    _add(conn, "m1")
    arts = [library.get_article(conn, "m1")]
    backend = FakeBackend({"m1": ["artificial-intelligence", "not-a-tag"]})
    got = await tagger.tag_articles(arts, VOCAB, backend)
    assert got == {"m1": ["artificial-intelligence"]}
    conn.close()


async def test_retag_replaces_llm_tags_but_keeps_delta():
    conn = library.connect(":memory:")
    _add(conn, "m1")
    library.set_llm_tags(conn, "m1", ["climate-energy"])
    library.apply_tag_delta(conn, "m1", "history", "add")   # reader overlay

    arts = [library.get_article(conn, "m1")]
    n = await tagger.retag(conn, arts, VOCAB, FakeBackend({"m1": ["artificial-intelligence"]}))
    assert n == 1
    # LLM set replaced; reader's added tag survives
    assert library.effective_tags(conn, "m1") == ["artificial-intelligence", "history"]
    conn.close()


async def test_tag_articles_empty_vocab_is_noop():
    conn = library.connect(":memory:")
    _add(conn, "m1")
    arts = [library.get_article(conn, "m1")]
    assert await tagger.tag_articles(arts, [], FakeBackend({})) == {}
    conn.close()
