# Newsfeed Summary

Daily, AI-scored digest of your Gmail newsletters — read on any device on your home network.

Each run fetches the previous day's newsletters from labelled Gmail folders, scores every
one 0–10 against your interests with Claude, summarises the ones worth reading, and renders
a clean HTML **digest**. Every newsletter is also saved as a self-contained **archive** (images
inlined, scripts stripped) so it reads well offline on a tablet. A small always-on server
publishes digests and archives over your LAN, and a feedback loop lets you correct scores from
the tablet to calibrate future runs.

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
   ▼  score 0–10 vs interests       newsfeed/scorer.py     (Claude Haiku)
ScoredEmail
   │  summarise high/medium         newsfeed/summarizer.py (Claude Haiku)
   ▼
Digest (HTML, grouped by tier)      newsfeed/renderer.py  ──► serve/digests/<date>.html
   │
   └─ append to feedback.yaml       newsfeed/feedback.py  (calibration history)

serve/  ──►  Archive Server (LAN, port 8080)   newsfeed/server.py
```

Scores fall into three **tiers**: High (≥7, "must read"), Medium (≥4, "worth a look"), Low
(skimmed). The thresholds and your interests live in `preferences.yaml`.

## Requirements

- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/)
- A Google Cloud OAuth client (`credentials.json`) with the Gmail API enabled
- An Anthropic API key

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

3. **Anthropic key.** Either export it:

   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

## Usage

```bash
newsfeed                    # digest for yesterday, opens in your browser
newsfeed --date 2026-05-13  # a specific day
newsfeed --no-open          # don't launch a browser (use in cron / on a headless box)
```

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

Browse from another device at `http://<host>.local:8080/` (mDNS; needs avahi-daemon). The
server exposes two POST endpoints used by the digest pages:

- `POST /api/feedback` `{subject, sender, score}` — record a corrected score in `feedback.yaml`
- `POST /api/mark-read` `{message_id}` — mark a newsletter read in `serve/read_state.json`

> The server binds `0.0.0.0:8080` with no authentication — intended for a trusted home LAN.

## Feedback & calibration

Each run appends its scored newsletters to `feedback.yaml`. Correcting a score from a digest
page (via the server) updates that entry. Future runs feed a weighted sample of this history
back to the scorer as calibration examples — biased toward recent items and ones where your
correction disagreed with the model. Tuning knobs live at the top of
[`newsfeed/feedback.py`](newsfeed/feedback.py).

## Privacy

`credentials.json`, `token.json`, `anthropic_key.txt`, your `preferences.yaml`, the
`feedback.yaml` history, and everything under `serve/` are personal and **gitignored**. Only
`preferences.example.yaml` is tracked as a template.
