import base64
from email.utils import parseaddr

from bs4 import BeautifulSoup


def parse_sender(raw: str) -> tuple[str, str]:
    name, addr = parseaddr(raw)
    return name or addr, addr


def extract_body(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return strip_html(html)

    parts = payload.get("parts", [])

    # Prefer plain text over HTML
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                return strip_html(html)

    # Recurse into nested multipart
    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            result = extract_body(part)
            if result:
                return result

    return ""


_VIEW_ONLINE_PHRASES = (
    "read online", "view online", "view in browser", "read in browser",
    "view this email", "read this email", "open in browser", "view as webpage",
    "view as a web page", "view the web version",
)


def extract_view_online_url(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        text = a.get_text(separator=" ").strip().lower()
        if any(phrase in text for phrase in _VIEW_ONLINE_PHRASES):
            return a["href"]
    return ""


def strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "img"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


_PAYWALL_PHRASES = (
    "this post is for paid subscribers",
    "this post is for paying subscribers",
    "this content is for paid subscribers",
    "for paid subscribers only",
    "subscribe to keep reading",
    "subscribe to read the full",
    "upgrade to paid",
    "become a paid subscriber",
    "become a paying subscriber",
    "unlock the full post",
    "this is a subscriber-only",
    "subscribers-only",
    "keep reading with a 7-day free trial",
    "keep reading with a free trial",
)


def detect_paywall(text: str) -> bool:
    """Heuristic: does this newsletter's body only tease paid-only content?

    Matches the boilerplate Substack et al. append when the email is a truncated
    preview of a subscriber-only post. Deliberately conservative to avoid flagging
    the mere presence of a "subscribe" call-to-action.
    """
    lowered = text.lower()
    return any(phrase in lowered for phrase in _PAYWALL_PHRASES)


def truncate_body(body: str, max_chars: int) -> str:
    if len(body) <= max_chars:
        return body
    return body[:max_chars] + "\n... [truncated]"
