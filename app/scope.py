"""Scope guard: the target's published endpoints are the whole world.

Every request the agent makes to a target is checked against the
endpoints this round's task published — before it leaves the process,
on every redirect hop, with no exception path. The platform's bearer
and the model key are simply never in scope objects or target clients:
not sending a credential is stronger than promising not to.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ctfy.schema import Endpoint


class ScopeViolation(Exception):
    """A request aimed somewhere this round's task never published."""


@dataclass(frozen=True)
class EndpointScope:
    allowed: tuple[tuple[str, int | None, str], ...]  # (host, port, scheme)

    @classmethod
    def from_endpoints(cls, endpoints: tuple[Endpoint, ...]) -> "EndpointScope":
        triples = []
        for endpoint in endpoints:
            if endpoint.type in ("http", "https"):
                parts = urlsplit(endpoint.url)
                if not parts.hostname:
                    continue
                triples.append((parts.hostname.lower(), parts.port, parts.scheme))
            elif endpoint.type in ("tcp", "udp", "ssh"):
                # Raw sockets publish host:port urls; ssh may be host only.
                host, _, port = endpoint.url.partition(":")
                if host:
                    triples.append(
                        (host.lower(), int(port) if port.isdigit() else None, endpoint.type)
                    )
        return cls(tuple(dict.fromkeys(triples)))  # order-keeping dedup

    def check_http(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ScopeViolation(f"scheme {parts.scheme!r} is not a target scheme")
        if not parts.hostname:
            raise ScopeViolation(f"url without host: {url!r}")
        self._check(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), parts.scheme)

    def check_tcp(self, host: str, port: int) -> None:
        self._check(host, port, None)

    def _check(self, host: str, port: int | None, scheme: str | None) -> None:
        host = host.lower().rstrip(".")
        for allowed_host, allowed_port, allowed_scheme in self.allowed:
            if host != allowed_host:
                continue
            if allowed_port is not None and port is not None and int(port) != allowed_port:
                continue
            if scheme is not None and allowed_scheme in ("http", "https") and scheme != allowed_scheme:
                continue
            return
        raise ScopeViolation(
            f"{host}:{port} ({scheme or 'tcp'}) is not among this task's "
            "published endpoints"
        )

    def describe(self) -> str:
        return ", ".join(f"{h}:{p or '*'}" for h, p, _ in self.allowed) or "(none)"
