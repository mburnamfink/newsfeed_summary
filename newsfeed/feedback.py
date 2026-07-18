import logging
import math
import random
from datetime import date

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 50
WEIGHT_DISAGREEMENT = 0.5
WEIGHT_RECENCY = 0.3
WEIGHT_RANDOM = 0.2
RECENCY_HALFLIFE_DAYS = 30


def select_examples_from_rows(
    entries: list[dict],
    max_n: int = MAX_EXAMPLES,
    weight_disagreement: float = WEIGHT_DISAGREEMENT,
    weight_recency: float = WEIGHT_RECENCY,
    weight_random: float = WEIGHT_RANDOM,
    recency_halflife_days: int = RECENCY_HALFLIFE_DAYS,
) -> list[dict]:
    """Weight and pick calibration examples from feedback rows (DB- or YAML-shaped).

    Each row needs ``date``, ``subject``, ``sender``, ``topic``, ``score`` and
    ``feedback`` keys — the shape the scorer's example formatter consumes.
    """
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
        disagreement = _disagreement(e.get("feedback"), e.get("score"))
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


# Feedback is a coarse reaction the reader taps on a digest page: "up" (undervalued,
# want more), "down" (overvalued, want less), or "confirmed" (score was right).
# null means no reaction was given. Older history may store a numeric corrected score.
def _disagreement(feedback: str | float | None, score: float | None) -> float:
    """0.0 (score stood) … 1.0 (reader pushed hard against it)."""
    if feedback is None or feedback == "confirmed":
        return 0.0
    if feedback in ("up", "down"):
        return 1.0
    if score is None:
        return 0.0
    try:
        return abs(float(feedback) - float(score)) / 10.0
    except (ValueError, TypeError):
        return 0.0


def _example_line(e: dict) -> str:
    head = f'- "{e["subject"]}" ({e["sender"]}) | topic: {e["topic"]} | system scored {e["score"]}'
    fb = e.get("feedback")
    if fb == "up":
        return f"{head} — reader wanted it ranked HIGHER (surface more like this)"
    if fb == "down":
        return f"{head} — reader wanted it ranked LOWER (less like this)"
    if fb == "confirmed":
        return f"{head} — reader confirmed the score was right"
    if fb is None:
        return f"{head} (not corrected)"
    direction = "too low" if float(fb) > float(e["score"]) else "too high"
    return f"{head}, reader said {fb} ({direction})"


def format_examples(examples: list[dict]) -> str:
    if not examples:
        return ""
    lines = ["Calibration examples from your past feedback (use to anchor the scoring scale):"]
    lines.extend(_example_line(e) for e in examples)
    return "\n".join(lines)
