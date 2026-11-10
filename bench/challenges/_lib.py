"""Shared helpers for the local benchmark challenges.

Each challenge is one tiny stdlib HTTP server with its flag injected by
the runner via --flag, so every run can use a fresh value and a solved
question is proof the agent read it out of the target, not a memory.
"""
from __future__ import annotations

import argparse
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class ChallengeHandler(BaseHTTPRequestHandler):
    """Subclass sets routes(); serve() wires everything up."""

    def routes(self, path: str, query: dict, body: dict) -> tuple[int, str, str] | None:
        """Return (status, content_type, body) or None to fall through 404."""
        raise NotImplementedError

    flag = "flag{unset}"

    def do_GET(self):  # noqa: N802
        self._handle({})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        body = {k: v[0] for k, v in parse_qs(raw).items()}
        self._handle(body)

    def _handle(self, body: dict):
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        result = self.routes(parts.path, query, body)
        if result is None:
            result = (404, "text/plain", "404 nothing here")
        status, ctype, payload = result
        blob = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):
        pass


def serve(handler_cls, name: str):
    parser = argparse.ArgumentParser(prog=name)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--flag", required=True)
    args = parser.parse_args()
    handler_cls.flag = args.flag
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler_cls)
    print(f"{name} ready on {args.port}", flush=True)
    httpd.serve_forever()
