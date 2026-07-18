"""Controlled-vocabulary tagging of stored articles for ``newsfeed retag``.

New articles are tagged inline by the scorer from their full body. This module
re-tags articles already in ``articles.db`` — the one-time backlog pass and every
later vocabulary edit — classifying from the text the DB retains (subject, stored
summary/one-liner, legacy topic), which carries enough signal to assign tags
without re-fetching the original email.

The returned tags are validated against the vocabulary, so an off-list label the
model invents is dropped rather than stored.
"""
import asyncio
import json
import logging
import sqlite3

from . import library
from .llm import LLMBackend
from .scorer import validate_tags

logger = logging.getLogger(__name__)

BATCH_SIZE = 20


def _row_text(article: library.Article) -> str:
    parts = [article.subject, article.topic, article.display_summary]
    return " — ".join(p for p in parts if p)


async def tag_articles(
    articles: list[library.Article],
    vocab: list[str],
    backend: LLMBackend,
) -> dict[str, list[str]]:
    """Return ``{message_id: [tags]}`` for the given articles, batched concurrently."""
    if not articles or not vocab:
        return {}
    batches = [articles[i : i + BATCH_SIZE] for i in range(0, len(articles), BATCH_SIZE)]
    logger.info(f"Tagging {len(articles)} articles across {len(batches)} batches")
    results = await asyncio.gather(*(_tag_batch(b, vocab, backend) for b in batches))
    merged: dict[str, list[str]] = {}
    for r in results:
        merged.update(r)
    return merged


async def _tag_batch(
    articles: list[library.Article], vocab: list[str], backend: LLMBackend
) -> dict[str, list[str]]:
    system_text = (
        "You tag newsletter articles for retrieval. Assign each article a subset "
        "of THIS fixed vocabulary only — never invent labels; use [] if none fit:\n"
        f"{', '.join(vocab)}\n\n"
        'Return only a JSON object: {"tags": [{"id": "...", "tags": ["..."]}]}'
    )
    payload = [{"id": a.message_id, "text": _row_text(a)} for a in articles]
    raw_text = await backend.acomplete(
        system_text, f"Tag these articles:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    parsed = _parse(raw_text)
    return {
        a.message_id: validate_tags(parsed.get(a.message_id), vocab) for a in articles
    }


def _parse(raw_text: str) -> dict[str, object]:
    raw = raw_text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        result = json.loads(raw)
        return {t["id"]: t.get("tags") for t in result["tags"]}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error(f"Failed to parse tagging response: {e}\nRaw: {raw_text[:300]}")
        return {}


async def retag(
    conn: sqlite3.Connection,
    articles: list[library.Article],
    vocab: list[str],
    backend: LLMBackend,
) -> int:
    """Recompute and store LLM tags for ``articles``; the reader overlay is untouched.

    Returns the number of articles re-tagged. ``library.effective_tags`` still
    layers each article's ``article_tag_delta`` on top of the fresh LLM set.
    """
    assigned = await tag_articles(articles, vocab, backend)
    for message_id, tags in assigned.items():
        library.set_llm_tags(conn, message_id, tags)
    conn.commit()
    return len(assigned)
