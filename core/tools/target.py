"""Target client: HTTP and TCP against this run's published endpoints.

Same transport rules as the rest of the shell: pure stdlib, no ambient
proxy, hard output caps and timeouts, and a scope check on every URL —
including each redirect hop, which is the classic way a target walks an
agent somewhere it should not go. What a target returns is data, never
instructions; nothing here parses or obeys it beyond length and bytes.
"""
from __future__ import annotations

import http.cookiejar
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from app.scope import EndpointScope, ScopeViolation

MAX_RESPONSE_BYTES = 256 * 1024
HTTP_TIMEOUT_S = 30
TCP_TIMEOUT_S = 20
MAX_REDIRECTS = 5


@dataclass(frozen=True)
class TargetHTTPRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class TargetHTTPResponse:
    status: int
    url: str  # final URL after redirects
    headers: dict[str, str]
    body: bytes
    truncated: bool


@dataclass(frozen=True)
class TargetTCPRequest:
    host: str
    port: int
    payload: bytes = b""
    read_expected_bytes: int = 4096


@dataclass(frozen=True)
class TargetTCPResponse:
    ok: bool
    data: bytes
    error: str | None = None


class TargetUnreachable(Exception):
    """The published endpoint did not answer. Data, not a crash."""


class TargetClient:
    """No credentials exist in this class — by construction."""

    def __init__(self, scope: EndpointScope, *, max_bytes: int = MAX_RESPONSE_BYTES):
        self.scope = scope
        self.max_bytes = max_bytes
        # A session jar: many challenges hand a cookie at login and refuse
        # every later request without it. One jar per client — the scope
        # pins us to this run's endpoints anyway.
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cookies),
            _NoRedirect(),
        )

    def http(self, request: TargetHTTPRequest) -> TargetHTTPResponse:
        url = request.url
        method = request.method.upper()
        for _hop in range(MAX_REDIRECTS + 1):
            self.scope.check_http(url)  # every hop, not just the first
            req = urllib.request.Request(
                url,
                data=request.body,
                headers={
                    "User-Agent": "sapper-ctfy/0.1",
                    **{
                        k: v
                        for k, v in request.headers.items()
                        if k.lower() not in ("authorization", "cookie", "proxy-authorization", "host")
                    },
                },
                method=method,
            )
            try:
                with self._opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                    body = resp.read(self.max_bytes + 1)
                    return TargetHTTPResponse(
                        status=resp.status,
                        url=resp.url,
                        headers={k: v for k, v in resp.headers.items()},
                        body=body[: self.max_bytes],
                        truncated=len(body) > self.max_bytes,
                    )
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = exc.headers.get("Location")
                    if not location:
                        raise
                    url = _absolute(url, location)
                    if exc.code in (303,) or (exc.code in (301, 302) and method == "POST"):
                        method = "GET"
                        request = TargetHTTPRequest(
                            url=url, method="GET",
                            headers=request.headers, body=None,
                        )
                    continue
                body = exc.read(self.max_bytes + 1)
                return TargetHTTPResponse(
                    status=exc.code,
                    url=url,
                    headers={k: v for k, v in (exc.headers or {}).items()},
                    body=body[: self.max_bytes],
                    truncated=len(body) > self.max_bytes,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise TargetUnreachable(
                    f"{method} {url}: {exc.__class__.__name__}"
                ) from exc
        raise ScopeViolation("too many redirects (loop or walk-out attempt?)")

    def tcp(self, request: TargetTCPRequest) -> TargetTCPResponse:
        self.scope.check_tcp(request.host, request.port)
        try:
            with socket.create_connection(
                (request.host, request.port), timeout=TCP_TIMEOUT_S
            ) as sock:
                sock.settimeout(TCP_TIMEOUT_S)
                if request.payload:
                    sock.sendall(request.payload)
                data = sock.recv(request.read_expected_bytes)
            return TargetTCPResponse(ok=True, data=data)
        except OSError as exc:
            return TargetTCPResponse(ok=False, data=b"", error=str(exc))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Intercept redirects ourselves so each hop is scope-checked."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _absolute(base: str, location: str) -> str:
    if location.startswith(("http://", "https://")):
        return location
    from urllib.parse import urljoin

    return urljoin(base, location)
