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
The historical record of Scored Emails and optional user score corrections (in `feedback.yaml`). Used to calibrate future scoring by providing the model with examples of past disagreements.

## Archive Server
A local HTTP server that serves Digests and Archives over the home network. Runs as a systemd user service so it is available whenever the desktop is on. Accessible to the user's tablet at `pop-os.local:8080`.
