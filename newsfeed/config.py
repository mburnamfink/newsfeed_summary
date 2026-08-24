"""Resolution of per-user data locations.

The console scripts (``newsfeed``, ``newsfeed-server``) may be installed anywhere
(``uv tool install``), so paths must not be derived from ``__file__`` — that would
point inside the install location, not the user's data. The project root is taken
from ``$NEWSFEED_HOME`` and nothing else: a cwd fallback let a run launched from
the wrong directory create a fresh empty ``articles.db`` and then back it up over
the real one (ADR 0003), so the variable is now required.
"""
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_SERVER_PORT = 8080


def project_root() -> Path:
    env = os.environ.get("NEWSFEED_HOME")
    if not env:
        raise RuntimeError(
            "NEWSFEED_HOME is not set. Point it at your data directory, e.g.\n"
            "  export NEWSFEED_HOME=/home/michael/dev/newsfeed_summary\n"
            "This is required so a run from the wrong directory can't create an empty "
            "articles.db and back it up over your real data (ADR 0003)."
        )
    return Path(env).expanduser().resolve()


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def credentials(self) -> Path:
        return self.root / "credentials.json"

    @property
    def token(self) -> Path:
        return self.root / "token.json"

    @property
    def preferences(self) -> Path:
        return self.root / "preferences.yaml"

    @property
    def feedback(self) -> Path:
        return self.root / "feedback.yaml"

    @property
    def db(self) -> Path:
        """``articles.db`` — the Library's single source of truth (ADR 0002)."""
        return self.root / "articles.db"

    @property
    def anthropic_key(self) -> Path:
        return self.root / "anthropic_key.txt"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def serve(self) -> Path:
        return self.root / "serve"

    @property
    def digests(self) -> Path:
        return self.serve / "digests"

    @property
    def archive(self) -> Path:
        return self.serve / "archive"


def paths() -> Paths:
    return Paths(project_root())


def _preferences_block(key: str) -> dict:
    """The optional ``<key>:`` block from preferences.yaml, or empty."""
    pref = paths().preferences
    if not pref.exists():
        return {}
    try:
        data = yaml.safe_load(pref.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data.get(key, {}) or {}


def _server_config() -> dict:
    return _preferences_block("server")


def backup_config() -> dict:
    """The optional ``backup:`` block from preferences.yaml, or empty."""
    return _preferences_block("backup")


def server_port() -> int:
    return int(_server_config().get("port", DEFAULT_SERVER_PORT))


def server_base_url() -> str:
    """Base URL the digest pages are reachable at, for opening a fresh digest.

    Defaults to ``http://localhost:<port>``; override with ``server.url`` in
    preferences.yaml to point at, say, ``http://pop-os.local:8080``.
    """
    cfg = _server_config()
    url = cfg.get("url")
    if url:
        return str(url).rstrip("/")
    return f"http://localhost:{cfg.get('port', DEFAULT_SERVER_PORT)}"


def ensure_anthropic_key() -> None:
    """Populate ANTHROPIC_API_KEY from anthropic_key.txt if it isn't already set.

    The Anthropic SDK reads the key from the environment; this lets a fresh
    checkout work by dropping the key in a file instead of editing the shell rc.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    key_file = paths().anthropic_key
    if key_file.exists():
        os.environ["ANTHROPIC_API_KEY"] = key_file.read_text(encoding="utf-8").strip()
