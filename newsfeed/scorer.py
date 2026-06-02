import json
import logging

from anthropic import Anthropic

from .models import Email, Preferences, ScoredEmail
from .parser import truncate_body

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
SCORE_BODY_LIMIT = 4000
BATCH_SIZE = 20


def score_emails(
    emails: list[Email],
    preferences: Preferences,
    client: Anthropic,
    examples: list[dict] | None = None,
) -> list[ScoredEmail]:
    if not emails:
        return []

    scored: list[ScoredEmail] = []
    for i in range(0, len(emails), BATCH_SIZE):
        batch = emails[i : i + BATCH_SIZE]
        logger.info(f"Scoring batch {i // BATCH_SIZE + 1} ({len(batch)} articles)")
        scored.extend(_score_batch(batch, preferences, client, examples or []))

    return scored


def _score_batch(
    emails: list[Email],
    preferences: Preferences,
    client: Anthropic,
    examples: list[dict],
) -> list[ScoredEmail]:
    from .feedback import format_examples

    examples_block = format_examples(examples)
    system_text = f"""You score newsletter articles for a reader based on their preferences.

{_format_preferences(preferences)}
{chr(10) + examples_block + chr(10) if examples_block else ""}
Scoring scale:
- 8-10: High interest (must read)
- 4-7: Medium interest (worth a look)
- 0-3: Low interest (can skip)

Return a JSON object with this exact structure:
{{
  "scores": [
    {{"message_id": "...", "interest_score": 7.5, "topic": "brief topic category", "one_line": "one sentence describing the article"}}
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

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Score these articles:\n{json.dumps(articles, ensure_ascii=False)}"}],
    )

    try:
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        scores_by_idx = {s["message_id"]: s for s in result["scores"]}
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse scoring response: {e}\nRaw: {response.content[0].text[:500]}")
        scores_by_idx = {}

    return [
        ScoredEmail(
            email=email,
            interest_score=float(scores_by_idx.get(str(i), {}).get("interest_score", 5.0)),
            topic=scores_by_idx.get(str(i), {}).get("topic", "General"),
            one_line=scores_by_idx.get(str(i), {}).get("one_line", email.subject),
        )
        for i, email in enumerate(emails)
    ]


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
