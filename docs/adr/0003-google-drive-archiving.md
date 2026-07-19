# ADR 0003 — Layered Backup to Google Drive via rclone

## Status
Accepted

## Context
The service retains a lot, but almost none of it is backed up off the machine it
runs on. Auditing what exists and where:

1. **Codebase** — tracked in git, pushed to a public GitHub remote. Fully
   protected, and intended to be public.
2. **`feedback.yaml`, `preferences.yaml`, `articles.db`** — small (~190 KB
   together) and irreplaceable. `articles.db` is nominally rebuildable from
   digests + archives (ADR 0002), but its stars, read-state, and feedback
   corrections are original and cannot be regenerated. These are gitignored
   (personal, and `articles.db` isn't diffable) so git does not protect them.
3. **`serve/archive/`** — 609 MB of self-contained per-article snapshots
   (ADR 0001), ~8k files. Local-only.
4. **Secrets** — `credentials.json` (Google OAuth client) and `token.json`.

The only off-machine copy today is a stale one-time `rclone copy .` from an
earlier date sitting in `gdrive:newsfeed_summary/`. It redundantly duplicates the
public code, is missing later modules, carries old `feedback.yaml`/`serve/`, and —
worst — contains an **unencrypted `credentials.json`**. It is not a backup so much
as an accident.

The reader controls a Google Drive, reachable through an already-configured
`rclone` remote named `gdrive:`. (There is a `~/gdrive` fuse mount, but it is not
reliably mounted, so backup must talk to the `gdrive:` remote directly rather than
depend on the mount being present.)

The four categories above do not want the same treatment, and the mismatch is the
whole point of this decision:

- Category 1 is already handled by git; copying it to Drive only creates a second,
  stale source of truth.
- Category 2 is tiny and irreplaceable — the highest-value, lowest-cost thing to
  protect.
- Category 3 is bulky, and its source of truth is external and durable: the
  articles live in Gmail and on the originating blogs. Backing up 609 MB to
  duplicate what Gmail already holds is not worth it. But the reader does want the
  subset they have **starred** to survive on storage they control, independent of
  Gmail retention.
- Category 4 is re-obtainable from Google on demand; storing long-lived secrets in
  cloud storage is a liability, especially unencrypted.

## Decision
Back up on a per-category basis to `gdrive:newsfeed_summary/`, using `rclone`
against the `gdrive:` remote as the transport:

- **Codebase** → git only. Not copied to Drive. The stale full dump is removed.
- **Config + Library state** (`feedback.yaml`, `preferences.yaml`, `articles.db`)
  → `rclone sync` to `…/state/`. A mirror, not accretion, so deletions propagate.
- **Starred articles** → for each `starred=1` row in `articles.db`, copy its
  self-contained archive directory to `…/starred/<DATE>/<message_id>/`, plus a
  human-browsable `manifest.csv`. Additive and idempotent.
- **All articles / the 609 MB `serve/archive/`** → not backed up. Gmail and the
  originating blogs are the source of truth.
- **Secrets** → deliberately excluded from Drive. Disaster recovery re-runs the
  Google OAuth flow. The leaked `credentials.json` on Drive is deleted (and the
  OAuth client should be rotated in Google Cloud Console).

The backup runs at the end of the daily digest pipeline, so state is snapshotted
immediately after it changes. It is best-effort: a backup failure logs a warning
but never fails the digest run.

## Implementation Notes
A new `newsfeed/backup.py` module:

- `backup_state(...)` → `rclone sync` the three small files to `…/state/`.
- `backup_starred(...)` → `SELECT date, message_id, archive_path, sender_name,
  subject, one_line FROM articles WHERE starred=1`. The DB's `archive_path` is a
  server-relative path like `/archive/<DATE>/<message_id>/index.html`; the on-disk
  directory is its parent under `serve/` (`paths.serve / archive_path.lstrip('/')`,
  then `.parent`). `rclone copy` that directory (which includes `index.html` and
  `images/`) to `…/starred/<DATE>/<message_id>/`, and write `manifest.csv`
  alongside. Idempotent — rclone skips unchanged files.
- Both functions guard on `rclone` being absent or the remote being unreachable:
  log a warning and no-op rather than raising.

Wiring:

- A `newsfeed backup` subcommand (dispatched like `migrate`/`retag` in `cli.py`)
  for manual runs.
- A call at the end of `_generate_digest` (after `render_index`), wrapped in a
  `try/except` that logs but does not propagate, so a Drive hiccup never costs the
  digest.

Configuration: an optional `backup:` block in `preferences.yaml`
(`remote: gdrive:`, `path: newsfeed_summary`, `enabled: true`), with defaults baked
in so it works with no config. Mirrored into `preferences.example.yaml`.

Resulting Drive layout:

```
gdrive:newsfeed_summary/
  state/     feedback.yaml, preferences.yaml, articles.db        (rclone sync)
  starred/   <DATE>/<message_id>/{index.html,images/}, manifest.csv  (rclone copy)
```

The one-time cleanup of the old dump is destructive on the remote and is done
interactively — list the existing contents, confirm the delete set (redundant
code, stale `serve/`, and the secret), then remove — never as an unattended step.

## Consequences
- The irreplaceable ~190 KB (Category 2) gains an off-machine copy that refreshes
  every daily run. This closes the ADR 0002 note that `articles.db` was "a new
  file to back up."
- Starred articles survive independently of Gmail retention, on storage the reader
  controls. Starring becomes a curation signal *and* a durability signal.
- Starred backup depends on `serve/archive/` being present when it runs. It runs at
  the end of the pipeline, where the archive is always fresh; but a manual
  `newsfeed backup` after pruning `serve/` would find nothing to copy.
- Secrets are not recoverable from backup by design — restoring on a new machine
  requires re-authenticating with Google. Accepted as the safer trade.
- Drive holds no copy of the code; a full-machine-loss recovery is `git clone` +
  restore `state/` + re-auth, not a single-folder restore.
- `rclone` becomes an operational dependency of the daily run (soft — its absence
  degrades to a logged warning, not a failure).
