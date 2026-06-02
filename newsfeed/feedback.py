import logging
import math
import random
from datetime import date, datetime
from pathlib import Path

import yaml

from .models import ScoredEmail

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 50
WEIGHT_DISAGREEMENT = 0.5
WEIGHT_RECENCY = 0.3
WEIGHT_RANDOM = 0.2
RECENCY_HALFLIFE_DAYS = 30


def append_run(scored: list[ScoredEmail], target_date: date, path: Path) -> None:
    existing = _load_raw(path)
    existing_keys = {(e["subject"], e["sender"]) for e in existing}

    new_entries = [
        {
            "date": target_date.isoformat(),
            "subject": s.email.subject,
            "sender": s.email.sender_name,
            "topic": s.topic,
            "score": round(s.interest_score, 1),
            "feedback": None,
        }
        for s in scored
        if (s.email.subject, s.email.sender_name) not in existing_keys
    ]

    if not new_entries:
        return

    combined = new_entries + existing
    path.write_text(yaml.dump(combined, allow_unicode=True, sort_keys=False, default_flow_style=False), encoding="utf-8")
    logger.info(f"Appended {len(new_entries)} new entries to {path.name}")


def select_examples(
    path: Path,
    max_n: int = MAX_EXAMPLES,
    weight_disagreement: float = WEIGHT_DISAGREEMENT,
    weight_recency: float = WEIGHT_RECENCY,
    weight_random: float = WEIGHT_RANDOM,
    recency_halflife_days: int = RECENCY_HALFLIFE_DAYS,
) -> list[dict]:
    entries = _load_raw(path)
    if not entries:
        return []

    today = date.today()
    scored = []
    for e in entries:
        try:
            entry_date = date.fromisoformat(str(e["date"]))
        except (ValueError, TypeError):
            entry_date = today

        days_ago = max(0, (today - entry_date).days)
        # null feedback = user confirmed the score was correct, disagreement = 0
        disagreement = abs(float(e["feedback"]) - float(e["score"])) / 10.0 if e.get("feedback") is not None else 0.0
        recency = math.exp(-days_ago / recency_halflife_days)
        rnd = random.random()

        priority = (
            weight_disagreement * disagreement
            + weight_recency * recency
            + weight_random * rnd
        )
        scored.append((priority, e))

    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:max_n]]


def format_examples(examples: list[dict]) -> str:
    if not examples:
        return ""
    lines = ["Calibration examples from your past feedback (use to anchor the scoring scale):"]
    for e in examples:
        if e.get("feedback") is None:
            lines.append(
                f'- "{e["subject"]}" ({e["sender"]}) | topic: {e["topic"]} | '
                f'system scored {e["score"]}, confirmed correct'
            )
        else:
            direction = "too low" if float(e["feedback"]) > float(e["score"]) else "too high"
            lines.append(
                f'- "{e["subject"]}" ({e["sender"]}) | topic: {e["topic"]} | '
                f'system scored {e["score"]}, you said {e["feedback"]} ({direction})'
            )
    return "\n".join(lines)


def _load_raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Could not load {path.name}: {e}")
        return []
