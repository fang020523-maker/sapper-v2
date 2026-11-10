"""Gateway contract tests against a local fake model server.

The fake speaks both wire protocols, records what arrived, and can be
scripted to answer 429/401/500 before a good response so retry and
no-retry behaviour are exercised on the real HTTP stack.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.config import LLMConfig
from core.llm.router import (
    AnthropicGateway,
    GatewayError,
    LLMRequest,
    OpenAIGateway,
    UsageTracker,
    build_gateway,
)


class FakeLLMHandler(BaseHTTPRequestHandler):
    server_version = "FakeLLM/1"

    def do_POST(self):  # noqa: N802 — http.server API
        server: FakeLLMServer = self.server.fake  # type: ignore[union-attr]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        index = len(server.done)
        if index < len(server.script):
            item = server.script[index]
            if callable(item):
                status, payload = item(body)
            else:
                status, payload = item
        else:
            status, payload = 200, server.default
        server.done.append(status)
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        for key, value in server.extra_headers(len(server.done) - 1).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):  # silence the test log
        pass


class FakeLLMServer:
    def __init__(self, default: dict):
        self.default = default
        self.script: list[tuple[int, dict]] = []
        self.retry_after: dict[int, str] = {}
        self.requests: list[dict] = []
        self.done: list[int] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLMHandler)
        # Handlers reach the fake's state through the httpd instance they
        # are handed; the handler's own `.server` is the httpd itself.
        self._httpd.fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def extra_headers(self, index: int) -> dict[str, str]:
        if index in self.retry_after:
            return {"Retry-After": self.retry_after[index]}
        return {}

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


OPENAI_OK = {
    "choices": [{"message": {"role": "assistant", "content": '{"kind":"fact"}'}}],
    "usage": {"prompt_tokens": 101, "completion_tokens": 7},
}
ANTHROPIC_OK = {
    "content": [{"type": "text", "text": '{"kind":"fact"}'}],
    "usage": {"input_tokens": 55, "output_tokens": 5},
}


def request() -> LLMRequest:
    return LLMRequest(
        role="reason",
        system="You are a solver.",
        messages=({"role": "user", "content": "hello"},),
        json_mode=True,
        max_output_tokens=64,
    )


class GatewayContractTest(unittest.TestCase):
    def setUp(self):
        # Keep the local fake off any ambient proxy configuration.
        self._saved = {k: os.environ.get(k) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")}
        os.environ["no_proxy"] = "localhost,127.0.0.1"
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ.pop(k, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_openai_wire_shape(self):
        server = FakeLLMServer(OPENAI_OK)
        try:
            config = LLMConfig(flavor="openai", base_url=server.base + "/v1",
                               model="gpt-test", api_key="handle-1")
            usage = UsageTracker()
            response = OpenAIGateway(config, usage).complete(request())
            self.assertEqual(response.text, '{"kind":"fact"}')
            self.assertEqual(usage.calls, 1)
            self.assertEqual(usage.input_tokens, 101)
            sent = server.requests[0]
            # Exactly one /v1 — the platform's base plus the SDK's suffix.
            self.assertEqual(sent["path"], "/v1/chat/completions")
            self.assertEqual(sent["headers"]["Authorization"], "Bearer handle-1")
            self.assertEqual(sent["body"]["messages"][0]["role"], "system")
            self.assertEqual(sent["body"]["response_format"], {"type": "json_object"})
        finally:
            server.close()

    def test_anthropic_wire_shape(self):
        server = FakeLLMServer(ANTHROPIC_OK)
        try:
            config = LLMConfig(flavor="anthropic", base_url=server.base,
                               model="claude-test", api_key="handle-2")
            usage = UsageTracker()
            response = AnthropicGateway(config, usage).complete(request())
            self.assertEqual(response.input_tokens, 55)
            sent = server.requests[0]
            self.assertEqual(sent["path"], "/v1/messages")
            # Both auth names travel together — no guessing the wire style.
            self.assertEqual(sent["headers"]["X-Api-Key"], "handle-2")
            self.assertEqual(sent["headers"]["Authorization"], "Bearer handle-2")
            self.assertEqual(sent["body"]["system"], "You are a solver.")
            self.assertNotIn("response_format", sent["body"])
        finally:
            server.close()

    def test_retry_after_429_then_success(self):
        server = FakeLLMServer(OPENAI_OK)
        server.script = [(429, {}), (200, OPENAI_OK)]
        server.retry_after = {0: "0"}
        try:
            config = LLMConfig(flavor="openai", base_url=server.base, model="m", api_key="k")
            usage = UsageTracker()
            response = OpenAIGateway(config, usage).complete(request())
            self.assertEqual(usage.retries, 1)
            self.assertEqual(usage.calls, 1)
            self.assertEqual(len(server.requests), 2)
        finally:
            server.close()

    def test_401_is_not_retryable(self):
        server = FakeLLMServer(OPENAI_OK)
        server.script = [(401, {"error": "unauthorized"})]
        try:
            config = LLMConfig(flavor="openai", base_url=server.base, model="m", api_key="bad")
            usage = UsageTracker()
            with self.assertRaises(GatewayError) as ctx:
                OpenAIGateway(config, usage).complete(request())
            self.assertFalse(ctx.exception.retryable)
            self.assertEqual(ctx.exception.status, 401)
            self.assertEqual(len(server.requests), 1)  # no second POST
        finally:
            server.close()

    def test_unfunded_gateway_refused(self):
        config = LLMConfig(flavor="openai", base_url="https://x", model="m", api_key=None)
        with self.assertRaises(GatewayError):
            OpenAIGateway(config)

    def test_router_picks_one_flavor_only(self):
        config = LLMConfig(flavor="openai", base_url="https://x", model="m", api_key="k")
        self.assertIsInstance(build_gateway(config), OpenAIGateway)
        with self.assertRaises(GatewayError):
            build_gateway(LLMConfig(flavor="vertex", base_url="https://x", model="m", api_key="k"))


if __name__ == "__main__":
    unittest.main()
