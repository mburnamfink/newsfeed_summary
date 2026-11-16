import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from newsfeed import library, server


@pytest.fixture
def live_server(tmp_path):
    db_path = tmp_path / "articles.db"
    conn = library.connect(db_path)
    library.upsert_article(
        conn, message_id="m1", date="2026-05-10", sender_name="Max Read",
        subject="AI slop rising", summary="A paragraph about slop.", score=8.0,
        archive_path="/archive/2026-05-10/m1/index.html",
    )
    library.set_llm_tags(conn, "m1", ["artificial-intelligence"])
    library.set_body(conn, "m1", "a discussion of transformers and slop")
    library.upsert_article(
        conn, message_id="m2", date="2026-05-11", sender_name="Karl Schroeder",
        subject="Solarpunk futures", summary="Green tomorrow.", score=5.0,
    )
    library.set_llm_tags(conn, "m2", ["science-fiction"])
    conn.commit()
    conn.close()

    serve_root = tmp_path / "serve"
    serve_root.mkdir()

    # serve() binds and blocks, so capture the ThreadingHTTPServer instance as it is
    # constructed (with port 0 → an ephemeral port) to learn its address and shut it
    # down afterwards.
    httpd_box: dict = {}
    orig_init = ThreadingHTTPServer.__init__

    def capture_init(self, addr, hdlr, *a, **k):
        orig_init(self, addr, hdlr, *a, **k)
        httpd_box["httpd"] = self

    ThreadingHTTPServer.__init__ = capture_init  # type: ignore[method-assign]
    try:
        threading.Thread(
            target=server.serve,
            kwargs=dict(serve_root=serve_root, host="127.0.0.1", port=0, db_path=db_path),
            daemon=True,
        ).start()
        while "httpd" not in httpd_box:
            pass
    finally:
        ThreadingHTTPServer.__init__ = orig_init  # type: ignore[method-assign]

    httpd = httpd_box["httpd"]
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, db_path
    httpd.shutdown()


def _get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.read().decode()


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return r.status, r.read().decode()


def test_library_home_lists_facets(live_server):
    base, _ = live_server
    status, html = _get(base, "/library")
    assert status == 200
    assert "artificial-intelligence" in html
    assert "Max Read" in html


def test_author_page(live_server):
    base, _ = live_server
    _, html = _get(base, "/library/author/Max%20Read")
    assert "AI slop rising" in html
    assert "Solarpunk" not in html


def test_list_body_is_not_double_escaped(live_server):
    """Card HTML must reach the browser as tags, not visible &lt;div&gt; text."""
    base, _ = live_server
    _, html = _get(base, "/library/author/Max%20Read")
    assert '<div class="card' in html
    assert "&lt;div" not in html


def test_tag_page_uses_effective_tags(live_server):
    base, _ = live_server
    _, html = _get(base, "/library/tag/artificial-intelligence")
    assert "AI slop rising" in html


def test_search(live_server):
    base, _ = live_server
    _, html = _get(base, "/library/search?q=transformers")
    assert "AI slop rising" in html
    _, empty = _get(base, "/library/search?q=nonexistentword")
    assert "AI slop rising" not in empty


def test_star_endpoint_persists(live_server):
    base, db_path = live_server
    status, _ = _post(base, "/api/star", {"message_id": "m1", "starred": True})
    assert status == 200
    conn = library.connect(db_path)
    assert library.get_article(conn, "m1").starred is True
    _, html = _get(base, "/library?starred=1")
    assert "AI slop rising" in html
    conn.close()


def test_rate_endpoint_writes_feedback_and_read(live_server):
    base, db_path = live_server
    _post(base, "/api/rate", {"message_id": "m1", "sentiment": "up"})
    conn = library.connect(db_path)
    art = library.get_article(conn, "m1")
    assert art.feedback == "up"
    assert art.read is True
    conn.close()


def test_tag_endpoint_add_and_remove(live_server):
    base, db_path = live_server
    _post(base, "/api/tag", {"message_id": "m2", "tag": "futurism", "op": "add"})
    conn = library.connect(db_path)
    assert "futurism" in library.effective_tags(conn, "m2")
    _post(base, "/api/tag", {"message_id": "m2", "tag": "science-fiction", "op": "remove"})
    conn2 = library.connect(db_path)
    assert "science-fiction" not in library.effective_tags(conn2, "m2")
    conn.close()
    conn2.close()


def test_state_endpoint_reports_reader_state(live_server):
    base, _ = live_server
    _post(base, "/api/star", {"message_id": "m1", "starred": True})
    _post(base, "/api/rate", {"message_id": "m2", "sentiment": "down"})
    status, body = _get(base, "/api/state")
    assert status == 200
    state = json.loads(body)
    assert state["m1"]["starred"] is True
    assert state["m2"]["feedback"] == "down"
    assert state["m2"]["read"] is True


def test_bad_tag_op_is_rejected(live_server):
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/tag", {"message_id": "m2", "tag": "x", "op": "bogus"})
    assert exc.value.code == 400


# --- Reader app (ADR 0006) --------------------------------------------------


def test_reader_shell_served(live_server):
    base, _ = live_server
    status, html = _get(base, "/reader")
    assert status == 200
    assert 'href="/manifest.webmanifest"' in html
    assert "/api/queue" in html


def test_queue_splits_today_from_backlog(live_server):
    base, _ = live_server
    status, body = _get(base, "/api/queue")
    assert status == 200
    queue = json.loads(body)
    # m2 (2026-05-11) is the latest digest date; m1 (2026-05-10) is backlog.
    assert queue["digest_date"] == "2026-05-11"
    assert [a["message_id"] for a in queue["today"]] == ["m2"]
    assert [a["message_id"] for a in queue["backlog"]] == ["m1"]
    assert queue["backlog_count"] == 1
    m1 = queue["backlog"][0]
    assert m1["tier"] == "high"
    assert m1["summary"] == "A paragraph about slop."


def test_queue_today_is_score_ordered(live_server):
    base, db_path = live_server
    conn = library.connect(db_path)
    library.upsert_article(conn, message_id="m3", date="2026-05-11",
                           sender_name="X", subject="Better", score=9.0)
    conn.commit()
    conn.close()
    _, body = _get(base, "/api/queue")
    assert [a["message_id"] for a in json.loads(body)["today"]] == ["m3", "m2"]


def _queued_ids(base):
    q = json.loads(_get(base, "/api/queue")[1])
    return [a["message_id"] for a in q["today"] + q["backlog"]]


def test_queue_omits_read_items(live_server):
    base, _ = live_server
    _post(base, "/api/rate", {"message_id": "m2", "sentiment": "up"})  # marks m2 read
    assert _queued_ids(base) == ["m1"]


def test_dismiss_and_undo(live_server):
    base, db_path = live_server
    status, _ = _post(base, "/api/dismiss", {"message_id": "m1"})
    assert status == 200
    assert _queued_ids(base) == ["m2"]
    conn = library.connect(db_path)
    m1 = library.get_article(conn, "m1")
    conn.close()
    assert m1.dismissed and not m1.read and m1.feedback is None
    _post(base, "/api/dismiss", {"message_ids": ["m1"], "dismissed": False})
    assert _queued_ids(base) == ["m2", "m1"]


def test_dismiss_requires_ids(live_server):
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/dismiss", {})
    assert exc.value.code == 400


def test_dismiss_backlog_returns_ids(live_server):
    base, _ = live_server
    # m1 scores 8.0, so a 9.0 threshold catches it; m2 is today and is untouched.
    _, body = _post(base, "/api/dismiss-backlog", {"max_score": 9})
    assert json.loads(body)["message_ids"] == ["m1"]
    assert _queued_ids(base) == ["m2"]


def test_dismiss_backlog_default_threshold(live_server):
    base, _ = live_server
    _, body = _post(base, "/api/dismiss-backlog", {})
    assert json.loads(body)["message_ids"] == []  # m1 scores 8.0, above the default 5


def test_article_transform_injects_mobile_chrome(live_server, tmp_path):
    base, _ = live_server
    # Write the archive m1's row points at, then fetch it back through the transform.
    archive = tmp_path / "serve" / "archive" / "2026-05-10" / "m1"
    archive.mkdir(parents=True)
    (archive / "index.html").write_text(
        "<html><head></head><body><img src='images/0.png'>hi</body></html>",
        encoding="utf-8",
    )
    status, html = _get(base, "/reader/article/m1")
    assert status == 200
    assert 'name="viewport"' in html
    assert '<base href="/archive/2026-05-10/m1/"' in html
    assert "max-width" in html  # the responsive reset


def test_article_missing_archive_is_404(live_server):
    base, _ = live_server
    # m2 has no archive_path in the fixture.
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, "/reader/article/m2")
    assert exc.value.code == 404


# --- long-form summaries (ADR 0007) -----------------------------------------


class _FakeBackend:
    model = "fake-opus"

    async def acomplete(self, system_text, prompt, **opts):
        return "## TL;DR\n\nA **canned** long-form summary."


def test_summarize_endpoint_generates_and_persists(live_server, monkeypatch):
    from newsfeed import deep_summary
    monkeypatch.setattr(deep_summary, "_build_backend", lambda: _FakeBackend())
    base, db_path = live_server

    status, body = _post(base, "/api/summarize", {"message_id": "m1"})
    assert status == 200
    payload = json.loads(body)
    assert payload["model"] == "fake-opus"
    assert payload["source"] == "body"  # m1 has body text and no url → no re-fetch

    conn = library.connect(db_path)
    stored = library.get_summary(conn, "m1")
    conn.close()
    assert "canned" in stored["summary_md"]


def test_summary_page_renders_markdown(live_server, monkeypatch):
    from newsfeed import deep_summary
    monkeypatch.setattr(deep_summary, "_build_backend", lambda: _FakeBackend())
    base, _ = live_server
    _post(base, "/api/summarize", {"message_id": "m1"})

    status, html = _get(base, "/reader/summary/m1")
    assert status == 200
    assert "<strong>canned</strong>" in html  # markdown → HTML
    assert "AI slop rising" in html            # article subject in the head


def test_summary_page_absent_is_404(live_server):
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, "/reader/summary/m2")
    assert exc.value.code == 404


def test_summary_page_sanitizes_injected_html(live_server):
    """A prompt-injected summary must not reach the non-sandboxed tab as live HTML."""
    base, db_path = live_server
    conn = library.connect(db_path)
    library.set_summary(
        conn, "m1",
        "## H\n\nText <script>alert(1)</script> <img src=x onerror=alert(1)> "
        "[bad](javascript:alert(1)) [ok](https://example.com)",
        model="m", source="body", word_count=5,
    )
    conn.commit()
    conn.close()
    _, html = _get(base, "/reader/summary/m1")
    assert "<script" not in html
    assert "onerror" not in html
    assert "javascript:" not in html
    assert '<h2>H</h2>' in html
    assert 'href="https://example.com"' in html


def test_summarize_missing_article_is_404(live_server):
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base, "/api/summarize", {"message_id": "nope"})
    assert exc.value.code == 404


def test_queue_reports_has_summary(live_server, monkeypatch):
    from newsfeed import deep_summary
    monkeypatch.setattr(deep_summary, "_build_backend", lambda: _FakeBackend())
    base, _ = live_server
    _post(base, "/api/summarize", {"message_id": "m1"})
    _, body = _get(base, "/api/queue")
    q = json.loads(body)
    by_id = {a["message_id"]: a for a in q["today"] + q["backlog"]}
    assert by_id["m1"]["has_summary"] is True
    assert by_id["m2"]["has_summary"] is False


def test_pwa_assets_served(live_server):
    base, _ = live_server
    status, manifest = _get(base, "/manifest.webmanifest")
    assert status == 200
    assert json.loads(manifest)["display"] == "standalone"
    assert _get(base, "/sw.js")[0] == 200
    assert _get(base, "/reader/icon.svg")[0] == 200


def _stub_capture(monkeypatch, prepare):
    from newsfeed import capture
    monkeypatch.setattr(server, "load_preferences", lambda _p: None)
    monkeypatch.setattr(server, "load_llm_config", lambda _p: {})
    monkeypatch.setattr(server, "build_backend", lambda _c: None)
    monkeypatch.setattr(capture, "prepare", prepare)


def test_add_endpoint_saves_starred_article(live_server, monkeypatch):
    from datetime import date, datetime

    from newsfeed.models import Email, ScoredEmail

    async def fake_prepare(url, p, preferences, backend, rows):
        email = Email(
            message_id="url-abc123", subject="A saved essay", sender_name="Some Blog",
            sender_email="", date=datetime(2026, 11, 7, 9, 0), body="essay text here",
            source="url", url=url,
            archive_path="/archive/2026-11-07/url-abc123/index.html",
        )
        return ScoredEmail(email=email, interest_score=6.5, topic="tech", one_line="An essay.",
                           summary="An essay.", tags=["technology"]), date(2026, 11, 7)

    _stub_capture(monkeypatch, fake_prepare)
    base, db_path = live_server
    status, body = _post(base, "/api/add", {"url": "https://blog.example.com/essay"})
    assert status == 200
    payload = json.loads(body)
    assert payload["message_id"] == "url-abc123"
    assert payload["subject"] == "A saved essay"

    conn = library.connect(db_path)
    saved = [a.message_id for a in library.list_saved(conn)]
    starred = [a.message_id for a in library.list_starred(conn)]
    conn.close()
    assert "url-abc123" in saved and "url-abc123" in starred


def test_add_endpoint_rejects_non_http(live_server):
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(base, "/api/add", {"url": "javascript:alert(1)"})
    assert e.value.code == 400


def test_add_endpoint_unextractable_is_422(live_server, monkeypatch):
    async def fail(*_a, **_k):
        raise ValueError("no main content")

    _stub_capture(monkeypatch, fail)
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(base, "/api/add", {"url": "https://example.com/empty"})
    assert e.value.code == 422


def test_saved_shelf_and_home_show_save_box(live_server):
    base, _ = live_server
    for path in ("/library", "/library?source=url"):
        _, html = _get(base, path)
        assert 'onsubmit="saveUrl(this)' in html


def test_summaries_tab_groups_by_day_written(live_server):
    base, db_path = live_server
    conn = library.connect(db_path)
    library.set_summary(conn, "m1", "# Long\n\nbody", model="fake-opus", source="body",
                        word_count=1500)
    conn.commit()
    conn.close()
    _, html = _get(base, "/library/summaries")
    assert "AI slop rising" in html
    assert "Solarpunk futures" not in html  # no summary → not listed
    assert "1,500-word summary" in html
    assert 'class="day"' in html
    _, home = _get(base, "/library")
    assert 'href="/library/summaries"' in home


def test_summaries_tab_empty(live_server):
    base, _ = live_server
    _, html = _get(base, "/library/summaries")
    assert "No summaries yet" in html


def _get_with_cookie(base, path, cookie):
    req = urllib.request.Request(base + path, headers={"Cookie": cookie})
    with urllib.request.urlopen(req) as r:
        return r.status, r.read().decode(), r.headers


def test_readable_font_cookie_applies_to_reader_and_direct_archive(live_server, tmp_path):
    base, _ = live_server
    archive = tmp_path / "serve" / "archive" / "2026-05-10" / "m1"
    archive.mkdir(parents=True)
    (archive / "index.html").write_text(
        "<html><head></head><body><p style='font-size:12px'>tiny</p></body></html>",
        encoding="utf-8",
    )
    path = "/archive/2026-05-10/m1/index.html"

    _, plain, headers = _get_with_cookie(base, path, "")
    assert "font-size:12px" in plain  # served exactly as stored
    assert headers["Cache-Control"] == "no-cache"

    _, html, _ = _get_with_cookie(base, path, "other=x; nf_readable=1")
    assert "font-size:12px" not in html and "font-size:18px" in html

    _, html, _ = _get_with_cookie(base, "/reader/article/m1", "nf_readable=1")
    assert "font-size:18px" in html
    _, html, _ = _get_with_cookie(base, "/reader/article/m1", "nf_readable=0")
    assert "font-size:12px" in html


def test_digest_read_round_trip(live_server):
    base, _ = live_server
    assert json.loads(_get(base, "/api/digest-reads")[1]) == []
    _post(base, "/api/digest-read", {"date": "2026-05-10"})
    _post(base, "/api/digest-read", {"date": "2026-05-11", "read": True})
    assert json.loads(_get(base, "/api/digest-reads")[1]) == ["2026-05-11", "2026-05-10"]
    _post(base, "/api/digest-read", {"date": "2026-05-10", "read": False})
    assert json.loads(_get(base, "/api/digest-reads")[1]) == ["2026-05-11"]


def test_digest_read_rejects_bad_date(live_server):
    base, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(base, "/api/digest-read", {"date": "../etc"})
    assert e.value.code == 400
