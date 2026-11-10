"""A minimal in-process CTFy platform double for local benchmark runs.

Same idea as the tests' FakeCtfyServer, trimmed to what the runner needs:
hand out the challenge's visible question, grade one answer shape (the
injected flag), and record every submission so the report knows exactly
what the agent spent.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        fake = self.server.fake  # type: ignore[attr-defined]
        fake.requests.append({"method": "GET", "path": self.path})
        if self.path.startswith("/api/v1/challenges/"):
            blob = json.dumps(
                {
                    "id": fake.challenge_id,
                    "questions": [{"id": "flag", "prompt": fake.question_prompt}],
                }
            ).encode()
            self._reply(200, blob)
        else:
            self._reply(404, b"{}")

    def do_POST(self):  # noqa: N802
        fake = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        fake.requests.append({"method": "POST", "path": self.path, "body": body})
        if self.path == "/api/v1/submissions":
            correct = body.get("answer") == fake.flag
            blob = json.dumps(
                {
                    "id": f"s{len(fake.requests)}",
                    "correct": correct,
                    "question_id": body.get("question_id"),
                    "challenge_fully_solved": correct,
                    "attempts_remaining": 4,
                }
            ).encode()
            self._reply(200, blob)
        else:
            self._reply(404, b"{}")

    def _reply(self, status: int, blob: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):
        pass


class FakePlatform:
    def __init__(self, challenge_id: str, question_prompt: str, flag: str):
        self.challenge_id = challenge_id
        self.question_prompt = question_prompt
        self.flag = flag
        self.requests: list[dict] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.fake = self  # type: ignore[attr-defined]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @property
    def submissions(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]

    @property
    def solved(self) -> bool:
        return any(
            r["body"].get("answer") == self.flag for r in self.submissions
        )

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
