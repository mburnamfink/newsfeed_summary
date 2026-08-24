import pytest


@pytest.fixture(autouse=True)
def _newsfeed_home(tmp_path, monkeypatch):
    """Point NEWSFEED_HOME at a throwaway dir for every test.

    ``project_root()`` now requires the variable (ADR 0003), so any code path that
    resolves ``paths()`` — e.g. ``server.run`` — would otherwise raise under the
    test runner's bare environment.
    """
    monkeypatch.setenv("NEWSFEED_HOME", str(tmp_path))
