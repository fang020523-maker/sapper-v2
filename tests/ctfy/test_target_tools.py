"""Scope guard and target client: every hop checked, every byte capped."""
from __future__ import annotations

import http.server
import os
import threading
import unittest
from pathlib import Path

from app.scope import EndpointScope, ScopeViolation
from core.tools.target import (
    TargetClient,
    TargetHTTPRequest,
    TargetTCPRequest,
)
from ctfy.schema import Endpoint

from .test_platform_client import ProxyCleanTest


def endpoints_for(host: str, port: int) -> tuple[Endpoint, ...]:
    return (Endpoint(label="web", type="http", url=f"http://{host}:{port}/"),)


class ScopeTest(unittest.TestCase):
    def test_http_url_must_match_published_endpoint(self):
        scope = EndpointScope.from_endpoints(
            (Endpoint(label="web", type="http", url="http://target.example:8080/"),)
        )
        scope.check_http("http://target.example:8080/admin")
        with self.assertRaises(ScopeViolation):
            scope.check_http("http://target.example:8081/")  # wrong port
        with self.assertRaises(ScopeViolation):
            scope.check_http("http://evil.example/")
        with self.assertRaises(ScopeViolation):
            scope.check_http("file:///etc/passwd")

    def test_tcp_scope(self):
        scope = EndpointScope.from_endpoints(
            (Endpoint(label="nc", type="tcp", url="target.example:9999"),)
        )
        scope.check_tcp("target.example", 9999)
        with self.assertRaises(ScopeViolation):
            scope.check_tcp("target.example", 22)
        with self.assertRaises(ScopeViolation):
            scope.check_tcp("elsewhere.example", 9999)


class _Handler(http.server.BaseHTTPRequestHandler):
    seen_auth: list[str | None] = []

    def do_GET(self):  # noqa: N802
        type(self).seen_auth.append(self.headers.get("Authorization"))
        if self.path == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/landed")  # unpublished port
            self.end_headers()
        elif self.path == "/redirect-in":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
        elif self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            body = b"A" * 100
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class TargetHTTPTest(ProxyCleanTest):
    @classmethod
    def setUpClass(cls):
        cls._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls._httpd.server_address[1]
        threading.Thread(target=cls._httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls._httpd.shutdown()
        cls._httpd.server_close()

    def client(self) -> TargetClient:
        scope = EndpointScope.from_endpoints(endpoints_for("127.0.0.1", self.port))
        return TargetClient(scope)

    def test_get_within_scope(self):
        response = self.client().http(TargetHTTPRequest(url=f"http://127.0.0.1:{self.port}/"))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body), 100)

    def test_internal_redirect_followed(self):
        response = self.client().http(
            TargetHTTPRequest(url=f"http://127.0.0.1:{self.port}/redirect-in")
        )
        self.assertEqual(response.status, 200)
        self.assertIn("/final", response.url)

    def test_redirect_out_of_scope_blocked(self):
        with self.assertRaises(ScopeViolation):
            self.client().http(
                TargetHTTPRequest(url=f"http://127.0.0.1:{self.port}/redirect-out")
            )

    def test_error_status_returned_not_raised(self):
        response = self.client().http(
            TargetHTTPRequest(url=f"http://127.0.0.1:{self.port}/missing")
        )
        self.assertEqual(response.status, 404)

    def test_credential_headers_never_sent(self):
        # Ask with an Authorization header anyway; what the server
        # actually receives must not contain it.
        _Handler.seen_auth.clear()
        self.client().http(
            TargetHTTPRequest(
                url=f"http://127.0.0.1:{self.port}/",
                headers={"Authorization": "Bearer platform-token", "X-Note": "fine"},
            )
        )
        self.assertEqual(_Handler.seen_auth, [None])


class TargetTCPTest(ProxyCleanTest):
    def test_tcp_roundtrip(self):
        import socketserver

        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                data = self.request.recv(1024)
                self.request.sendall(b"echo:" + data)

        server = socketserver.TCPServer(("127.0.0.1", 0), Echo)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            scope = EndpointScope.from_endpoints(
                (Endpoint(label="nc", type="tcp", url=f"127.0.0.1:{port}"),)
            )
            client = TargetClient(scope)
            response = client.tcp(
                TargetTCPRequest(host="127.0.0.1", port=port, payload=b"hi")
            )
            self.assertTrue(response.ok)
            self.assertEqual(response.data, b"echo:hi")
            with self.assertRaises(ScopeViolation):
                client.tcp(TargetTCPRequest(host="127.0.0.1", port=port + 1))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
