# Newsfeed Summary — Domain Glossary

## Email
A raw newsletter fetched from Gmail. Carries subject, sender, body text, raw HTML, and a message ID.

## Scored Email
An Email annotated with an interest score (0–10), a topic label, a one-line description, and an optional summary. Produced by the scoring and summarisation pipeline.

## Tier
The interest-level category of a Scored Email, derived from its score:
- **High** — score ≥ 7 (Must Read)
- **Medium** — score ≥ 4 (Worth a Look)
- **Low** — score < 4 (Skimmed)

## Digest
The daily HTML page rendered by the system. Lists all Scored Emails grouped by Tier. Served from the local Archive Server so it can be read on a tablet.

## Archive
A self-contained, stored copy of a newsletter's raw HTML with all external images downloaded locally and HTML rewritten to reference them. Created at fetch time. Linked to from the Digest. Stored under `archive/YYYY-MM-DD/{message_id}/`.

## Preferences
User-defined configuration (in `preferences.yaml`) controlling which Gmail labels to fetch, interest topics, and source/keyword boost and mute rules.

## Feedback
A reader's reaction to a Scored Email — thumbs-up (undervalued), thumbs-down (overvalued), confirmed (score was right), or an explicit corrected score. Captured by tapping a Digest. Used to calibrate future scoring by anchoring the scale with past agreements and disagreements. Distinct from [[Starred]] (retrieval intent) and from read-state (seen vs. unseen). The legacy free-text corrections in `feedback.yaml` were a first-draft, manual-era artifact that the reaction taps have superseded.

## Library
The full, retained corpus of every Scored Email ever fetched — searchable and browsable by author, tag, and text. Not a curated subset: retention is comprehensive (every article stays archived and indexed). See [[Starred]] for the curated overlay.

## Starred
A user-applied flag marking a Scored Email as part of the curated shelf the reader is actively following up on. A lightweight overlay on the [[Library]]: everything is retained, starring only distinguishes "I want to revisit this" from the rest. Distinct from [[Feedback]] (which tunes scoring) and from read-state (which tracks what's been seen).

## Tag
A canonical label attached to a Scored Email for retrieval, drawn only from the [[Tag Vocabulary]]. An article carries several. Distinct from the legacy free-text `topic` string, which each article phrases idiosyncratically and which Tags supersede for retrieval.

An article's **effective tags** are computed, not stored flat: `(LLM tags ∪ reader-added) − reader-removed`. The LLM proposes tags constrained to the vocabulary and re-derives them freely on every retag; the reader's add/remove corrections are a durable overlay re-applied on top, so hand-fixes survive retagging while newly-added vocabulary terms still flow into old articles.

## Tag Vocabulary
The fixed, user-owned list of legal Tags, extended and edited deliberately over time. The LLM must choose from it; it cannot invent new labels. Editing the vocabulary (add / rename / remove) is expected to be easy.

The reader's **interests** are a subset of this vocabulary — the same terms, marking which Tags the scorer should boost. Every article is tagged from the full vocabulary; only interest Tags lift its interest score. So the two lists share one canonical spelling and never drift.

## Archive Server
A local HTTP server that serves Digests and Archives over the home network. Runs as a systemd user service so it is available whenever the desktop is on. Accessible to the user's tablet at `pop-os.local:8080`.
