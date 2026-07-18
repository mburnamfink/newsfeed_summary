# ADR 0002 — SQLite Index as the Single Source of Truth for the Library

## Status
Accepted

## Context
The tool retains everything it fetches: every newsletter is archived self-contained
(ADR 0001) and logged. But the retained data is fragmented and unqueryable:

- `feedback.yaml` — a flat log keyed on `(subject, sender)`. It stores date, topic,
  score, and a reaction, but **not** `message_id` or `archive_path`. It began as a
  manual-era, hand-edited artifact; the reaction taps on the Digest have since
  superseded hand-editing.
- `read_state.json` — a separate set of read `message_id`s.
- `serve/archive/YYYY-MM-DD/{message_id}/` — the bodies, on disk, keyed by `message_id`.
- `serve/digests/*.html` — rendered pages carrying `message_id`, sender, score, topic,
  summary, and archive links per article.

Nothing joins these. The reader wants a personal retrieval library — browse every
article by author (e.g. all Max Read over time, scanning stored summaries), browse by
topic, full-text search, and mark a curated subset to follow up on. None of that is a
capability the current stores can serve: multi-valued tag queries, full-text over
bodies, and cross-cutting filters are not something a `(subject, sender)`-keyed YAML
log and a JSON set can answer.

Alternatives considered:
1. **Extend the flat files + generate static browse pages.** Add tags/star to
   `feedback.yaml`, regenerate `/authors/*.html` and `/tags/*.html` each run. No new
   dependency, but multi-tag and full-text queries over a growing YAML file are
   hand-rolled and O(n), full-text search over bodies is not feasible, and every store
   still drifts against the others.
2. **Keep the flat files authoritative; rebuild a throwaway DB from them each run.**
   Preserves the plain-text workflow, but the DB can hold nothing the YAML doesn't
   (tags, star, body) unless those go back into the YAML too — which just moves the
   mess. Rejected once we established the plain-text workflow is a fossil, not a value.
3. **Add semantic (embedding) search now.** More powerful, but adds an embedding call
   per article and a vector store before we know we need it. Deferred; FTS5 keyword
   search is enough to start and layers cleanly under embeddings later.

## Decision
Introduce `articles.db`, a SQLite database (stdlib `sqlite3`, no new dependency) as the
**single source of truth** for the Library, keyed by `message_id`. All per-article
state — score, tags, star, read, feedback, summary, `archive_path`, and body text
(indexed with FTS5) — lives there. `feedback.yaml` and `read_state.json` are retired as
write targets; the scorer reads its calibration examples from the DB.

A one-time migration backfills the full history: digest HTML supplies per-article rows
including `message_id` and `archive_path`, archived HTML supplies body text for FTS,
`feedback.yaml` reactions are matched on `(subject, sender)`, and `read_state.json`
supplies read flags. The backlog is then tagged in one pass.

Tags are drawn from a controlled, user-owned **Tag Vocabulary** (a `tags:` block in
`preferences.yaml`, seeded from `interests`). The LLM assigns tags constrained to the
vocabulary. An article's effective tags are `(LLM tags ∪ reader-added) − reader-removed`;
the reader's add/remove corrections are a durable overlay re-applied on every retag, so
hand-fixes survive while new vocabulary terms still flow into old articles. Vocabulary
edits are applied on demand via `newsfeed retag [--all | --since DATE | --tag X]`, never
silently.

The Library is **retrieval only** — browse, filter, search, and scan stored per-article
summaries. No LLM synthesis or cross-article generation is in scope.

## Implementation Notes
The Archive Server gains dynamic `/library` query views (landing with search box +
author/tag facets + starred filter; `/library/author/<sender>`, `/library/tag/<tag>`,
`/library/search?q=`) that `SELECT` from `articles.db` and link through to the existing
offline archives. The write endpoints (`/api/rate`, `/api/mark-read`, plus new
star/tag-edit endpoints) write to the DB instead of the flat files. The pipeline upserts
one row per article keyed by `message_id` (finally a stable key, unlike the old
`(subject, sender)`). Low-tier articles have no paragraph summary and fall back to
`one_line` on browse pages, as the Digest already does.

## Consequences
- State is no longer hand-editable in a text editor or diffable in git; inspection/edit
  is via the Library UI, the server endpoints, or `sqlite3`.
- `message_id` becomes the identity key. The backfill must reconcile `feedback.yaml`'s
  `(subject, sender)` rows to `message_id`s via the digest-derived mapping; rare
  `(subject, sender)` collisions may mis-attach a reaction.
- One-time LLM cost to tag the ~570-article backlog; ongoing cost is one tagging step
  per run (foldable into scoring) plus occasional `retag` passes.
- The DB is a new file to back up. Losing it loses tags/stars/feedback, though articles
  remain re-derivable from the archives and digests.
- Semantic search is not available, only FTS5 keyword search; embeddings can be added as
  a column/table later without changing the source-of-truth model.
