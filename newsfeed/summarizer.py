import asyncio
import json
import logging

from .llm import LLMBackend
from .models import Preferences, ScoredEmail
from .parser import truncate_body

logger = logging.getLogger(__name__)

SUMMARIZE_BODY_LIMIT = 8000


async def summarize_emails(
    scored: list[ScoredEmail],
    preferences: Preferences,
    backend: LLMBackend,
) -> list[ScoredEmail]:
    high = [s for s in scored if s.tier == "high"]
    medium = [s for s in scored if s.tier == "medium"]

    tasks = []
    if high:
        tasks.append(_summarize_batch(high, "paragraph", backend))
    if medium:
        tasks.append(_summarize_batch(medium, "sentence", backend))
    await asyncio.gather(*tasks)

    if high:
        logger.info(f"Summarized {len(high)} high-interest articles")
    if medium:
        logger.info(f"Summarized {len(medium)} medium-interest articles")

    return scored


async def _summarize_batch(items: list[ScoredEmail], length: str, backend: LLMBackend) -> None:
    instruction = (
        "a paragraph (3-5 sentences) capturing the key points and why it matters"
        if length == "paragraph"
        else "a single sentence summarizing the main point"
    )

    system_text = (
        f"You summarize newsletter articles. For each article, write {instruction}. "
        "Return only a JSON object, no other text."
    )

    articles = [
        {
            "id": str(i),
            "subject": item.email.subject,
            "sender": item.email.sender_name,
            "body": truncate_body(item.email.body, SUMMARIZE_BODY_LIMIT),
        }
        for i, item in enumerate(items)
    ]

    prompt = (
        f"Summarize these newsletter articles:\n"
        f"{json.dumps(articles, ensure_ascii=False)}\n\n"
        'Return a JSON object: {"summaries": [{"message_id": "...", "summary": "..."}]}'
    )

    raw_text = await backend.acomplete(system_text, prompt)

    try:
        raw = raw_text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        summaries_by_idx = {s["message_id"]: s["summary"] for s in result["summaries"]}
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse summarization response: {e}")
        summaries_by_idx = {}

    for i, item in enumerate(items):
        item.summary = summaries_by_idx.get(str(i), item.one_line)
