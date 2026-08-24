"""Capture an article from a URL as an ``Email``, for ``newsfeed add``.

The Library is Gmail-shaped: everything downstream (archive, score, tag,
summarize, upsert) consumes an :class:`~newsfeed.models.Email` keyed by a stable
``message_id``. Rather than fork that pipeline, URL capture produces an ``Email``
too — one whose ``source`` is ``"url"`` and whose id is derived from the URL — so
a saved article flows through the identical stages and lands in the same
``articles.db``.

Fetch strategy: a plain HTTP GET plus trafilatura main-content extraction handles
most blogs and news. When that yields too little (JS-rendered or soft-paywalled
pages), fall back to a headless Chromium render via Playwright and re-extract.
Playwright is a soft dependency — if it isn't installed the static result stands.
"""
import asyncio
import hashlib
import logging
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
import trafilatura
from bs4 import BeautifulSoup

from .models import Email
from .parser import strip_html

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 20  # seconds for the static GET
_RENDER_TIMEOUT = 30_000  # ms for the Playwright render
# Below this many characters of extracted text we assume the static fetch missed
# the real content (JS app / paywall interstitial) and try the browser fallback.
_MIN_CONTENT_CHARS = 500

# A browser-like UA — many sites 403 the default python-requests agent.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

# Query params that identify the click, not the content; dropped so the same
# article shared with different tracking still maps to one row.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "ref", "ref_src", "cmpid", "spm", "yclid", "_hsenc", "_hsmi",
}


def normalize_url(url: str) -> str:
    """Canonicalise a URL for stable identity: drop tracking params + fragment."""
    parts = urlparse(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    query = urlencode([
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_KEYS
        and not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ])
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, parts.params, query, ""))


def url_message_id(normalized_url: str) -> str:
    """Path-safe, collision-namespaced id for a saved URL.

    The ``url-`` prefix keeps it out of the way of Gmail's hex ids and free of
    the colon that would litter the archive directory name.
    """
    digest = hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"url-{digest}"


async def fetch_url(url: str) -> Email:
    """Capture ``url`` as an :class:`Email` (``source='url'``).

    Network and browser work run off the event loop in a worker thread.
    """
    return await asyncio.to_thread(_capture, url)


def _capture(url: str) -> Email:
    normalized = normalize_url(url)
    html = _static_fetch(normalized)
    content_html, meta = _extract(html, normalized)

    text = strip_html(content_html) if content_html else ""
    if len(text) < _MIN_CONTENT_CHARS:
        rendered = _render_fetch(normalized)
        if rendered:
            r_html, r_meta = _extract(rendered, normalized)
            r_text = strip_html(r_html) if r_html else ""
            if len(r_text) > len(text):
                content_html, meta, text = r_html, r_meta, r_text

    if not content_html:
        raise ValueError(
            f"Could not extract article content from {normalized}. The page may "
            f"require JavaScript or a login; installing Playwright "
            f"(pip install playwright && playwright install chromium) may help."
        )

    domain = urlparse(normalized).netloc.removeprefix("www.")
    sender = _first(meta, "author") or _first(meta, "sitename") or domain
    title = _first(meta, "title") or domain

    return Email(
        message_id=url_message_id(normalized),
        sender_name=sender,
        sender_email="",
        subject=title,
        date=datetime.now(),
        body=text,
        url=normalized,
        source="url",
        raw_html=_absolutize(content_html, normalized),
    )


def _static_fetch(url: str) -> str:
    # Surface network failures as a clean ValueError (like the extraction path);
    # `add` runs detached, so the log must carry an actionable line, not a traceback.
    try:
        resp = requests.get(url, timeout=_FETCH_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise ValueError(f"Could not reach {url} — check the URL and your network connection.") from e
    except requests.exceptions.Timeout as e:
        raise ValueError(f"Timed out fetching {url} after {_FETCH_TIMEOUT}s.") from e
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "an error"
        raise ValueError(f"{url} returned HTTP {code}.") from e
    except requests.RequestException as e:
        raise ValueError(f"Could not fetch {url}: {e}") from e
    return resp.text


def _render_fetch(url: str) -> str | None:
    """Render ``url`` in headless Chromium; None if Playwright is unavailable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Static extraction was thin and Playwright is not installed; "
                       "using the static result. Install playwright for JS pages.")
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(user_agent=_USER_AGENT)
                page.goto(url, wait_until="networkidle", timeout=_RENDER_TIMEOUT)
                return page.content()
            finally:
                browser.close()
    except Exception as e:  # a render failure must not sink the whole capture
        logger.warning(f"Playwright render failed for {url}: {e}")
        return None


def _extract(html: str, url: str):
    """Return (clean article HTML | None, metadata Document | None)."""
    if not html:
        return None, None
    content = trafilatura.extract(
        html, url=url, output_format="html", include_images=True,
        include_links=True, favor_recall=True,
    )
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
    except Exception:
        meta = None
    return content, meta


def _first(meta, attr: str) -> str:
    value = getattr(meta, attr, None) if meta else None
    return value.strip() if isinstance(value, str) else ""


def _absolutize(html: str, base_url: str) -> str:
    """Resolve relative ``img``/``a`` URLs against ``base_url``.

    The archiver only downloads ``http(s)`` image sources, so relative ``src``
    values (common on the open web, rare in email) must be absolutised first or
    their images are dropped from the offline copy.
    """
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if isinstance(src, str) and src and not urlparse(src).scheme:
            img["src"] = urljoin(base_url, src)
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if isinstance(href, str) and href and not urlparse(href).scheme and not href.startswith("#"):
            a["href"] = urljoin(base_url, href)
    return str(soup)
