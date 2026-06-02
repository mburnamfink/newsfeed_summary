"""Build self-contained Archives of newsletters for offline tablet reading.

An Archive is a stored copy of a newsletter's HTML with every external image
downloaded locally and <script> tags stripped, so it renders fully without
network access and without executing newsletter-embedded tracking.
"""
import logging
import mimetypes
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .models import Email

logger = logging.getLogger(__name__)

_IMAGE_TIMEOUT = 10  # seconds per image; newsletters reference many, so fail fast
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def archive_email(email: Email, target_date: date, archive_root: Path) -> str:
    """Write a self-contained Archive for ``email`` and return its server-relative URL.

    Returns an empty string when there is no HTML to archive. Idempotent: an
    existing archive directory is reused rather than rebuilt.
    """
    if not email.raw_html.strip():
        return ""

    rel_dir = f"{target_date.isoformat()}/{email.message_id}"
    archive_dir = archive_root / rel_dir
    served_path = f"/archive/{rel_dir}/index.html"

    if (archive_dir / "index.html").exists():
        return served_path

    soup = BeautifulSoup(email.raw_html, "html.parser")

    for tag in soup(["script"]):
        tag.decompose()

    images_dir = archive_dir / "images"
    counter = 0
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            continue
        if not urlparse(src).scheme.startswith("http"):
            continue
        local_name = _download_image(src, images_dir, counter)
        if local_name:
            img["src"] = f"images/{local_name}"
            counter += 1
        else:
            # Drop the broken reference so the page doesn't beacon out on view.
            del img["src"]

    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "index.html").write_text(str(soup), encoding="utf-8")
    logger.info(f"Archived {email.subject!r} ({counter} images) -> {served_path}")
    return served_path


def _download_image(url: str, images_dir: Path, index: int) -> str | None:
    """Download one image into ``images_dir``; return its filename or None on failure."""
    try:
        resp = requests.get(url, timeout=_IMAGE_TIMEOUT, stream=True)
        resp.raise_for_status()
        content = resp.content
    except Exception as e:
        logger.debug(f"Image download failed ({url}): {e}")
        return None

    if len(content) > _MAX_IMAGE_BYTES:
        logger.debug(f"Image too large, skipping: {url}")
        return None

    ext = _guess_extension(url, resp.headers.get("Content-Type", ""))
    filename = f"{index}{ext}"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / filename).write_bytes(content)
    return filename


def _guess_extension(url: str, content_type: str) -> str:
    path_ext = Path(urlparse(url).path).suffix
    if path_ext and len(path_ext) <= 5:
        return path_ext
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    return ext or ".img"
