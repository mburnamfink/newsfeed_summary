# ADR 0005 — URL Article Capture into the Library

## Status
Accepted

## Context
Every article in the Library arrives through one door: Gmail. The pipeline fetches
labelled newsletters, archives each self-contained (ADR 0001), scores/tags/summarizes
them, and upserts one row per article into `articles.db` (ADR 0002), keyed by the Gmail
`message_id`. But the reader also reads things *outside* their newsletters — a linked
blog post, an article a friend sent — and wants those to land in the same retrieval
library: a stable offline copy, browsable by author/tag, full-text searchable, and
durable independent of the original site.

The retained data model is already general enough to hold such an article; only the
*ingestion* is Gmail-specific. `models.Email` already carries an (unused) `url` field,
`archiver.archive_email()` builds an offline copy from any `raw_html`, and the
scorer/summarizer/tagger consume an `Email` without caring where it came from.

The open questions were: how to fetch and clean a web page into an archivable body; what
stable key to use in place of a `message_id`; and how far a manually-saved article should
be pulled into the existing curation model.

## Decision
Add a second ingestion source — **URL capture** — that produces an `Email` and flows it
through the *existing* archive → score → tag → summarize → upsert stages unchanged.

- **Reuse `Email`, don't fork the pipeline.** A captured article is represented as an
  `Email` with `source="url"` (new field; default `"gmail"`). This keeps one code path
  for scoring, tagging, summarizing, archiving and DB upsert. The naming tension — an
  `Email` that is not an email — is accepted deliberately as the price of that reuse; the
  domain term for the result is a **Saved Article** (`CONTEXT.md`), distinguished only by
  its `source`.
- **Identity from the URL.** The key is `url-<sha1(normalized_url)[:16]>`. Normalization
  strips tracking params (`utm_*`, `fbclid`, …) and the fragment, so the same article
  shared with different tracking maps to one row; re-adding a URL upserts in place,
  preserving the reader's star/read/feedback/tag corrections. The `url-` prefix is
  path-safe (no colon in the archive directory name) and cannot collide with Gmail's hex
  ids.
- **Fetch: static first, browser fallback.** A plain HTTP GET plus `trafilatura`
  main-content extraction handles most blogs and news, yielding clean article HTML plus
  title/author/site metadata. When extraction is too thin (JS-rendered or soft-paywalled
  pages), fall back to a headless-Chromium render via **Playwright** and re-extract.
  Playwright is a **soft dependency**: absent, the static result stands (with a warning).
  Relative `img`/`a` URLs are absolutised against the page URL before archiving, since the
  archiver only downloads `http(s)` images.
- **Full KB treatment + auto-star.** A Saved Article is scored, tagged from the vocabulary,
  and summarized like any newsletter — and **summarized at paragraph length regardless of
  tier**, because a deliberately-saved article deserves a real summary. It is
  **auto-starred**: a manual save is both a curation signal and a durability signal, and
  starred articles already back up to Google Drive (ADR 0003), so the offline copy survives
  independent of the origin site for free.
- **CLI only, for now.** Entry is `newsfeed add <url>`. A server-side form / bookmarklet
  for the tablet is a deliberate future extension, not part of this decision.

New columns `url` and `source` are added to `articles` (additive, defaulted); `connect()`
applies them idempotently to pre-existing databases via `ALTER TABLE`. New columns aside,
no schema or downstream change was needed.

## Implementation Notes
- `newsfeed/ingest.py` — `fetch_url(url) -> Email` (network/browser work in a worker
  thread): normalize + hash → static fetch → trafilatura extract → Playwright fallback if
  thin → absolutize → build the `Email`.
- `newsfeed add <url>` (`cli._run_add`) — fetch, `archive_email`, `score_emails([email])`
  (reusing DB calibration examples), `summarizer.summarize_articles([scored], "paragraph")`,
  `upsert_scored` + `set_body` + `set_star(…, True)`, then a best-effort `backup`.
- Retrieval: `library.list_saved()` + a `/library?source=url` shelf and a "🔗 web" source
  badge (linking to the origin URL) on cards. Saved Articles never appear in the daily
  Digest — `render_digest` only iterates a given run's scored list.

## Consequences
- The Library is no longer Gmail-only; `source` distinguishes origins and opens the door to
  further ingestion sources (RSS, share targets) on the same model.
- Playwright, if installed, is a ~300 MB browser dependency; kept optional so a minimal
  install still captures static pages.
- Capture quality depends on `trafilatura`/render success. Hard-paywalled or aggressively
  JS-gated pages may still yield a thin body; the command surfaces a clear error rather than
  storing an empty archive.
- Publication date is not modelled — a Saved Article's `date` is its ingest date, so it
  clusters under "recently added". Sorting by the article's own publication date is a future
  enhancement (the metadata is extracted but not yet stored).
