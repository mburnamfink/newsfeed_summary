import asyncio
import json
import logging

from .llm import LLMBackend
from .models import Email, Preferences, ScoredEmail
from .parser import truncate_body

logger = logging.getLogger(__name__)

SCORE_BODY_LIMIT = 4000
BATCH_SIZE = 20

# Fallback when the model gives no usable score. Thin or unscorable articles are
# treated as low value rather than getting a neutral 5.0, so they sort to the bottom.
DEFAULT_LOW_SCORE = 2.0

# An article whose tags land in the reader's interests gets a bounded nudge, so
# the vocabulary drives ranking rather than only living in the prose prompt. Each
# matching interest tag adds INTEREST_BOOST_PER_TAG, capped at INTEREST_BOOST_MAX
# to keep a heavily-tagged article from pinning to 10.
INTEREST_BOOST_PER_TAG = 0.5
INTEREST_BOOST_MAX = 1.5


def validate_tags(raw_tags: object, vocab: list[str]) -> list[str]:
    """Keep only in-vocabulary tags, de-duplicated and order-preserving.

    The LLM is constrained to the vocabulary but can still return junk; anything
    off-list is dropped rather than trusted.
    """
    allowed = set(vocab)
    if not isinstance(raw_tags, list):
        return []
    return [t for t in dict.fromkeys(raw_tags) if isinstance(t, str) and t in allowed]


def boost_for_interests(score: float, tags: list[str], interests: list[str]) -> float:
    """Nudge the score up when an article's tags intersect the reader's interests."""
    matches = len(set(tags) & set(interests))
    if not matches:
        return score
    boost = min(matches * INTEREST_BOOST_PER_TAG, INTEREST_BOOST_MAX)
    return min(10.0, score + boost)


async def score_emails(
    emails: list[Email],
    preferences: Preferences,
    backend: LLMBackend,
    examples: list[dict] | None = None,
) -> list[ScoredEmail]:
    if not emails:
        return []

    batches = [emails[i : i + BATCH_SIZE] for i in range(0, len(emails), BATCH_SIZE)]
    logger.info(f"Scoring {len(emails)} articles across {len(batches)} batches concurrently")
    results = await asyncio.gather(
        *(_score_batch(batch, preferences, backend, examples or []) for batch in batches)
    )
    return [scored for batch in results for scored in batch]


async def _score_batch(
    emails: list[Email],
    preferences: Preferences,
    backend: LLMBackend,
    examples: list[dict],
) -> list[ScoredEmail]:
    from .feedback import format_examples

    examples_block = format_examples(examples)
    vocab = preferences.tags
    vocab_block = (
        f"\nTag vocabulary (assign each article a subset of THESE labels only — never "
        f"invent new ones; use [] if none fit):\n{', '.join(vocab)}\n"
        if vocab
        else ""
    )
    system_text = f"""You score newsletter articles for a reader based on their preferences.

{_format_preferences(preferences)}
{chr(10) + examples_block + chr(10) if examples_block else ""}{vocab_block}
Scoring scale:
- 8-10: High interest (must read)
- 4-7: Medium interest (worth a look)
- 0-3: Low interest (can skip)

Every article MUST get a numeric interest_score from 0 to 10 — never null, never
omitted. If an article is thin, promotional, or you are unsure, commit to a low
score (0-3) rather than leaving it blank.

Return a JSON object with this exact structure:
{{
  "scores": [
    {{"message_id": "...", "interest_score": 7.5, "topic": "brief topic category", "one_line": "one sentence describing the article", "tags": ["tag-from-vocabulary"]}}
  ]
}}

Return only the JSON object, no other text."""

    articles = [
        {
            "id": str(i),
            "sender": e.sender_name,
            "subject": e.subject,
            "body": truncate_body(e.body, SCORE_BODY_LIMIT),
        }
        for i, e in enumerate(emails)
    ]

    raw_text = await backend.acomplete(
        system_text,
        f"Score these articles:\n{json.dumps(articles, ensure_ascii=False)}",
    )

    try:
        raw = raw_text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        scores_by_idx = {s["message_id"]: s for s in result["scores"]}
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse scoring response: {e}\nRaw: {raw_text[:500]}")
        scores_by_idx = {}

    results = []
    for i, email in enumerate(emails):
        s = scores_by_idx.get(str(i), {})
        tags = validate_tags(s.get("tags"), preferences.tags)
        raw_score = s.get("interest_score")
        try:
            interest = float(raw_score)
        except (TypeError, ValueError):
            logger.warning(
                f"Unusable interest_score {raw_score!r} for '{email.subject}' "
                f"from {email.sender_name}; defaulting to {DEFAULT_LOW_SCORE}"
            )
            interest = DEFAULT_LOW_SCORE
        score = boost_for_interests(interest, tags, preferences.interests)
        results.append(
            ScoredEmail(
                email=email,
                interest_score=score,
                topic=s.get("topic", "General"),
                one_line=s.get("one_line", email.subject),
                tags=tags,
            )
        )
    return results


def _format_preferences(prefs: Preferences) -> str:
    lines = [f"Interests: {', '.join(prefs.interests)}"]
    if prefs.boost_sources:
        lines.append(f"Preferred sources (boost score): {', '.join(prefs.boost_sources)}")
    if prefs.mute_sources:
        lines.append(f"Muted sources (lower score): {', '.join(prefs.mute_sources)}")
    if prefs.boost_keywords:
        lines.append(f"Boost keywords: {', '.join(prefs.boost_keywords)}")
    if prefs.mute_keywords:
        lines.append(f"Mute keywords: {', '.join(prefs.mute_keywords)}")
    return "\n".join(lines)
