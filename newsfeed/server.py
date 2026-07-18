"""The Archive Server: serves Digests, Archives and the Library over the home network.

Static files (digests, archives, the landing page) are served straight from the
``serve/`` directory, which SimpleHTTPRequestHandler sandboxes so nothing in the
project root is reachable. Dynamic ``/library*`` pages and all writes go through
``articles.db`` (ADR 0002), the single source of truth. Run as a systemd user
service (see deploy/newsfeed-server.service).

GET (HTML):
  /library                    landing: search box + tag/author facets
  /library?starred=1          the starred shelf
  /library/author/<sender>    one author's articles, newest first
  /library/tag/<tag>          every article with that effective tag
  /library/search?q=<terms>   FTS keyword search over bodies

POST (JSON body, JSON response) — all write to articles.db under a lock:
  /api/rate       {message_id, sentiment}     feedback reaction + read flag
  /api/mark-read  {message_id, read=true}     read flag only
  /api/star       {message_id, starred}       curated-shelf toggle
  /api/tag        {message_id, tag, op}        reader tag overlay (add|remove|clear)
"""
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import library, library_pages
from .config import paths, server_port

logger = logging.getLogger(__name__)

HOST = "0.0.0.0"
VALID_SENTIMENTS = (None, "up", "down", "confirmed")


def serve(
    serve_root: Path | None = None,
    host: str = HOST,
    port: int | None = None,
    db_path: Path | None = None,
) -> None:
    p = paths()
    serve_root = serve_root or p.serve
    db_path = db_path or p.db
    port = port if port is not None else server_port()
    serve_root.mkdir(parents=True, exist_ok=True)
    _lock = threading.Lock()

    def _connect() -> sqlite3.Connection:
        # A fresh connection per request keeps SQLite off the thread that opened it
        # (ThreadingHTTPServer serves each request on its own thread).
        return library.connect(db_path)

    class _Handler(SimpleHTTPRequestHandler):
        # --- reads ----------------------------------------------------------
        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path == "/api/state":
                conn = _connect()
                try:
                    self._ok(library.state_map(conn))
                finally:
                    conn.close()
                return
            if parts.path == "/library" or parts.path.startswith("/library/"):
                self._handle_library(parts.path, parse_qs(parts.query))
                return
            super().do_GET()

        def _handle_library(self, path: str, query: dict[str, list[str]]) -> None:
            conn = _connect()
            try:
                if path == "/library":
                    if query.get("starred"):
                        html = library_pages.render_list(
                            "★ Starred", library.list_starred(conn),
                            "Articles you've starred to follow up on.",
                        )
                    else:
                        html = library_pages.render_home(
                            library.author_facets(conn), library.tag_facets(conn)
                        )
                elif path == "/library/search":
                    q = (query.get("q") or [""])[0]
                    results = library.search(conn, q) if q else []
                    html = library_pages.render_list(
                        f"Search: {q}" if q else "Search",
                        results, f"{len(results)} result(s)." if q else "",
                    )
                elif path.startswith("/library/author/"):
                    sender = unquote(path[len("/library/author/"):])
                    html = library_pages.render_list(
                        sender, library.list_by_author(conn, sender), "All articles, newest first."
                    )
                elif path.startswith("/library/tag/"):
                    tag = unquote(path[len("/library/tag/"):])
                    html = library_pages.render_list(
                        f"#{tag}", library.list_by_tag(conn, tag), "Every article with this tag."
                    )
                else:
                    self.send_error(404)
                    return
            finally:
                conn.close()
            self._html(html)

        # --- writes ---------------------------------------------------------
        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self.send_error(400, "Invalid JSON")
                return

            handlers = {
                "/api/rate": self._handle_rate,
                "/api/mark-read": self._handle_mark_read,
                "/api/star": self._handle_star,
                "/api/tag": self._handle_tag,
            }
            handler = handlers.get(self.path)
            if handler is None:
                self.send_error(404)
                return
            handler(body)

        def _handle_rate(self, body: dict) -> None:
            message_id = body.get("message_id")
            if not message_id:
                self.send_error(400, "Required: message_id")
                return
            sentiment = body.get("sentiment")
            if sentiment not in VALID_SENTIMENTS:
                self.send_error(400, "sentiment must be up, down, confirmed, or null")
                return
            # Any reaction marks the item read; clearing it (null) un-reads.
            self._write(lambda c: library.set_feedback(
                c, str(message_id), sentiment, read=sentiment is not None))
            self._ok({"ok": True})

        def _handle_mark_read(self, body: dict) -> None:
            message_id = body.get("message_id")
            if not message_id:
                self.send_error(400, "Required: message_id")
                return
            read = bool(body.get("read", True))
            self._write(lambda c: library.set_read(c, str(message_id), read))
            self._ok({"ok": True})

        def _handle_star(self, body: dict) -> None:
            message_id = body.get("message_id")
            if not message_id:
                self.send_error(400, "Required: message_id")
                return
            starred = bool(body.get("starred", True))
            self._write(lambda c: library.set_star(c, str(message_id), starred))
            self._ok({"ok": True})

        def _handle_tag(self, body: dict) -> None:
            message_id = body.get("message_id")
            tag = body.get("tag")
            op = body.get("op")
            if not message_id or not tag or op not in ("add", "remove", "clear"):
                self.send_error(400, "Required: message_id, tag, op in add|remove|clear")
                return
            self._write(lambda c: library.apply_tag_delta(c, str(message_id), str(tag), op))
            self._ok({"ok": True})

        def _write(self, fn: Callable[[sqlite3.Connection], None]) -> None:
            with _lock:
                conn = _connect()
                try:
                    fn(conn)
                    conn.commit()
                finally:
                    conn.close()

        # --- responses ------------------------------------------------------
        def _html(self, html: str) -> None:
            payload = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _ok(self, data: dict) -> None:
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    handler = partial(_Handler, directory=str(serve_root))
    httpd = ThreadingHTTPServer((host, port), handler)
    logger.info(f"Archive Server serving {serve_root} (+ Library from {db_path}) on http://{host}:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Archive Server")
        httpd.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    serve()
