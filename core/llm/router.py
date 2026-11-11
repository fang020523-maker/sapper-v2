"""Flavor routing and the shared request/response shapes.

Pure stdlib on purpose: the gateway must work in an offline container on
a bare python image, and the two wire protocols we speak are plain JSON
over HTTP. The SDK-shaped names (base_url, api_key, model) mirror what
the platform injects; no other credential source exists in this module.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from app.config import LLMConfig

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT_S = 120
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 3


class GatewayError(Exception):
    """A model call failed. retryable says whether another attempt makes sense."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class LLMRequest:
    role: str                      # reason / explore / verify / repair — for usage only
    system: str
    messages: tuple[dict, ...]     # {"role": "user"|"assistant", "content": str}
    max_output_tokens: int = 4096
    json_mode: bool = False        # ask for a JSON object when the flavor supports it
    timeout_s: int = DEFAULT_TIMEOUT_S
    turn_id: int = 0
    extra_body: dict = field(default_factory=dict)  # vendor knobs (e.g. thinking on/off)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


@dataclass
class UsageTracker:
    """One counters object per round; RunSummary reads it at the end."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0
    retries: int = 0

    def observe(self, response: LLMResponse) -> None:
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens

    def observe_error(self) -> None:
        self.errors += 1


class _HTTPGateway:
    """Shared transport: auth, retries, timeouts — never a second base URL."""

    flavor = ""
    auth_headers: dict[str, str] = {}

    def __init__(self, config: LLMConfig, usage: UsageTracker | None = None):
        if not config.funded:
            raise GatewayError(
                "LLM gateway built without a funded key — refusing to guess "
                "another provider or endpoint",
            )
        self.config = config
        self.usage = usage or UsageTracker()
        self.extra_body = dict(config.extra_body)
        # No ambient proxy: the platform's gateway address is reachable
        # directly, and a developer machine's proxy settings must not
        # silently capture the run's model traffic.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- subclasses implement the protocol shape --------------------------

    def endpoint(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def body(self, request: LLMRequest) -> dict:
        raise NotImplementedError

    def parse(self, payload: dict) -> tuple[str, int, int]:
        """Return (text, input_tokens, output_tokens)."""
        raise NotImplementedError

    # -- one door out ------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        url = self.endpoint()
        data = json.dumps(self.body(request)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.headers(),
        }
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with self._opener.open(req, timeout=request.timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                text, in_tok, out_tok = self.parse(payload)
                response = LLMResponse(
                    text=text,
                    model=self.config.model or "",
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                self.usage.observe(response)
                return response
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                    self.usage.retries += 1
                    _sleep_retry_after(exc)
                    continue
                self.usage.observe_error()
                raise GatewayError(
                    f"{self.flavor} gateway returned {status} for role "
                    f"{request.role!r}",
                    retryable=status in RETRYABLE_STATUS,
                    status=status,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < MAX_ATTEMPTS:
                    self.usage.retries += 1
                    time.sleep(min(2 ** attempt, 8))
                    continue
                self.usage.observe_error()
                raise GatewayError(
                    f"{self.flavor} gateway unreachable for role "
                    f"{request.role!r}: {exc.__class__.__name__}",
                    retryable=True,
                ) from exc
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self.usage.observe_error()
                raise GatewayError(
                    f"{self.flavor} gateway returned an unparseable body "
                    f"for role {request.role!r}",
                ) from exc


class OpenAIGateway(_HTTPGateway):
    """Chat Completions over the platform's base URL.

    The platform injectates a base URL already shaped for the SDK, which
    appends ``/chat/completions`` — we match that and never add a second
    ``/v1``.
    """

    flavor = "openai"

    def endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def body(self, request: LLMRequest) -> dict:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m["role"], "content": m["content"]} for m in request.messages)
        body: dict = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra_body)
        body.update(request.extra_body)
        return body

    def parse(self, payload: dict) -> tuple[str, int, int]:
        choice = payload["choices"][0]
        text = choice["message"]["content"] or ""
        usage = payload.get("usage") or {}
        return (
            text,
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
        )


class AnthropicGateway(_HTTPGateway):
    """Messages API over the platform's base URL.

    Both auth headers go out together: the platform injects the key under
    two names precisely so either wire style works, and choosing one by
    guessing is the documented way to arrive unauthenticated.
    """

    flavor = "anthropic"

    def endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/v1/messages"

    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key,
            "authorization": f"Bearer {self.config.api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
        }

    def body(self, request: LLMRequest) -> dict:
        return {
            "model": self.config.model,
            "system": request.system,
            "messages": [
                {"role": m["role"], "content": m["content"]} for m in request.messages
            ],
            "max_tokens": request.max_output_tokens,
        }

    def parse(self, payload: dict) -> tuple[str, int, int]:
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = payload.get("usage") or {}
        return (
            text,
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )


def build_gateway(config: LLMConfig, usage: UsageTracker | None = None) -> _HTTPGateway:
    if config.flavor == "openai":
        return OpenAIGateway(config, usage)
    if config.flavor == "anthropic":
        return AnthropicGateway(config, usage)
    raise GatewayError(
        f"no gateway for LLM_FLAVOR {config.flavor!r} — one flavor per run, "
        "never a fallback"
    )


def _sleep_retry_after(exc: urllib.error.HTTPError) -> None:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    try:
        delay = min(float(retry_after), 30.0) if retry_after else 2.0
    except ValueError:
        delay = 2.0
    time.sleep(max(delay, 0.5))
