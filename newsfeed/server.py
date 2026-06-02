"""The Archive Server: serves Digests and Archives over the home network.

Rooted at the ``serve/`` directory, which contains only ``digests/``, ``archive/``,
and ``read_state.json``. SimpleHTTPRequestHandler normalises and rejects paths that
escape its root, so credentials in the project root are unreachable. Run as a
systemd user service (see deploy/newsfeed-server.service).

POST endpoints (JSON body, JSON response):
  /api/feedback   {subject, sender, score}  — sets feedback field in feedback.yaml
  /api/mark-read  {message_id}              — records in serve/read_state.json
"""
import json
import logging
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from .config import paths

logger = logging.getLogger(__name__)

HOST = "0.0.0.0"
PORT = 8080


def serve(
    serve_root: Path | None = None,
    host: str = HOST,
    port: int = PORT,
    feedback_path: Path | None = None,
) -> None:
    p = paths()
    serve_root = serve_root or p.serve
    feedback_path = feedback_path or p.feedback
    serve_root.mkdir(parents=True, exist_ok=True)
    read_state_path = serve_root / "read_state.json"
    _lock = threading.Lock()

    class _Handler(SimpleHTTPRequestHandler):
        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self.send_error(400, "Invalid JSON")
                return

            if self.path == "/api/feedback":
                self._handle_feedback(body)
            elif self.path == "/api/mark-read":
                self._handle_mark_read(body)
            else:
                self.send_error(404)

        def _handle_feedback(self, body: dict) -> None:
            try:
                subject = str(body["subject"])
                sender = str(body["sender"])
                score = float(body["score"])
            except (KeyError, ValueError):
                self.send_error(400, "Required: subject, sender, score")
                return
            if not (0.0 <= score <= 10.0):
                self.send_error(400, "score must be 0–10")
                return

            with _lock:
                data = yaml.safe_load(feedback_path.read_text(encoding="utf-8")) or []
                for entry in data:
                    if entry.get("subject") == subject and entry.get("sender") == sender:
                        entry["feedback"] = round(score, 1)
                        break
                feedback_path.write_text(
                    yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
                    encoding="utf-8",
                )
            self._ok({"ok": True})

        def _handle_mark_read(self, body: dict) -> None:
            try:
                message_id = str(body["message_id"])
            except KeyError:
                self.send_error(400, "Required: message_id")
                return

            with _lock:
                state = set(json.loads(read_state_path.read_text())) if read_state_path.exists() else set()
                state.add(message_id)
                read_state_path.write_text(json.dumps(sorted(state)))
            self._ok({"ok": True})

        def _ok(self, data: dict) -> None:
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    handler = partial(_Handler, directory=str(serve_root))
    httpd = ThreadingHTTPServer((host, port), handler)
    logger.info(f"Archive Server serving {serve_root} on http://{host}:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Archive Server")
        httpd.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    serve()
