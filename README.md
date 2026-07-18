# Newsfeed Summary

Daily, AI-scored digest of your Gmail newsletters — read on any device on your home network.

Each run fetches the previous day's newsletters from labelled Gmail folders, scores every
one 0–10 against your interests with Claude, summarises the ones worth reading, and renders
a clean HTML **digest**. Every newsletter is also saved as a self-contained **archive** (images
inlined, scripts stripped) so it reads well offline on a tablet. A small always-on server
publishes digests and archives over your LAN, and a one-tap feedback loop (👎 / ✓ Read &
right / 👍) lets you react to items from the tablet to calibrate future runs.

See [`CONTEXT.md`](CONTEXT.md) for the domain glossary and
[`docs/adr/0001-local-archive-server.md`](docs/adr/0001-local-archive-server.md) for the
archive-server design rationale.

## How it works

```
Gmail (labelled newsletters)
   │  fetch + parse        newsfeed/gmail_client.py, parser.py
   ▼
Email ──► archive (offline copy)   newsfeed/archiver.py ──► serve/archive/<date>/<id>/
   │
   ▼  score + tag vs vocabulary     newsfeed/scorer.py     (Claude, via newsfeed/llm.py)
ScoredEmail
   │  summarise high/medium         newsfeed/summarizer.py (Claude, via newsfeed/llm.py)
   ▼
Digest (HTML, grouped by tier)      newsfeed/renderer.py  ──► serve/digests/<date>.html
   │
   └─ upsert row + body index       newsfeed/library.py   ──► articles.db (source of truth)

articles.db + serve/  ──►  Archive Server + Library (LAN, port 8080)   newsfeed/server.py
```

Scores fall into three **tiers**: High (≥7, "must read"), Medium (≥4, "worth a look"), Low
(skimmed). The thresholds and your interests live in `preferences.yaml`.

## Requirements

- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/)
- A Google Cloud OAuth client (`credentials.json`) with the Gmail API enabled
- Claude access, one of:
  - a **Claude Pro/Max subscription** (default) — runs through the Claude Agent SDK,
    which bundles its own Claude Code CLI; you only need to be authenticated (a
    logged-in Claude Code session, or a `CLAUDE_CODE_OAUTH_TOKEN`), or
  - an **Anthropic API key** (set `llm.backend: api` in `preferences.yaml`)

## Install

```bash
# from a clone of this repo
uv tool install .
```

This puts two commands on your `PATH`:

- `newsfeed` — generate a digest
- `newsfeed-server` — run the LAN archive server

> **Where data lives.** The commands read configuration and write output relative to the
> **current working directory** (or `$NEWSFEED_HOME` if set) — not the install location. Run
> them from your project directory, or export `NEWSFEED_HOME=/path/to/newsfeed_summary`.

Alternatively, for development, install editable into a venv:

```bash
uv pip install -e .
# or, without installing, run from the repo:
uv run newsfeed --help
```

## Configure

1. **Preferences.** Copy the template and edit it (your copy is gitignored):

   ```bash
   cp preferences.example.yaml preferences.yaml
   ```

   Set the Gmail `labels` to fetch, your `interests`, score `thresholds`, and any
   source/keyword boosts or mutes.

2. **Gmail credentials.** Create an OAuth *Desktop app* client in the
   [Google Cloud Console](https://console.cloud.google.com/apis/credentials), enable the Gmail
   API, and download it as `credentials.json` into the project directory. The first run opens a
   browser to authorise read-only Gmail access and caches the result in `token.json`.

3. **Claude access.** Pick a backend in `preferences.yaml` under `llm:` (default
   `subscription`).

   - **Subscription (default).** The Claude Agent SDK ships its own bundled Claude
     Code CLI, so no global `npm` install is needed to *run* the pipeline — you just
     need to be authenticated. If you already use Claude Code interactively on this
     machine, its logged-in session works. For a headless/cron box, mint a
     long-lived token (this step needs the standalone `claude` CLI —
     `npm install -g @anthropic-ai/claude-code` if you don't have it):

     ```bash
     claude setup-token                 # opens a browser; prints a token
     export CLAUDE_CODE_OAUTH_TOKEN=...  # put this in your shell rc or the cron env
     ```

     Make sure `ANTHROPIC_API_KEY` is **not** exported — if it is, Claude Code uses
     per-token API billing instead of your subscription. (The pipeline also drops it
     from its own environment for subscription runs.)

   - **API.** Set `llm.backend: api` in `preferences.yaml` and provide a key, either
     by exporting `ANTHROPIC_API_KEY=sk-ant-...` or dropping it in `anthropic_key.txt`.

## Usage

```bash
newsfeed                    # digest for yesterday; runs in the background, opens when ready
newsfeed --date 2026-05-13  # a specific day
newsfeed --foreground       # stay attached to the terminal (see progress live)
newsfeed --no-open          # don't launch a browser; implies --foreground (cron / headless)

newsfeed migrate            # one-time: backfill articles.db from existing digests/archives
newsfeed retag --all        # (re)tag stored articles against your tags: vocabulary
newsfeed retag --since 2026-06-01
newsfeed retag --tag technology
```

By default `newsfeed` **detaches to the background** so your terminal returns immediately, logs
to `logs/newsfeed-<date>.log`, and opens the finished digest in a browser. It opens the digest
**through the Archive Server** (`http://localhost:8080/…` by default), not as a local file, so
the feedback buttons work — the server must be running (see below). Point it elsewhere with the
`server:` block in `preferences.yaml`.

Output: `serve/digests/<date>.html`, archives under `serve/archive/`, and an index at
`serve/index.html`.

### Daily run (cron)

```cron
# 6 AM daily; NEWSFEED_HOME lets the job run from anywhere
0 6 * * * NEWSFEED_HOME=%h/dev/newsfeed_summary %h/.local/bin/newsfeed --no-open
```

## Archive Server

Serves digests and archives to other devices (e.g. a tablet) over your home WiFi. It is rooted
at `serve/` only, so credentials in the project directory are never reachable.

Run it directly:

```bash
newsfeed-server   # http://0.0.0.0:8080/
```

Or install it as a systemd **user** service (no root) — see
[`deploy/newsfeed-server.service`](deploy/newsfeed-server.service):

```bash
mkdir -p ~/.config/systemd/user
ln -s "$PWD/deploy/newsfeed-server.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now newsfeed-server
loginctl enable-linger "$USER"   # keep running across logout / reboot
```

Browse from another device at `http://<host>.local:8080/` (mDNS; needs avahi-daemon). Beyond the
static digests and archives it serves the **Library** and a set of one-tap write endpoints, all
backed by `articles.db`:

- `GET  /library` — search box + tag/author facets (`?starred=1` for the starred shelf)
- `GET  /library/author/<sender>`, `/library/tag/<tag>`, `/library/search?q=` — browse & search
- `POST /api/rate` `{message_id, sentiment}` — record a reaction (`up`/`down`/`confirmed`/`null`)
  and mark the item read in one tap
- `POST /api/mark-read` `{message_id, read}` — mark a newsletter read
- `POST /api/star` `{message_id, starred}` — toggle the curated ★ shelf
- `POST /api/tag` `{message_id, tag, op}` — add / remove / clear a reader tag correction

> The server binds `0.0.0.0:8080` with no authentication — intended for a trusted home LAN.

## The Library

Every fetched article is retained, scored, tagged, and full-text indexed in `articles.db`
(SQLite + FTS5) — the single source of truth for scores, tags, stars, feedback and read state
(see [ADR 0002](docs/adr/0002-sqlite-knowledge-library.md) and
[`docs/knowledge-library.md`](docs/knowledge-library.md)). Browse it by **author** or **tag**,
**search** article bodies, and **star** the subset you want to follow up on — all from the
tablet. Tags come from the fixed `tags:` vocabulary in `preferences.yaml`; the LLM assigns them
and you correct them with the chips on any card. Edit the vocabulary, then `newsfeed retag` to
re-apply it to the backlog.

First-time setup on an existing install: `newsfeed migrate` reconstructs the database from your
digests, archives, `feedback.yaml` and read state, then `newsfeed retag --all` tags the backlog.

## Feedback & calibration

Reacting to an item from a digest or Library page (👎 too low a bar / 👍 wanted it ranked higher /
✓ score was right) records the reaction on that article in `articles.db`. Future runs feed a
weighted sample of this history back to the scorer as calibration examples — biased toward recent
items and ones where your reaction pushed against the model. Tuning knobs live at the top of
[`newsfeed/feedback.py`](newsfeed/feedback.py).

## Privacy

`credentials.json`, `token.json`, `anthropic_key.txt`, your `preferences.yaml`, the legacy
`feedback.yaml`, the `articles.db` Library, and everything under `serve/` are personal and
**gitignored**. Only `preferences.example.yaml` is tracked as a template.
