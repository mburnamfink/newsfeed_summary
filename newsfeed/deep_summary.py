"""On-demand long-form article summaries (ADR 0007).

The digest summarizer (:mod:`newsfeed.summarizer`) writes a sentence or a short
paragraph for the reader card. This module writes a standalone 1000-2000 word
summary of a single article on request — a faithful condensation to read instead
of a long or verbose original. Summaries persist in ``article_summaries`` and
surface on every card for that article (Reader, Library, digest).

Teaser sources: some newsletters (e.g. Phenomenal World) carry only the opening
paragraphs of a longer story. When the stored body is that thin and the article
has a URL, the full text is re-fetched with :func:`newsfeed.ingest.fetch_url`
before summarizing; ``source`` records which text was used ('body' vs 'url').
"""
import logging
from dataclasses import dataclass

from . import ingest, library
from .config import summary_config
from .library import Article
from .llm import LLMBackend, build_backend

logger = logging.getLogger(__name__)

# Opus 4.8 has a 1M-token context, so cap only to bound a pathological page —
# not to trim real articles the way the digest summarizer's 8k char limit does.
BODY_LIMIT = 200_000
# At or below this many characters the stored body is treated as a teaser: if the
# article carries a URL, the full text is re-fetched before summarizing.
TEASER_CHARS = 1_500
# ~2000 words is ~2.7k output tokens; leave headroom for adaptive thinking on the
# API backend (the subscription backend ignores this — see llm.py).
MAX_TOKENS = 8_000
EFFORT = "medium"

SYSTEM_PROMPT = """You are a meticulous reading assistant. You produce faithful, \
self-contained summaries of articles for a well-informed reader who wants the \
full substance without the length.

Write the summary in Markdown, structured as:
- A bold **TL;DR** line (<=40 words) with the single most important takeaway.
- 2-5 sections under `##` headings that follow the article's own line of \
argument, in prose. Use bullet lists only where the source itself enumerates.
- If the piece builds to a conclusion, recommendation, or forecast, end on it.

Length: aim for 1000-2000 words for a long, dense article. Scale down for \
shorter sources — a summary is always substantially shorter than what it \
summarizes, and you must NEVER pad to reach a word count.

Fidelity rules (these override everything else):
- Use ONLY information present in the provided text. Add no facts, context, \
examples, or figures from your own knowledge, even when you know them to be true.
- Preserve the specifics that carry the article's weight: key numbers, dates, \
names, quotations, causal claims, and the author's stance and hedges.
- Attribute claims as the article does ("the author argues", "the study found"). \
Never upgrade a claim's certainty or take a side the article doesn't take.
- If the text is clearly incomplete — a teaser, an excerpt, or only the opening \
paragraphs of a longer piece — put one italic note at the very top (e.g. *Note: \
the source appears to be an excerpt; this summarizes only the available text.*) \
and summarize what is present.

Output only the Markdown summary — no preamble, no "Here is", no remarks about \
the summary itself."""


@dataclass
class SummaryResult:
    summary_md: str
    model: str
    source: str  # 'body' | 'url'
    word_count: int


def _user_prompt(subject: str, sender: str, url: str, text: str) -> str:
    src = sender + (f" · {url}" if url else "")
    return (
        f"Summarize the following article.\n\n"
        f"Title: {subject}\n"
        f"Source: {src}\n"
        f"---\n{text}\n---"
    )


def _build_backend() -> LLMBackend:
    """The summary backend from config. Split out so tests can substitute it."""
    return build_backend(summary_config())


async def _resolve_text(article: Article, body: str) -> tuple[str, str]:
    """Return ``(text, source)``, re-fetching full text for thin/paywalled sources."""
    text, source = body, "body"
    if article.url and (len(body) < TEASER_CHARS or article.paywalled):
        try:
            email = await ingest.fetch_url(article.url)
        except Exception as e:
            logger.warning(f"Full-text re-fetch failed for {article.url}: {e}")
            email = None
        if email and len(email.body) > len(text):
            text, source = email.body, "url"
    return text[:BODY_LIMIT], source


async def generate(article: Article, body: str, *, backend: LLMBackend | None = None) -> SummaryResult:
    """Write a long-form summary of ``article`` (does not persist it)."""
    backend = backend or _build_backend()
    text, source = await _resolve_text(article, body)
    if not text.strip():
        raise ValueError("No article text available to summarize.")

    summary_md = (await backend.acomplete(
        SYSTEM_PROMPT,
        _user_prompt(article.subject, article.sender_name, article.url, text),
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        effort=EFFORT,
    )).strip()
    if not summary_md:
        raise ValueError("The model returned an empty summary.")

    model = getattr(backend, "model", "unknown")
    return SummaryResult(summary_md, model, source, len(summary_md.split()))


async def summarize_message(conn, message_id: str, *, backend: LLMBackend | None = None) -> SummaryResult:
    """Load ``message_id`` and summarize it. Raises KeyError if it isn't stored.

    The result is returned, not persisted — the caller owns the write (and its
    transaction/lock), matching how the server serialises DB writes.
    """
    article = library.get_article(conn, message_id)
    if article is None:
        raise KeyError(message_id)
    return await generate(article, library.get_body(conn, message_id), backend=backend)
