# ADR 0007 — Long-form article summaries (on demand)

## Status
Accepted

## Context
The pipeline already summarizes newsletters, but only for the card: a single
sentence for medium-tier items, a 3-5 sentence paragraph for high-tier ones
(`summarizer.py`). That is a triage aid — enough to decide *whether* to read —
not a substitute for the article.

Two recurring situations aren't served by that:

1. **Too long / too verbose.** A genuinely interesting piece that's 4,000 words of
   throat-clearing. The reader wants its substance in a form they can actually get
   through — roughly 1000-2000 words, faithful enough to trust *instead of* the
   original.
2. **Teasers.** Some sources (e.g. Phenomenal World) email only the opening
   paragraphs of a longer story, with a link to the full text. The stored body is
   a stub; summarizing it summarizes nothing.

The card summary is also the wrong quality tier for this. It runs on the digest's
cheap workhorse model (Haiku on the API, Sonnet on the subscription) and is
truncated to 8,000 characters of body — fine for a one-liner, useless for a
faithful condensation of a long argument.

What already exists to build on: `articles.db` (ADR 0002) as the single source of
truth keyed by `message_id`; the Archive Server's JSON write endpoints and the
three display surfaces (Reader ADR 0006, Library pages, digest); `ingest.fetch_url`
(ADR 0005), which turns any URL into full extracted text; and the pluggable
`LLMBackend` (`llm.py`) whose model is configurable per backend.

## Decision
Add an **on-demand long-form summarizer**: one 1000-2000 word Markdown summary per
article, generated on request with a strong model, persisted, and surfaced on every
card for that article.

**1. A dedicated model and prompt.** Summaries use their own LLM config
(`config.summary_config()`), defaulting to **Opus 4.8 on the subscription backend**
regardless of the digest's cheaper default; override with a `summary:` block in
`preferences.yaml`. The prompt (`deep_summary.SYSTEM_PROMPT`) is strict about
fidelity: use only the provided text, preserve numbers / names / quotes / the
author's stance, attribute claims as the source does, scale length to the source
(never pad), and flag an excerpt when the input is clearly incomplete. Output is
Markdown (a `## TL;DR` plus 2-5 sections). Unlike the card summarizer, the body is
capped at 200,000 chars (a safety bound, not a real trim — Opus 4.8 has a 1M-token
context).

**2. Its own table, so a re-run can't clobber it.**

```sql
CREATE TABLE article_summaries (
    message_id TEXT PRIMARY KEY,
    summary_md TEXT NOT NULL,
    model      TEXT NOT NULL,
    source     TEXT NOT NULL,   -- 'body' | 'url' (full text re-fetched)
    word_count INTEGER,
    created_at TEXT NOT NULL
);
```

A separate table (not a column on `articles`) keeps the summary entirely out of
the `upsert_scored` path, which overwrites every pipeline-derived column on each
digest re-run. It also carries provenance. `Article.has_summary` is populated on
read so cards render the right control.

**3. Teaser handling via re-fetch.** When the stored body is below
`TEASER_CHARS` (or the article is flagged paywalled) *and* the article has a URL,
`deep_summary` re-fetches the full text with `ingest.fetch_url` before
summarizing, recording `source='url'`. For a hard paywall the re-fetch is also
thin, and the prompt's excerpt note fires — an honest summary of what's available.

**4. Two entry points, one write path.**
- `POST /api/summarize {message_id}` generates synchronously and stores the result.
  The model call (and any re-fetch) runs **off** the server's write lock; the lock
  is taken only for the final write. `GET /reader/summary/<message_id>` renders the
  stored Markdown as a standalone reading page.
- `newsfeed summarize <url-or-message_id>` (CLI) does the same for a stored article,
  and for a not-yet-saved URL first captures it through the identical `add`
  pipeline (scored, tagged, starred) so the summary attaches to a real row. Like
  `add`, it detaches to the background.

**5. Surfaced near the original, everywhere.** Because it keys off `message_id` and
`has_summary`, a **📄 Summarize** control appears on every card — Reader, Library
lists, author/tag pages, the Saved shelf, and the digest — and becomes **📄
Summary** once one exists. The Reader opens it in place; the Library and digest
open `/reader/summary/<id>`. The digest, rendered from fresh scores, learns which
articles are summarized from the existing `/api/state` hydration call (extended
with a `summary` flag).

## Consequences
- Synchronous generation means the button spins for 15-40s. Acceptable for a
  single-user server; each request is served on its own thread and never holds the
  write lock during the model call. An async job model was deemed unnecessary
  complexity at this scale.
- Summaries are never regenerated automatically — a digest re-run refreshes scores
  and card summaries but leaves `article_summaries` untouched. Re-running
  `summarize` on an article replaces its summary in place.
- Adds one hard dependency, `markdown`, to render stored Markdown to HTML
  server-side (keeping the client free of a Markdown library).
- The summary is faithful to the *captured* text. For a hard paywall that yields
  only a teaser even after re-fetch, the summary is of the teaser (and says so).
