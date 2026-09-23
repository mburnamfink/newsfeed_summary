# ADR 0006 — Mobile Reader App (queue-driven, installable)

## Status
Accepted

## Context
Reading on a phone is still clunky. Today the reader opens a digest or Library page
in a Chrome tab, taps a headline, and the archived newsletter opens in **another**
Chrome tab — competing with every other open tab — then the reader taps back to the
list, hunts for the next item, and repeats. Two problems compound:

1. **No focused flow.** The digest is a scrolling list, not a queue. There is no
   "next unread" motion — the reader manually re-finds their place after each article,
   and rating happens back on the list, away from the thing just read.
2. **Archives don't fit a phone.** An Archive (ADR 0001) is the newsletter's own HTML
   written verbatim (`str(soup)` in `archiver.py`) — no `<meta viewport>`, no mobile
   CSS. Newsletters are typically fixed ~600px `<table>` layouts, so on a ~390px screen
   they overflow: pinch-zoom and horizontal scrolling to read every article.

The reader wants an **app**: move through summarized, locally-hosted articles one at a
time; see the summary; choose **read** or **skip**; and once read, **rate** it. Each
article should keep as much of the host's own formatting as possible while being resized
to read comfortably on a phone.

The data to drive this already exists. `articles.db` (ADR 0002) holds every article with
its score/tier, summary, tags, `read`/`starred`/`feedback` state, `archive_path`, and
`source`/`url`. The Archive Server (ADR 0001) already serves the archives and exposes the
one-tap write endpoints (`/api/rate`, `/api/star`, `/api/mark-read`) — `/api/rate` already
records a reaction *and* marks the item read in one call. Off-LAN access over real HTTPS
already works via Tailscale Serve (ADR 0004). What's missing is a reading *surface* built
for the phone and a queue to feed it.

A native iOS/Android app was considered and rejected: it needs a build/signing/distribution
pipeline and an API client, all to reach a personal single-user server — entirely against
the grain of a stdlib-`http.server`, no-build-step, Jinja-templated project. An **installable
web app (PWA)** gets the one thing a native app buys here — its own home-screen icon and a
standalone window that does *not* live among the browser tabs — with none of that overhead.

## Decision
Add a **Reader**: a mobile-first, single-page web app served by the Archive Server that walks
the reader through their unread queue one card at a time, reads the article in place, and
rates it — installable to the home screen so it launches as its own app, not a browser tab.
No build step, no framework, no new runtime dependency (vanilla JS, one served HTML shell),
consistent with the rest of the project.

**1. A reading queue.** A new `GET /api/queue` returns the unread articles as JSON, newest
first with the must-reads within a day ahead of the rest (`date` desc, then `score` desc),
capped by a `limit` so the whole backlog isn't shipped at once. The Reader fetches it once,
then advances a client-side cursor through it. Only the fields a card needs are sent (subject,
sender, score/tier, summary, tags, `archive_path`, `source`, `url`, date, paywalled). The queue
is the existing read/unread axis surfaced as an ordered list — no new state model.

**2. Summary → read / skip → rate, in one place.** The Reader shows one **card** at a time:
subject, sender, tier badge, tags, and the summary (`display_summary` — the paragraph summary,
falling back to the one-liner). Two choices:
- **Skip** — advance the cursor without any write. Skip is **session-local**: the item stays
  unread and reappears in a future queue. (A durable "dismissed" state is deliberately out of
  scope; unread already means "still to deal with".)
- **Read** — open the article *in place* (below/over the card, not a new tab). After reading,
  the rating controls — the existing 👎 / ✓ / 👍 reactions plus ★ — are right there. Tapping a
  reaction calls `POST /api/rate`, which marks the item read, and the Reader advances to the
  next card. ★ maps to `POST /api/star` and is orthogonal (retrieval intent), left available
  but not required to advance.

All writes reuse the endpoints that already exist; the Reader adds none.

**3. Article opens inside the app, never a new tab.** Reading loads the article into a
sandboxed `<iframe>` inside the Reader shell (scripts are already stripped from archives, so
the iframe is inert). The app chrome — a back control and the rating bar — stays outside the
iframe. This is the direct fix for the tab-to-tab back-and-forth: navigation never leaves the
Reader.

**4. Resize for the phone, keep the host formatting.** The article is served through a new
transform endpoint, `GET /reader/article/<message_id>`, which reads the stored Archive's
`index.html` and **injects at serve time**, without mutating the stored file:
- a `<meta name="viewport" content="width=device-width, initial-scale=1">`, and
- a small responsive stylesheet that constrains width and reflows: `img, table, td { max-width:
  100% !important; height: auto; }`, fixed-width tables coerced to fluid, a readable base
  font-size/line-height, and `overflow-wrap` to stop long tokens from forcing horizontal scroll.

This keeps the newsletter's own colours, type and structure (the reader's stated preference —
"as much of the host formatting as possible") while making it fit the screen. Injecting at serve
time rather than at archive time fixes the **entire existing backlog** for free and leaves the
immutable-snapshot property of archives (ADR 0001) intact. A per-article **Reader-view toggle**
(`?mode=reader`) is the escape hatch for layouts the CSS can't rescue: it re-extracts clean
article text with `trafilatura` (already a dependency, ADR 0005) — trading formatting for
legibility only when the reader asks.

**5. Installable, so it isn't a tab.** Serve a web app manifest (`display: standalone`) and the
iOS `apple-mobile-web-app-capable` meta so "Add to Home Screen" launches the Reader in its own
window, separate from the browser's tabs. A minimal service worker registers the app and may
later cache the shell for offline open; installation as a PWA requires a secure context, so it
works from the Tailscale HTTPS URL (ADR 0004) — and on `localhost` — but not over plain-`http`
LAN. The Reader still *runs* over plain LAN; only home-screen install needs HTTPS.

## Implementation Notes
- **`newsfeed/library.py`** — add `list_unread(conn, limit=...) -> list[Article]`
  (`WHERE read = 0`, `ORDER BY score DESC, date DESC`). The `Article` dataclass and its
  `tier`/`display_summary` already carry everything a card needs.
- **`newsfeed/server.py`** — new routes on the existing handler:
  - `GET /reader` → the SPA shell (one static HTML/CSS/JS document; no build).
  - `GET /api/queue` → `json` list built from `library.list_unread`.
  - `GET /reader/article/<message_id>` → look up the row, read
    `serve/<archive_path>`, inject viewport + responsive CSS (and, on `?mode=reader`,
    substitute `trafilatura`-extracted content), return the HTML. 404 if the article has no
    archive (e.g. a Gmail-only item); fall back to the `url` link for `source=url` items.
  - `GET /manifest.webmanifest`, `GET /sw.js` → static PWA assets under `serve/`.
  - Reuse `POST /api/rate` / `/api/star` unchanged.
- **Reader HTML** lives alongside `library_pages.py` (e.g. a `reader_page.py` string template,
  same no-dependency Jinja/plain-string approach). Keep the app chrome outside the iframe so
  newsletter CSS can't leak into it.
- **Ordering & filters** — v1 is the whole unread queue. Optional query params (`?tag=`,
  `?source=url`, `?starred=1`) reusing the existing `list_by_*` selectors are a natural follow-on,
  not part of this decision.

## The phone/tablet end
There is **no mobile development** — no Android Studio, SDK, Kotlin/Java, Play Store, or
signing. That is the point of choosing a PWA. Everything is authored on the desktop in the
languages already in this repo (Python + HTML/CSS/JS) and served by the existing Archive
Server. The Android device only ever runs a browser pointed at that server, and Chrome on
Android has first-class PWA support (stronger than iOS Safari), so the install is the real
thing, not a bookmark.

One-time setup on an Android phone/tablet:
1. Install the **Tailscale** app and sign into the tailnet (the same access as ADR 0004 — it
   is what gives the device an HTTPS route to the desktop from anywhere).
2. Open Chrome to the Reader over the Tailscale HTTPS URL:
   `https://<host>.<tailnet>.ts.net/reader`.
3. Chrome offers **"Install app"** (or ⋮ menu → *Add to Home screen* → *Install*). Tap it.

After that, a home-screen icon launches the Reader in its **own window** — no address bar, no
Chrome tabs, its own entry in the Android app-switcher, an app name/icon and splash screen
drawn from the served `manifest.webmanifest`. That window not being a browser tab is the
concrete fix for "competes with other Chrome tabs".

The **HTTPS caveat**: Chrome only offers *Install* on a secure origin, so install via the
Tailscale HTTPS URL, not the plain-`http` LAN address (`pop-os.local:8080`). Over plain LAN the
Reader still runs as an ordinary web page — only the home-screen install is gated. `localhost`
counts as secure, which is enough for developing the Reader on the desktop.

## Consequences
- The phone gets a single-purpose reading app: one summary at a time, read-in-place, rate, next —
  no tab juggling and no losing your place. Rating happens on the article just read, not back on a
  list.
- The entire archive backlog becomes phone-legible at once, because the resize is a serve-time
  transform, not a re-archive. Stored archives stay byte-for-byte as captured (ADR 0001).
- The responsive CSS is a best-effort tamer of arbitrary newsletter HTML; deeply nested fixed
  tables may still look imperfect. The `?mode=reader` fallback covers the worst offenders at the
  cost of the host formatting.
- No new server security surface: the Reader only adds read endpoints and reuses the existing
  writes, which remain unauthenticated behind tailnet membership (ADR 0004) / trusted LAN
  (ADR 0001). Home-screen install needs the HTTPS (Tailscale) origin; plain-LAN use is unaffected.
- Skip is intentionally session-local, so a skipped item returns next time. If "don't show me this
  again without reading it" is wanted later, that's a new durable state and a separate decision.
- No native app, no app store, no build pipeline, no new dependency — the Reader is more served
  HTML from the same server.
