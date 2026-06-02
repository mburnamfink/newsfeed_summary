import argparse
import logging
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import yaml
from anthropic import Anthropic

from .archiver import archive_email
from .config import ensure_anthropic_key, paths
from .feedback import append_run, select_examples
from .gmail_client import authenticate, fetch_newsletter_emails
from .models import Preferences
from .renderer import render_digest, render_index
from .scorer import score_emails
from .summarizer import summarize_emails

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def _load_labels(gmail_config: dict) -> list[str]:
    raw = gmail_config.get("labels", gmail_config.get("label", "Newsletters"))
    if isinstance(raw, list):
        return raw
    return [raw]


def load_preferences(path: Path) -> Preferences:
    with path.open() as f:
        data = yaml.safe_load(f)
    return Preferences(
        gmail_labels=_load_labels(data.get("gmail", {})),
        interests=data.get("interests", []),
        thresholds=data.get("thresholds", {"high": 7, "medium": 4}),
        boost_sources=data.get("boost_sources", []),
        mute_sources=data.get("mute_sources", []),
        boost_keywords=data.get("boost_keywords", []),
        mute_keywords=data.get("mute_keywords", []),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a newsletter digest from Gmail.")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today() - timedelta(days=1),
        metavar="YYYY-MM-DD",
        help="Date to fetch newsletters for (default: yesterday)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the digest in a browser when done",
    )
    args = parser.parse_args()
    target_date: date = args.date

    logger.info(f"Generating digest for {target_date}")

    p = paths()
    ensure_anthropic_key()
    preferences = load_preferences(p.preferences)
    creds = authenticate(p.credentials, p.token)
    emails = fetch_newsletter_emails(creds, preferences.gmail_labels, target_date)

    if not emails:
        logger.warning(f"No newsletter emails found for {target_date}. Check your label name and date.")
        return

    client = Anthropic()

    examples = select_examples(p.feedback)
    if examples:
        logger.info(f"Loaded {len(examples)} feedback examples for scoring calibration")

    for email in emails:
        email.archive_path = archive_email(email, target_date, p.archive)

    scored = score_emails(emails, preferences, client, examples)
    scored = summarize_emails(scored, preferences, client)

    append_run(scored, target_date, p.feedback)

    output_path = render_digest(scored, target_date, p.digests)
    render_index(p.digests, p.serve)
    logger.info(f"Digest saved to {output_path}")

    if not args.no_open:
        webbrowser.open(output_path.as_uri())


if __name__ == "__main__":
    main()
