"""Estimated reading time from body text — no model call, so it applies
uniformly to every stored article and can be back-filled onto old digests."""

import re

# Middle of the commonly cited 200–250 wpm adult prose range; newsletters skew
# denser than pulp, so the lower end keeps estimates from reading too fast.
WORDS_PER_MINUTE = 200

_WORD_RE = re.compile(r"\S+")


def reading_minutes(text: str) -> int:
    """Whole-minute reading estimate for ``text``; 0 when there's no body.

    Rounds to the nearest minute with a 1-minute floor, so any real body reads
    as at least "1 minute" rather than "0".
    """
    words = len(_WORD_RE.findall(text or ""))
    if words == 0:
        return 0
    return max(1, round(words / WORDS_PER_MINUTE))
