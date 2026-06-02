"""Resolution of per-user data locations.

The console scripts (``newsfeed``, ``newsfeed-server``) may be installed anywhere
(``uv tool install``), so paths must not be derived from ``__file__`` — that would
point inside the install location, not the user's data. Instead the project root
is ``$NEWSFEED_HOME`` if set, else the current working directory. The systemd unit
sets ``WorkingDirectory``; an interactive run is expected from the project dir.
"""
import os
from dataclasses import dataclass
from pathlib import Path


def project_root() -> Path:
    env = os.environ.get("NEWSFEED_HOME")
    return Path(env).expanduser().resolve() if env else Path.cwd()


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
    def anthropic_key(self) -> Path:
        return self.root / "anthropic_key.txt"

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
