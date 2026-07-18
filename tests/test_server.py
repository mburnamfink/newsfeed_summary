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
