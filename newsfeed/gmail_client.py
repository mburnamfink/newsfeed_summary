import logging
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .models import Email
from .parser import detect_paywall, extract_body, extract_view_online_url, parse_sender

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def authenticate(credentials_path: Path, token_path: Path) -> Credentials:
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # A revoked or expired refresh token (e.g. test-app tokens lapse after
            # 7 days) raises here; fall back to a fresh browser authorization
            # rather than crashing the run.
            logger.warning(f"Stored Gmail token could not be refreshed ({e}); re-authorizing.")
            creds = _authorize(credentials_path)
    else:
        creds = _authorize(credentials_path)

    token_path.write_text(creds.to_json())
    return creds


def _authorize(credentials_path: Path) -> Credentials:
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found at {credentials_path}\n"
            "Download credentials.json from Google Cloud Console:\n"
            "  https://console.cloud.google.com/apis/credentials"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    return flow.run_local_server(port=0)


def fetch_newsletter_emails(
    creds: Credentials,
    label_names: list[str],
    target_date: date,
) -> list[Email]:
    service = build("gmail", "v1", credentials=creds)

    label_ids = [_get_label_id(service, name) for name in label_names]

    after_ts = int(datetime.combine(target_date, datetime.min.time()).timestamp())
    before_ts = int(datetime.combine(target_date + timedelta(days=1), datetime.min.time()).timestamp())
    query = f"after:{after_ts} before:{before_ts}"

    logger.info(f"Fetching {label_names} emails for {target_date}")

    # Gmail AND-s multiple labelIds, so we query each label separately and deduplicate
    seen_ids: set[str] = set()
    all_messages: list[dict] = []
    for label_id in label_ids:
        response = service.users().messages().list(
            userId="me", labelIds=[label_id], q=query, maxResults=200
        ).execute()
        for msg in response.get("messages", []):
            if msg["id"] not in seen_ids:
                seen_ids.add(msg["id"])
                all_messages.append(msg)

    logger.info(f"Found {len(all_messages)} emails across {len(label_ids)} labels")

    emails = []
    for msg in all_messages:
        try:
            full_msg = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
            email = _parse_message(full_msg)
            if email:
                emails.append(email)
        except Exception as e:
            logger.warning(f"Skipping message {msg['id']}: {e}")

    return emails


def _extract_raw_html(payload: dict) -> str:
    import base64
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType", "").startswith("multipart/"):
            result = _extract_raw_html(part)
            if result:
                return result
    return ""


def _get_label_id(service, label_name: str) -> str:
    labels = service.users().labels().list(userId="me").execute()
    for label in labels.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    available = [l["name"] for l in labels.get("labels", [])]
    raise ValueError(
        f"Gmail label '{label_name}' not found.\n"
        f"Available labels: {', '.join(available)}\n"
        "Update gmail.label in preferences.yaml."
    )


def _parse_message(msg: dict) -> Email | None:
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

    sender_raw = headers.get("From", "")
    sender_name, sender_email = parse_sender(sender_raw)
    subject = headers.get("Subject", "(no subject)")

    try:
        msg_date = parsedate_to_datetime(headers.get("Date", ""))
    except Exception:
        msg_date = datetime.now()

    body = extract_body(msg["payload"])
    if not body.strip():
        logger.debug(f"Skipping empty body: {subject}")
        return None

    raw_html = _extract_raw_html(msg["payload"])
    url = extract_view_online_url(raw_html) if raw_html else ""

    return Email(
        message_id=msg["id"],
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        date=msg_date,
        body=body,
        url=url,
        raw_html=raw_html,
        paywalled=detect_paywall(body),
    )
