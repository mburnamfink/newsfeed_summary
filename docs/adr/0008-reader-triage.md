# ADR 0008 — Reader triage: ignore, rate-from-card, daily batches

## Status
Accepted

## Context
The Reader (ADR 0006) shows one card at a time, but it still feels like a pile:

- The counter reads "1 / 100 unread" across every day's backlog.
- **Skip** is session-local, so skipped items return every session and the pile
  never visibly shrinks. There is no way to say "I'm not going to read this".
- Rating (👎 / ✓ / 👍) and ★ are only reachable after opening the article, so a
  card whose summary was enough still has to be opened to be cleared.
- The ✓ reaction is labelled "Read", colliding with the **Read** verb.

The Reader's real job is **triage**: one ~2-second decision per card.

## Decision
**1. A durable "ignored" state.** `articles.dismissed INTEGER DEFAULT 0`, added via
`_ADDED_COLUMNS`. It is separate from `read` (the reader did not read it; the digest
and Library should not claim they did) and from `feedback`. `upsert_article` never
writes it, so digest re-runs don't resurrect ignored items. Ignoring **does not feed
the scorer**: the reader ignores good articles when busy, and treating that as 👎
would teach the scorer to down-rank real interests. The data is kept so it can be
mined later as a weak signal.

**2. Card verbs.** Primary, large: **Ignore** · **Later** · **Read →**. Secondary,
small: ★ 👎 ✓ 👍 📄.
- *Ignore* → `dismissed = 1`, advance.
- *Later* → no write; the card moves to the end of this session's queue (it still
  returns next session, as Skip did).
- *Rating from the card* → `POST /api/rate`, which marks it read, then advance —
  "the summary was enough".
- ★ toggles and does not advance. ✓ is relabelled **Right**.
- Touch: swipe left = Ignore, swipe right = Read. Only horizontal swipes are gestures,
  so the summary keeps native vertical scroll. Keyboard: `x` ignore, `s` later,
  `Enter` read, `1`/`2`/`3` = 👎/✓/👍, `*` star, `u` undo, `Esc` back.

**3. Undo everywhere.** Every advancing action shows an "Undo" toast (also `u`),
which reverses its write (`dismissed = 0`, or `/api/rate` with `sentiment: null`,
which un-reads) and returns to that card.

**4. Today and Backlog.** **Today** is every unread, non-ignored article dated on or
after the **latest digest date** (`MAX(date)` over `source = 'gmail'`, falling back to
any source). Older unread items are the **Backlog**. The Reader works through Today
first (score desc — must-reads first, so stopping early still covers the best), with a
"N left today" counter. When Today is empty it says so and offers the Backlog or a
one-tap **"Ignore low-scored backlog"** (score < 5), undoable.

## API
- `GET /api/queue` → `{digest_date, today: [...], backlog: [...], backlog_count}`.
  `today` is ordered score desc; `backlog` date desc then score desc, capped by `limit`.
- `POST /api/dismiss {message_id | message_ids, dismissed = true}`.
- `POST /api/dismiss-backlog {max_score = 5}` → `{ok, message_ids}` — ignores unread
  backlog items below `max_score` and returns their ids so the client can undo.
- `/api/state` gains `dismissed`; Library cards and the digest mute ignored items like
  read ones.

## Consequences
- The queue can actually reach zero; the daily batch gives a finish line.
- Ignored items stay in the Library (muted) and remain searchable; nothing is deleted.
- Swipe-up for Later was rejected: it fights the summary's vertical scroll.
