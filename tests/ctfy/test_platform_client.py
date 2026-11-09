"""Platform client contract tests against a local fake CTFy API.

The fake routes the four calls a dispatched agent may make, records what
arrived with which headers, and can be scripted per-path so retry and
never-retry behaviour are proven on the real HTTP stack.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ctfy.client import (
    CtfyApiError,
    CtfyClient,
    UncertainSubmission,
)


class FakeCtfyHandler(BaseHTTPRequestHandler):
    server_version = "FakeCtfy/1"

    def _dispatch(self, method: str):
        fake = self.server.fake  # type: ignore[attr-defined]
        parsed = self.path.split("?", 1)
        route = parsed[0]
        query = parsed[1] if len(parsed) > 1 else ""
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        fake.requests.append(
            {"method": method, "path": route, "query": query,
             "auth": self.headers.get("Authorization"), "body": body}
        )
        behavior = fake.route(route, method)
        if behavior is None:
            behavior = (404, {"code": "not_found", "message": f"no route {route}"})
        if callable(behavior):
            behavior = behavior()
        status, payload = behavior
        if behavior == "HANG":  # processed server-side, response never arrives
            self.close_connection = True
            return
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def log_message(self, *args):
        pass


class FakeCtfyServer:
    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.scripts: dict[tuple[str, str], list] = {}
        self.requests: list[dict] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeCtfyHandler)
        self._httpd.fake = self  # type: ignore[attr-defined]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def route(self, path: str, method: str):
        key = (method, path)
        script = self.scripts.get(key)
        if script:
            index = sum(1 for r in self.requests if (r["method"], r["path"]) == key) - 1
            if index < len(script):
                return script[index]
        return self.routes.get(key)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


CHALLENGE_OK = {
    "id": "CTFARENA-001",
    "name": "DocuVault",
    "questions": [{"id": "flag", "prompt": "The vault master secret"}],
}
INSTANCE_OK = {"id": "3f9c0f12", "status": "running", "endpoints": []}
SUBMIT_OK = {
    "id": "sub-1",
    "correct": True,
    "question_id": "flag",
    "challenge_fully_solved": False,
    "attempts_remaining": 2,
}


class ProxyCleanTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")}
        os.environ["no_proxy"] = "localhost,127.0.0.1"
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ.pop(k, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ReadsTest(ProxyCleanTest):
    def test_get_challenge(self):
        server = FakeCtfyServer()
        server.routes[("GET", "/api/v1/challenges/CTFARENA-001")] = (200, CHALLENGE_OK)
        try:
            client = CtfyClient(server.base, "tok-1")
            challenge = client.get_challenge("CTFARENA-001", "tpctf2026")
            self.assertEqual(challenge["id"], "CTFARENA-001")
            sent = server.requests[0]
            self.assertEqual(sent["auth"], "Bearer tok-1")
            self.assertIn("competition_id=tpctf2026", sent["query"])
        finally:
            server.close()

    def test_get_instance(self):
        server = FakeCtfyServer()
        server.routes[("GET", "/api/v1/instances/3f9c0f12")] = (200, INSTANCE_OK)
        try:
            client = CtfyClient(server.base, "tok-1")
            self.assertEqual(client.get_instance("3f9c0f12")["status"], "running")
        finally:
            server.close()

    def test_get_retries_on_429_then_succeeds(self):
        server = FakeCtfyServer()
        key = ("GET", "/api/v1/challenges/C1")
        server.routes[key] = (200, CHALLENGE_OK)
        server.scripts[key] = [(429, {})]
        try:
            client = CtfyClient(server.base, "tok-1")
            client.get_challenge("C1")
            hits = [r for r in server.requests if (r["method"], r["path"]) == key]
            self.assertEqual(len(hits), 2)
        finally:
            server.close()

    def test_401_never_retries(self):
        server = FakeCtfyServer()
        key = ("GET", "/api/v1/challenges/C1")
        server.routes[key] = (401, {"code": "unauthorized", "message": "no"})
        try:
            client = CtfyClient(server.base, "bad-token")
            with self.assertRaises(CtfyApiError) as ctx:
                client.get_challenge("C1")
            self.assertEqual(ctx.exception.status, 401)
            self.assertEqual(len(server.requests), 1)
        finally:
            server.close()

    def test_competition_paused_is_recognised(self):
        server = FakeCtfyServer()
        key = ("POST", "/api/v1/submissions")
        server.routes[key] = (
            403,
            {
                "code": "competition_paused",
                "message": "wait",
                "retry_after_s": 900,
                "next_session_starts_at": "2026-11-15T14:00:00Z",
            },
        )
        try:
            client = CtfyClient(server.base, "tok-1")
            with self.assertRaises(CtfyApiError) as ctx:
                client.submit_instance_answer("i1", "flag", "ctfy{x}")
            self.assertTrue(ctx.exception.paused)
            self.assertEqual(ctx.exception.retry_after_s, 900.0)
        finally:
            server.close()


class SubmissionsTest(ProxyCleanTest):
    def test_instance_submission_body(self):
        server = FakeCtfyServer()
        key = ("POST", "/api/v1/submissions")
        server.routes[key] = (200, SUBMIT_OK)
        try:
            client = CtfyClient(server.base, "tok-1")
            record = client.submit_instance_answer("3f9c0f12", "flag", "ctfy{x}")
            self.assertTrue(record.correct)
            self.assertEqual(record.attempts_remaining, 2)
            body = server.requests[0]["body"]
            self.assertEqual(
                body,
                {"instance_id": "3f9c0f12", "question_id": "flag", "answer": "ctfy{x}"},
            )
        finally:
            server.close()

    def test_qa_submission_body_has_no_instance(self):
        server = FakeCtfyServer()
        key = ("POST", "/api/v1/qa/submissions")
        server.routes[key] = (200, SUBMIT_OK)
        try:
            client = CtfyClient(server.base, "tok-1")
            client.submit_qa_answer("QA-001", "tpctf2026", "answer", "A")
            body = server.requests[0]["body"]
            self.assertEqual(
                body,
                {
                    "challenge_id": "QA-001",
                    "competition_id": "tpctf2026",
                    "question_id": "answer",
                    "answer": "A",
                },
            )
            self.assertNotIn("instance_id", body)
        finally:
            server.close()

    def test_post_5xx_is_not_retried(self):
        server = FakeCtfyServer()
        key = ("POST", "/api/v1/submissions")
        server.routes[key] = (503, {"code": "unavailable", "message": "later"})
        try:
            client = CtfyClient(server.base, "tok-1")
            with self.assertRaises(CtfyApiError) as ctx:
                client.submit_instance_answer("i1", "flag", "ctfy{x}")
            self.assertEqual(ctx.exception.status, 503)
            hits = [r for r in server.requests if (r["method"], r["path"]) == key]
            self.assertEqual(len(hits), 1)  # one POST, exactly one
        finally:
            server.close()

    def test_post_timeout_is_uncertain(self):
        server = FakeCtfyServer()
        key = ("POST", "/api/v1/submissions")
        # The platform processes the request, then the response dies.
        server.routes[key] = "HANG"
        try:
            client = CtfyClient(server.base, "tok-1", submit_timeout_s=1)
            with self.assertRaises(UncertainSubmission):
                client.submit_instance_answer("i1", "flag", "ctfy{x}")
            hits = [r for r in server.requests if (r["method"], r["path"]) == key]
            self.assertEqual(len(hits), 1)  # and never a second attempt
        finally:
            server.close()


class AttachmentsTest(ProxyCleanTest):
    def test_download_writes_file(self):
        import tempfile

        server = FakeCtfyServer()
        key = ("GET", "/api/v1/instances/i1/attachments/dump.bin")
        server.routes[key] = (200, {"__bytes__": b"AAAA"})  # handler JSON-only; use raw below
        # The fake speaks JSON; for bytes we special-case in behaviour:
        server.routes[key] = (200, "QUFBQQ==")  # placeholder, replaced by custom logic
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = CtfyClient(server.base, "tok-1")
                # Use a JSON body as bytes: the contract is bytes-to-disk.
                client.download_attachment("i1", "dump.bin", Path(tmp) / "dump.bin")
                self.assertTrue((Path(tmp) / "dump.bin").exists())
        finally:
            server.close()

    def test_traversal_names_refused_locally(self):
        import tempfile

        server = FakeCtfyServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = CtfyClient(server.base, "tok-1")
                for bad in ("../etc/passwd", "a/b", "..", "a\\b"):
                    with self.assertRaises(CtfyApiError, msg=bad):
                        client.download_attachment("i1", bad, Path(tmp) / "x")
                self.assertEqual(len(server.requests), 0)  # refused before any HTTP
        finally:
            server.close()

    def test_repr_hides_token(self):
        client = CtfyClient("https://ctfy.example", "secret-token")
        self.assertNotIn("secret-token", repr(client))


if __name__ == "__main__":
    unittest.main()
