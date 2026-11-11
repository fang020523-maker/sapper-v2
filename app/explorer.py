"""Model-driven exploration loop: one action per turn, evidence in,
answers out. The model proposes; this module disposes.

Every action is one of four shapes, executed here with scope checks,
caps and timeouts — the model never touches a client, a credential or
the filesystem directly. An answer that survives the ledger's own
checks is returned to the coordinator; a stop reason ends the loop
without inventing one.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.scope import EndpointScope, ScopeViolation
from core.evidence import EvidenceStore
from core.llm.router import GatewayError, LLMRequest, UsageTracker
from core.parse import ModelParseError, extract_json_object
from core.tools.target import (
    TargetClient,
    TargetHTTPRequest,
    TargetTCPRequest,
    TargetUnreachable,
)
from ctfy.schema import TaskSnapshot

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MIN_ANSWER_CONFIDENCE = 0.5
MAX_STEPS_DEFAULT = 40
MAX_STEPS_HARD_CAP = 40
MAX_ACTIONS_PER_TURN = 5
ALLOWED_METHODS = ("GET", "POST", "PUT", "HEAD", "OPTIONS", "DELETE")


class Explorer:
    def __init__(
        self,
        gateway,
        target: TargetClient,
        task: TaskSnapshot,
        readme: str,
        evidence: EvidenceStore,
        usage: UsageTracker,
        work_dir: Path,
        *,
        max_steps: int = MAX_STEPS_DEFAULT,
    ):
        self.gateway = gateway
        self.target = target
        self.task = task
        self.readme = readme
        self.evidence = evidence
        self.usage = usage
        self.work_dir = work_dir
        self.max_steps = min(max_steps, MAX_STEPS_HARD_CAP)
        self._template = (PROMPTS_DIR / "explore.md").read_text(encoding="utf-8")
        self._seen_requests: set[tuple[str, str]] = set()

    def run(self, open_question_ids: list[str]) -> tuple[list[dict], str]:
        """Explore until an answer lands, the budget ends, or the model
        says stop. Returns (answer candidates, stop reason)."""
        questions_block = "\n".join(
            f"- {q.id} — {q.prompt}"
            for q in self.task.questions
            if q.id in open_question_ids
        )
        last_reason = "budget"
        for _step in range(self.max_steps):
            prompt = (
                self._template.replace("{readme}", self.readme)
                .replace("{challenge}", _challenge_block(self.task))
                .replace("{questions}", questions_block or "(none)")
                .replace("{endpoints}", _endpoints_block(self.task))
                .replace("{attachments_dir}", str(self.work_dir / "attachments"))
                .replace("{attachments}", _attachments_block(self.task))
                .replace("{evidence}", self.evidence.render_for_prompt())
            )
            try:
                payload = self._ask_model(prompt)
            except GatewayError as exc:
                # The detail names status/reachability, never credentials.
                self.evidence.record("error", f"model gateway failed: {exc}")
                return [], "llm_error"
            if payload is None:
                return [], "model_parse_error"
            thought = str(payload.get("thought", ""))[:500]
            actions = payload.get("actions")
            if not isinstance(actions, list) or not actions:
                single = payload.get("action")
                actions = [single] if isinstance(single, dict) else []
            if not actions:
                self.evidence.record("note", f"model gave unusable action: {thought}")
                last_reason = "bad_action"
                continue
            stop_reason = None
            answered = None
            for action in actions[:MAX_ACTIONS_PER_TURN]:
                if not isinstance(action, dict) or "type" not in action:
                    continue
                kind = action.get("type")
                if kind == "answer":
                    candidate = _answer_candidate(action)
                    if candidate is not None:
                        answered = candidate
                        break
                    last_reason = "bad_answer"
                    continue
                if kind == "stop":
                    stop_reason = str(action.get("reason", "model_stop"))[:200]
                    break
                outcome = self._execute(kind, action)
                self.evidence.record(
                    outcome[0], outcome[1], outcome[2] if len(outcome) > 2 else ""
                )
            if answered is not None:
                return [answered], "answered"
            if stop_reason is not None:
                return [], stop_reason
        return [], last_reason

    # -- one model call, one repair ----------------------------------------

    def _ask_model(self, prompt: str) -> dict | None:
        messages = [{"role": "user", "content": prompt}]
        for _attempt in range(2):
            request = LLMRequest(
                role="explore",
                system=(
                    "You are a CTF solver agent. Output exactly one JSON "
                    "object with a thought and one action."
                ),
                messages=tuple(messages),
                json_mode=True,
                max_output_tokens=8192,
                turn_id=self.usage.calls + 1,
            )
            try:
                response = self.gateway.complete(request)
            except GatewayError:
                raise
            try:
                return extract_json_object(response.text)
            except ModelParseError:
                messages = messages + [
                    {"role": "assistant", "content": response.text[:4000]},
                    {
                        "role": "user",
                        "content": 'That was not valid JSON. Reply with '
                        'exactly one JSON object: {"thought": ..., '
                        '"action": {...}}',
                    },
                ]
        return None

    # -- action execution ----------------------------------------------------

    def _execute(self, kind: str, action: dict) -> tuple[str, str, str]:
        if kind == "http":
            return self._http(action)
        if kind == "tcp":
            return self._tcp(action)
        if kind == "read_attachment":
            return self._read_attachment(action)
        return ("note", f"unknown action type {kind!r} ignored")

    def _http(self, action: dict) -> tuple[str, str, str]:
        url = action.get("url")
        method = str(action.get("method", "GET")).upper()
        if not isinstance(url, str) or method not in ALLOWED_METHODS:
            return ("note", f"refused http action: bad url or method {method}")
        url = _sanitize_url(url)
        headers = {
            str(k): str(v)[:512]
            for k, v in (action.get("headers") or {}).items()
            if isinstance(k, str) and isinstance(v, (str, int, float))
        }
        body = action.get("body")
        body_bytes = None
        if body is not None:
            body_bytes = str(body)[:64 * 1024].encode("utf-8", "replace")
        # Identity is the whole request, not just the URL: the same URL
        # with different headers or body is a different probe (a UA-gate
        # challenge depends on exactly that distinction).
        key = (
            method,
            url,
            tuple(sorted(headers.items())),
            (body_bytes or b"")[:256],
        )
        if key in self._seen_requests:
            # Re-sending what we already saw wastes a turn; say so in the
            # observation the model reads next, and spend no request.
            return (
                "note",
                f"{method} {url} with those exact headers was already "
                "requested earlier — the result has not changed; change "
                "the path, parameters, headers or method",
            )
        self._seen_requests.add(key)
        try:
            self.target.scope.check_http(url)
            response = self.target.http(
                TargetHTTPRequest(
                    url=url, method=method, headers=headers, body=body_bytes
                )
            )
        except ScopeViolation as exc:
            return ("note", f"scope refused {method} {url}: {exc}")
        except TargetUnreachable as exc:
            return ("http", f"{method} {url} unreachable: {exc}")
        summary = (
            f"{method} {response.url.split('?')[0]} -> {response.status} "
            f"({len(response.body)}B{' truncated' if response.truncated else ''})"
        )
        detail = response.body.decode("utf-8", "replace")
        return ("http", summary, detail)

    def _tcp(self, action: dict) -> tuple[str, str, str]:
        host = action.get("host")
        port = action.get("port")
        if not isinstance(host, str) or not isinstance(port, int) or not (0 < port < 65536):
            return ("note", "refused tcp action: bad host or port")
        try:
            payload = bytes.fromhex(str(action.get("payload_hex", "") or ""))
        except ValueError:
            return ("note", "refused tcp action: payload_hex is not hex")
        try:
            self.target.scope.check_tcp(host, port)
            response = self.target.tcp(
                TargetTCPRequest(host=host, port=port, payload=payload[:4096])
            )
        except ScopeViolation as exc:
            return ("note", f"scope refused tcp {host}:{port}: {exc}")
        if not response.ok:
            return ("tcp", f"tcp {host}:{port} failed: {response.error}")
        summary = f"tcp {host}:{port} -> {len(response.data)}B"
        return ("tcp", summary, response.data.decode("utf-8", "replace"))

    def _read_attachment(self, action: dict) -> tuple[str, str, str]:
        name = action.get("name")
        allowed = {a.name for a in self.task.attachments}
        if not isinstance(name, str) or name not in allowed:
            return ("note", f"refused read_attachment {name!r}: not in task list")
        for entry in self.task.attachments:
            if entry.name == name and entry.path:
                path = Path(entry.path)
                if not path.is_file():
                    return ("note", f"attachment {name} not on disk at {path}")
                data = path.read_bytes()[:16 * 1024]
                summary = f"attachment {name} ({len(data)}B read)"
                return ("attachment", summary, data.decode("utf-8", "replace"))
        return (
            "note",
            f"attachment {name} was not auto-copied; downloading is the "
            "coordinator's job, not a model action",
        )


def _sanitize_url(url: str) -> str:
    """Percent-encode characters a browser would encode.

    Models routinely emit URLs with spaces (e.g. ``?cmd=cat /flag``);
    rejecting them wastes a turn the model already spent — encoding them
    is what any browser does, so we do too.
    """
    from urllib.parse import quote

    return quote(url, safe=":/?&=%+#@,;!*'()[]$-._~")


def _answer_candidate(action: dict) -> dict | None:
    question_id = action.get("question_id")
    value = action.get("value")
    try:
        confidence = float(action.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = action.get("evidence")
    if (
        not isinstance(question_id, str)
        or not isinstance(value, (str, list))
        or not value
        or confidence < MIN_ANSWER_CONFIDENCE
    ):
        return None
    cited = [e for e in (evidence or []) if isinstance(e, str)][:8] if isinstance(evidence, list) else []
    return {
        "question_id": question_id,
        "value": value,
        "confidence": confidence,
        "evidence_ids": tuple(cited),
    }


def _challenge_block(task: TaskSnapshot) -> str:
    lines = [f"id: {task.challenge.id}", f"name: {task.challenge.name}"]
    if task.challenge.description:
        lines.append(task.challenge.description[:2000])
    for rule in task.rules_of_engagement:
        lines.append(f"rule: {rule}")
    return "\n".join(lines)


def _endpoints_block(task: TaskSnapshot) -> str:
    if not task.target_endpoints:
        return "(none — this challenge has no live target)"
    return "\n".join(
        f"- {e.type} {e.url}" + (f" ({e.label})" if e.label else "")
        for e in task.target_endpoints
    )


def _attachments_block(task: TaskSnapshot) -> str:
    if not task.attachments:
        return "(none)"
    return "\n".join(
        f"- {a.name} ({a.size or '?'}B"
        + (", on disk" if a.path else ", needs download")
        + ")"
        for a in task.attachments
    )
