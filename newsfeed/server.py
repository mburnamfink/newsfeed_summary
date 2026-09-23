"""The Archive Server: serves Digests, Archives and the Library over the home network.

Static files (digests, archives, the landing page) are served straight from the
``serve/`` directory, which SimpleHTTPRequestHandler sandboxes so nothing in the
project root is reachable. Dynamic ``/library*`` pages and all writes go through
``articles.db`` (ADR 0002), the single source of truth. Run as a systemd user
service (see deploy/newsfeed-server.service).

GET (HTML):
  /library                    landing: search box + tag/author facets
  /library?starred=1          the starred shelf
  /library?source=url         articles saved from the web (newsfeed add)
  /library/author/<sender>    one author's articles, newest first
  /library/tag/<tag>          every article with that effective tag
  /library/search?q=<terms>   FTS keyword search over bodies

  /reader/summary/<id>        long-form summary (ADR 0007) rendered from Markdown

POST (JSON body, JSON response) — all write to articles.db under a lock:
  /api/rate       {message_id, sentiment}     feedback reaction + read flag
  /api/mark-read  {message_id, read=true}     read flag only
  /api/star       {message_id, starred}       curated-shelf toggle
  /api/tag        {message_id, tag, op}        reader tag overlay (add|remove|clear)
  /api/summarize  {message_id}                generate + store a long-form summary
"""
import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import deep_summary, library, library_pages, reader_page
from .config import paths, server_port
from .library import Article

logger = logging.getLogger(__name__)

HOST = "0.0.0.0"
VALID_SENTIMENTS = (None, "up", "down", "confirmed")
QUEUE_LIMIT_MAX = 500
# "Ignore low-scored backlog" threshold: below Medium tier (ADR 0008).
BACKLOG_DISMISS_MAX_SCORE = 5.0


def _article_json(a: Article) -> dict:
    """The fields the Reader card needs (ADR 0006), as a JSON-ready dict."""
    return {
        "message_id": a.message_id,
        "subject": a.subject,
        "sender_name": a.sender_name,
        "date": a.date,
        "score": a.score,
        "tier": a.tier,
        "summary": a.display_summary,
        "tags": a.tags,
        "archive_path": a.archive_path,
        "source": a.source,
        "url": a.url,
        "paywalled": a.paywalled,
        "starred": a.starred,
        "has_summary": a.has_summary,
        "reading_minutes": a.reading_minutes,
    }


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
            # Reader app (ADR 0006) — SPA shell, queue, per-article transform, PWA assets.
            if parts.path == "/reader":
                self._send(reader_page.READER_HTML, "text/html; charset=utf-8")
                return
            if parts.path == "/api/queue":
                self._handle_queue(parse_qs(parts.query))
                return
            if parts.path.startswith("/reader/article/"):
                message_id = unquote(parts.path[len("/reader/article/"):])
                self._handle_article(message_id, parse_qs(parts.query))
                return
            if parts.path.startswith("/reader/summary/"):
                message_id = unquote(parts.path[len("/reader/summary/"):])
                self._handle_summary_page(message_id)
                return
            if parts.path == "/manifest.webmanifest":
                self._send(reader_page.MANIFEST, "application/manifest+json")
                return
            if parts.path == "/sw.js":
                self._send(reader_page.SERVICE_WORKER, "text/javascript")
                return
            if parts.path == "/reader/icon.svg":
                self._send(reader_page.ICON_SVG, "image/svg+xml")
                return
            if parts.path == "/library" or parts.path.startswith("/library/"):
                self._handle_library(parts.path, parse_qs(parts.query))
                return
            super().do_GET()

        def _handle_queue(self, query: dict[str, list[str]]) -> None:
            try:
                limit = int((query.get("limit") or ["100"])[0])
            except ValueError:
                limit = 100
            limit = max(1, min(limit, QUEUE_LIMIT_MAX))
            conn = _connect()
            try:
                digest_date = library.latest_digest_date(conn)
                today = library.list_unread(conn, limit, on_or_after=digest_date)
                backlog = library.list_unread(conn, limit, before=digest_date) if digest_date else []
                backlog_count = library.count_unread(conn, before=digest_date) if digest_date else 0
            finally:
                conn.close()
            # Today is worked best-first so stopping early still covers the must-reads.
            today.sort(key=lambda a: a.score, reverse=True)
            self._ok({
                "digest_date": digest_date,
                "today": [_article_json(a) for a in today],
                "backlog": [_article_json(a) for a in backlog],
                "backlog_count": backlog_count,
            })

        def _handle_article(self, message_id: str, query: dict[str, list[str]]) -> None:
            reader_mode = query.get("mode") == ["reader"]
            conn = _connect()
            try:
                article = library.get_article(conn, message_id)
            finally:
                conn.close()
            if article is None or not article.archive_path:
                self.send_error(404, "No archived copy for this article")
                return
            # archive_path is our own DB value, but resolve-and-contain anyway so a
            # crafted id can never read outside the web root.
            index_file = serve_root / article.archive_path.lstrip("/")
            try:
                resolved = index_file.resolve()
                resolved.relative_to(serve_root.resolve())
                html = resolved.read_text(encoding="utf-8")
            except (OSError, ValueError):
                self.send_error(404, "Archived copy is missing on disk")
                return
            out = reader_page.render_article(
                html, reader_page.article_base_href(article.archive_path),
                reader_mode=reader_mode,
            )
            self._send(out, "text/html; charset=utf-8")

        def _handle_summary_page(self, message_id: str) -> None:
            conn = _connect()
            try:
                article = library.get_article(conn, message_id)
                summary = library.get_summary(conn, message_id)
            finally:
                conn.close()
            if article is None or summary is None:
                self.send_error(404, "No summary for this article")
                return
            self._send(
                reader_page.render_summary_page(
                    article.subject,
                    summary["summary_md"],
                    model=summary["model"],
                    source=summary["source"],
                    word_count=summary["word_count"] or 0,
                    created_at=(summary["created_at"] or "")[:10],
                ),
                "text/html; charset=utf-8",
            )

        def _handle_library(self, path: str, query: dict[str, list[str]]) -> None:
            conn = _connect()
            try:
                if path == "/library":
                    if query.get("starred"):
                        html = library_pages.render_list(
                            "★ Starred", library.list_starred(conn),
                            "Articles you've starred to follow up on.",
                        )
                    elif query.get("source") == ["url"]:
                        html = library_pages.render_list(
                            "📌 Saved", library.list_saved(conn),
                            "Articles you saved from the web with `newsfeed add`.",
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
                "/api/dismiss": self._handle_dismiss,
                "/api/dismiss-backlog": self._handle_dismiss_backlog,
                "/api/tag": self._handle_tag,
                "/api/summarize": self._handle_summarize,
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

        def _handle_dismiss(self, body: dict) -> None:
            ids = body.get("message_ids") or ([body["message_id"]] if body.get("message_id") else [])
            if not isinstance(ids, list) or not ids:
                self.send_error(400, "Required: message_id or message_ids")
                return
            dismissed = bool(body.get("dismissed", True))
            self._write(lambda c: library.set_dismissed(c, [str(i) for i in ids], dismissed))
            self._ok({"ok": True})

        def _handle_dismiss_backlog(self, body: dict) -> None:
            try:
                max_score = float(body.get("max_score", BACKLOG_DISMISS_MAX_SCORE))
            except (TypeError, ValueError):
                self.send_error(400, "max_score must be a number")
                return
            result: list[str] = []

            def run(c: sqlite3.Connection) -> None:
                digest_date = library.latest_digest_date(c)
                if digest_date:
                    result.extend(library.dismiss_backlog(c, digest_date, max_score))

            self._write(run)
            self._ok({"ok": True, "message_ids": result})

        def _handle_tag(self, body: dict) -> None:
            message_id = body.get("message_id")
            tag = body.get("tag")
            op = body.get("op")
            if not message_id or not tag or op not in ("add", "remove", "clear"):
                self.send_error(400, "Required: message_id, tag, op in add|remove|clear")
                return
            self._write(lambda c: library.apply_tag_delta(c, str(message_id), str(tag), op))
            self._ok({"ok": True})

        def _handle_summarize(self, body: dict) -> None:
            message_id = body.get("message_id")
            if not message_id:
                self.send_error(400, "Required: message_id")
                return
            conn = _connect()
            try:
                article = library.get_article(conn, str(message_id))
                article_body = library.get_body(conn, str(message_id))
            finally:
                conn.close()
            if article is None:
                self.send_error(404, "No such article")
                return
            # The model call takes tens of seconds; run it (and any full-text
            # re-fetch) off the write lock, then take the lock only for the write.
            try:
                result = asyncio.run(deep_summary.generate(article, article_body))
            except Exception as e:
                logger.exception("Summarization failed for %s", message_id)
                self.send_error(502, f"Summarization failed: {e}")
                return
            self._write(lambda c: library.set_summary(
                c, str(message_id), result.summary_md,
                model=result.model, source=result.source, word_count=result.word_count,
            ))
            self._ok({
                "ok": True,
                "model": result.model,
                "source": result.source,
                "word_count": result.word_count,
            })

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
            self._send(html, "text/html; charset=utf-8")

        def _send(self, text: str, content_type: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _ok(self, data: object) -> None:
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
