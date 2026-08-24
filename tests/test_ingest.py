from unittest.mock import patch

from newsfeed import ingest


# --- normalization + identity ----------------------------------------------


def test_normalize_strips_tracking_and_fragment():
    url = "https://Example.com/Post/?utm_source=x&id=7&fbclid=abc#section"
    assert ingest.normalize_url(url) == "https://example.com/Post?id=7"


def test_normalize_drops_trailing_slash_but_keeps_root():
    assert ingest.normalize_url("https://example.com/a/b/") == "https://example.com/a/b"
    assert ingest.normalize_url("https://example.com/") == "https://example.com/"


def test_message_id_is_stable_and_prefixed():
    a = ingest.url_message_id(ingest.normalize_url("https://example.com/x?utm_medium=q"))
    b = ingest.url_message_id(ingest.normalize_url("https://example.com/x"))
    assert a == b  # tracking params don't change identity
    assert a.startswith("url-")
    assert ":" not in a  # path-safe for the archive directory name


# --- extraction → Email mapping --------------------------------------------


_ARTICLE = """
<html><head><title>The Real Title</title>
<meta name="author" content="Jane Doe"></head>
<body><article><h1>The Real Title</h1>
<p>%s</p>
<img src="/img/pic.png">
<a href="/next">next</a></article></body></html>
""" % ("A substantial paragraph of article content. " * 40)


def test_capture_maps_metadata_and_absolutizes(monkeypatch):
    with patch.object(ingest, "_static_fetch", return_value=_ARTICLE):
        email = ingest._capture("https://blog.example.com/post/?utm_source=news")

    assert email.source == "url"
    assert email.url == "https://blog.example.com/post"
    assert email.message_id.startswith("url-")
    assert email.subject == "The Real Title"
    assert email.sender_name == "Jane Doe"
    assert "substantial paragraph" in email.body
    # relative image/link resolved against the base URL for the archiver
    assert "https://blog.example.com/img/pic.png" in email.raw_html
    assert "https://blog.example.com/next" in email.raw_html


def test_thin_static_falls_back_to_render(monkeypatch):
    thin = "<html><body><div id='app'></div></body></html>"
    with patch.object(ingest, "_static_fetch", return_value=thin), \
         patch.object(ingest, "_render_fetch", return_value=_ARTICLE) as render:
        email = ingest._capture("https://spa.example.com/x")
    render.assert_called_once()
    assert "substantial paragraph" in email.body


def test_render_returns_none_without_playwright():
    # Simulate Playwright not installed: the import inside _render_fetch fails.
    with patch.dict("sys.modules", {"playwright.sync_api": None}):
        assert ingest._render_fetch("https://example.com") is None


def test_capture_raises_when_nothing_extractable():
    empty = "<html><body></body></html>"
    with patch.object(ingest, "_static_fetch", return_value=empty), \
         patch.object(ingest, "_render_fetch", return_value=None):
        try:
            ingest._capture("https://example.com/nope")
        except ValueError as e:
            assert "extract" in str(e).lower()
        else:
            raise AssertionError("expected ValueError")
