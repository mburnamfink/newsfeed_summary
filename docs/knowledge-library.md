# Knowledge Library — Design

Turn the daily-digest pipeline into a personal retrieval library: browse every
newsletter by author and by tag, full-text search bodies, star a curated subset to
follow up on. Retrieval only — no LLM synthesis.

This doc is self-contained: it assumes only the current codebase (Gmail → score →
archive → summarize → render digest → serve over LAN) plus ADR 0001 (Archive Server)
and ADR 0002 (SQLite source of truth). Read those two ADRs and `CONTEXT.md` first.

## Decisions (settled)

1. **Hybrid curation.** Every fetched article is retained and indexed (the *Library*);
   a one-tap *star* marks the curated subset. Star is orthogonal to read-state and to
   feedback.
2. **Controlled-vocabulary tags.** Tags come from a fixed, user-owned *Tag Vocabulary*
   (`tags:` in `preferences.yaml`, seeded from `interests`). The LLM assigns tags
   constrained to the vocabulary — it cannot invent labels. This replaces the messy
   free-text `topic` for retrieval (topic may stay as a display string).
3. **SQLite (`articles.db`, FTS5) is the single source of truth.** Retire
   `feedback.yaml` and `read_state.json` as write targets. Keyed by `message_id`.
4. **Full backfill.** Reconstruct history from digests + archives + feedback + read
   state, then tag the backlog in one pass. Library is deep from day one.
5. **No synthesis.** "Max Read over time" means a page listing that author's articles
   with their already-stored summaries to scan — not LLM-generated prose.
6. **Vocabulary edits apply on demand** via `newsfeed retag`, never silently.
7. **Per-article tag correction is supported**, but tags are mostly machine-driven.
8. **Layered tag overrides.** Effective tags = `(LLM tags ∪ reader-added) − reader-removed`.
   Retag recomputes LLM tags freely and re-applies the reader's delta on top.

## Initial Tag Vocabulary

Curated from the 604-article backlog (clustering the free-text `topic` field) and
populated in the `tags:` block of `preferences.yaml`. 22 tags covering ~96% of history.
Shares below are the fraction of backlog articles that would carry each tag.

Boundary rules the tagging prompt must encode (the non-obvious ones):

- **`software-engineering`** (18%) vs **`software-teams`** (8%): "building software" vs
  "the people who build software". Craft — code, languages, architecture, tooling,
  algorithms — is `software-engineering`. Org — engineering management, leadership,
  workplace culture, tech hiring and careers — is `software-teams`. An article may carry
  both. (This is where "hiring trends" lives.)
- **`technology`** (8%) is a *residual*: tech developments that are **not** AI, software,
  or infrastructure (hardware, robotics, biotech, science advances). Without this
  exclusion it collapses into `artificial-intelligence`/`software-engineering` and adds
  noise.
- **`accountability`** (16%) absorbs the reader's signature `"bad things happening to bad
  people"` — corruption, fraud, litigation, exposés.
- **`geopolitics`** (12%) is mostly Israel/Palestine, which the reader *mutes* as a
  scoring keyword. The tag is for retrieval, not endorsement — muting lowers the score;
  the tag still lets those articles be found later. (Tension worth remembering.)

Non-content promotional emails (`event announcement`, `newsletter subscription
promotion`, sales) are the bulk of the untagged tail; handle by muting/filtering, not by
adding vocabulary.

## Data model

`articles.db` (stdlib `sqlite3`), one row per article:

```
articles
  message_id     TEXT PRIMARY KEY     -- stable Gmail id; replaces (subject,sender)
  date           TEXT                 -- ISO date the article was fetched for
  sender_name    TEXT
  sender_email   TEXT
  subject        TEXT
  one_line       TEXT
  summary        TEXT                 -- paragraph (high/med) or "" ; UI falls back to one_line
  topic          TEXT                 -- legacy free-text, display only
  score          REAL
  archive_path   TEXT                 -- server-relative offline copy
  paywalled      INTEGER
  starred        INTEGER DEFAULT 0
  read           INTEGER DEFAULT 0
  feedback       TEXT                 -- up | down | confirmed | NULL

article_llm_tags   (message_id, tag)  -- recomputed on every retag
article_tag_delta  (message_id, tag, op)   -- op: 'add' | 'remove'  (reader overlay)

articles_fts  -- FTS5 over body text, external-content joined on message_id
```

Effective tags for a row: `(llm_tags ∪ delta.add) − delta.remove`, computed in a view
or query helper. Never store the effective set flat — always derive it.

## Backfill (one-time migration)

Order matters; each step feeds the next.

1. **Rows + `message_id` + `archive_path`** — parse every `serve/digests/*.html`. Each
   card/li carries `data-msgid`, `data-subject`, `data-sender`, the score badge, topic,
   summary, and the archive link. This is the richest structured source and the only one
   that maps `(subject, sender)` ↔ `message_id`.
2. **Body text** — read each `serve/archive/<date>/<message_id>/index.html`, strip to
   text, load into `articles_fts`.
3. **Feedback reactions** — for each `feedback.yaml` row, match on `(subject, sender)`
   to the digest-derived `message_id` and set `feedback`. Log unmatched rows;
   `(subject, sender)` collisions are a known small risk.
4. **Read flags** — `read_state.json` is already `message_id`-keyed; set `read`.
5. **Tag the backlog** — run the controlled-vocab tagging pass over all rows to populate
   `article_llm_tags`. One-time LLM cost (~570 articles, batched like scoring).

After migration, `feedback.yaml` and `read_state.json` are no longer written. Keep the
files on disk as an inert backup; do not delete in the same change.

## Pipeline changes

- **Tagging step.** Add a tagging pass constrained to the Tag Vocabulary. Cheapest to
  fold into the existing batched scorer call (return `tags: [...]` alongside
  `interest_score`/`topic`/`one_line`), validating each returned tag against the vocab
  and dropping anything off-list.
- **Upsert to DB.** After scoring/summarizing/archiving, upsert one row per article by
  `message_id` and write `article_llm_tags`. Preserve existing `article_tag_delta`,
  `starred`, `read`, `feedback` for that `message_id`.
- **Calibration source.** `scorer`/`feedback` read calibration examples from the DB
  instead of `feedback.yaml` (same selection weighting: disagreement, recency, random).
- **Interest boost is designed, not yet wired.** The model treats `interests` as the
  subset of the vocabulary the scorer should boost. Today `scorer.py` only passes
  `interests` to the LLM as prose — it does not mechanically lift scores by tag. Wiring
  tag-driven boosting (e.g. nudging the score when an article's tags intersect
  `interests`) is part of this work, not pre-existing behavior.

## Retrieval surface (Archive Server)

Dynamic pages that `SELECT` from `articles.db` and link through to the offline archives.
The tablet's bookmarked landing page (`serve/index.html`) gains nav to **Daily Digests**
(existing) and **Library** (new).

- `/library` — search box + facet lists (authors, tags, with counts) + a *Starred* filter.
- `/library/author/<sender>` — that author's articles, newest first, each with its
  stored summary (fallback `one_line`). This is the "scan Max Read over time" page.
- `/library/tag/<tag>` — every article with that effective tag.
- `/library/search?q=` — FTS5 keyword results across bodies.
- Each result links to its archive (`archive_path`) — the existing offline copy.

## Write endpoints (Archive Server)

All write to `articles.db` under the existing lock:

- `POST /api/rate` — existing; now writes `feedback` + `read` to the DB.
- `POST /api/mark-read` — existing; now writes `read` to the DB.
- `POST /api/star` — `{message_id, starred}` → sets `starred`.
- `POST /api/tag` — `{message_id, tag, op}` where `op` ∈ `add|remove|clear` → updates
  `article_tag_delta`. `add` on a tag the LLM already gave is a no-op; `remove` on an
  LLM tag suppresses it; `clear` drops the reader's delta for that tag.

## UI additions

- Digest cards/rows and Library results gain a **★ star** toggle (one tap) next to the
  existing 👎 ✓ 👍 feedback bar. Star ≠ read ≠ feedback.
- **Tag chips** on each item, drawn from effective tags; tap a chip to remove, a "+"
  to add from the vocabulary. Fiddly on tablet but low-frequency — the LLM does the bulk.

## CLI additions

- `newsfeed retag [--all | --since YYYY-MM-DD | --tag TAG]` — recompute `article_llm_tags`
  for the selected rows against the current vocabulary; re-apply `article_tag_delta`.
  Run after editing the `tags:` vocabulary.
- `newsfeed migrate` (or run once implicitly if `articles.db` is absent) — the backfill
  above.

## Out of scope

- LLM synthesis / cross-article summaries / trend write-ups / scheduled reports.
- Semantic (embedding) search — FTS5 keyword only; embeddings can be added later as a
  column/table without changing the source-of-truth model.

## Suggested build order

1. Schema + `articles.db` bootstrap; DB read/write helpers.
2. Migration (steps 1–4 above, no tagging yet) → verify browse-by-author works off real
   backlog data.
3. Tag Vocabulary in `preferences.yaml`; tagging pass folded into the scorer; migration
   step 5 (backfill tags); `newsfeed retag`.
4. Pipeline upsert + move calibration read to the DB; stop writing the flat files.
5. Library pages + facets + FTS search.
6. Star endpoint + ★ UI; tag endpoint + chip UI.
