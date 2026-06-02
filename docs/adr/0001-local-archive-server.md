# ADR 0001 — Local Archive Server for Newsletter Delivery

## Status
Accepted

## Context
The digest is generated as a local HTML file and opened in a desktop browser. The user wants to read newsletters on a tablet over home WiFi. Most digest links fall back to `mail.google.com` because ~70% of newsletters do not include a detectable "view online" URL. Gmail links require authentication and present a poor reading experience on a tablet.

Two alternatives were considered:
1. **Improve view-online URL extraction** — scan newsletter HTML more aggressively for web versions. Rejected: a meaningful fraction of newsletters genuinely have no web version, so this cannot be a complete solution.
2. **Link directly to Gmail** — requires the user to be logged into Gmail in the tablet browser and still delivers a poor reading experience.

## Decision
At fetch time, save each newsletter as a self-contained Archive: raw HTML with all external images downloaded locally and `src` attributes rewritten to local paths. Strip `<script>` tags. Store under `archive/YYYY-MM-DD/{message_id}/`.

Run a local HTTP server (systemd user service, port 8080) that serves Digests and Archives from dedicated directories — never the project root, which contains credentials. The tablet bookmarks `http://pop-os.local:8080/` via mDNS (`hostname.local`), which is resilient to DHCP IP changes.

## Implementation Notes

The web root is a single `serve/` directory holding `serve/digests/` and `serve/archive/`; the server (`newsfeed/server.py`) is rooted there via `SimpleHTTPRequestHandler(directory=...)`, whose path normalisation rejects `..` escapes. A `serve/index.html`, regenerated on each run, lists digests by date and is the page the tablet bookmarks. Archiving runs as a distinct pipeline step in `main.py` (the `Email` carries `raw_html` from fetch and gains `archive_path`). Image download uses `requests` with a per-image timeout and size cap; broken images have their `src` dropped rather than left to beacon out. The unit lives at `deploy/newsfeed-server.service`.

## Consequences
- Newsletters are archived at fetch time; re-archiving old digests requires re-fetching from Gmail.
- The server must be running for the tablet to access content — guaranteed by the systemd user service whenever the desktop is on. `loginctl enable-linger` is required for it to survive logout/reboot.
- Rooting the server at `serve/` (which contains only `digests/` and `archive/`) keeps it physically separated from `credentials.json`, `token.json`, and `anthropic_key.txt` in the project root; path-traversal attempts cannot escape the web root.
- JavaScript is stripped from archived HTML, eliminating newsletter-embedded tracking and script execution.
- Archived newsletters are fully readable without internet access (images included).
- Digest output moved from `digests/` to `serve/digests/`; pre-existing files under the old `digests/` are not served and would need to be moved to remain accessible.
- Only `<img>` sources are localised. Images referenced from CSS (e.g. `background-image`) still point at remote URLs and will not load offline; in practice most newsletters use `<img>`.
- The server binds `0.0.0.0:8080` with no authentication, so the digest is readable by anyone on the home LAN. Acceptable for a trusted home network; revisit if the network is shared.
