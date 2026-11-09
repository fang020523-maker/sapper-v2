"""Narrow REST client for this run's own challenge (CTFY-0001 v3 §2).

Only the calls a dispatched agent may make: read the current challenge
and instance, fetch this run's attachments, submit an answer. Start,
stop, reset, list-everything and any other challenge are somebody
else's business — the token has no scope for them, and this client has
no code path for them either.

Transport rules that the protocol makes ours to keep:
- GETs retry, bounded, on 429/5xx/connection errors, honouring
  Retry-After; 401/403/404 never retry and never widen anything.
- POSTs never retry here. A submission whose response did not arrive
  is ``UncertainSubmission`` — it may have been recorded, and the
  answer ledger decides what that means. Nothing in this module ever
  sends the same submission twice.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GET_TIMEOUT_S = 30
SUBMIT_TIMEOUT_S = 120
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 3


class CtfyApiError(Exception):
    """A platform call failed with a status we understand."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        payload: dict | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload or {}

    @property
    def paused(self) -> bool:
        """403 competition_paused — a wait, not a failure (§4)."""
        return self.status == 403 and self.code == "competition_paused"

    @property
    def retry_after_s(self) -> float | None:
        if self.payload.get("retry_after_s") is not None:
            try:
                return float(self.payload["retry_after_s"])
            except (TypeError, ValueError):
                return None
        return None


class UncertainSubmission(Exception):
    """A POST may have reached the platform but its answer did not come back.

    Carries nothing about the answer's value; the ledger treats this as
    ``unknown`` and must not submit it again automatically.
    """


@dataclass(frozen=True)
class SubmissionRecord:
    question_id: str
    correct: bool
    challenge_fully_solved: bool
    attempts_remaining: int | None
    raw: dict


class CtfyClient:
    def __init__(self, platform_url: str, token: str, *, get_timeout_s: int = GET_TIMEOUT_S, submit_timeout_s: int = SUBMIT_TIMEOUT_S):
        self._base = platform_url.rstrip("/")
        self._token = token
        self._get_timeout_s = get_timeout_s
        self._submit_timeout_s = submit_timeout_s
        # No ambient proxy, same reasoning as the model gateway.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self) -> str:
        return f"CtfyClient(platform_url={self._base!r}, token=<set>)"

    # -- reads ---------------------------------------------------------------

    def get_challenge(self, challenge_id: str, competition_id: str = "") -> dict:
        query = {"competition_id": competition_id} if competition_id else {}
        return self._request_json("GET", f"/challenges/{_enc(challenge_id)}", query=query)

    def get_instance(self, instance_id: str) -> dict:
        return self._request_json("GET", f"/instances/{_enc(instance_id)}")

    def me(self) -> dict:
        return self._request_json("GET", "/me")

    def download_attachment(self, instance_id: str, name: str, dest: Path) -> Path:
        """Fetch one of this run's listed attachments by its exact name."""
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise CtfyApiError(f"refusing attachment name {name!r}")
        url = f"{self._base}/api/v1/instances/{_enc(instance_id)}/attachments/{_enc(name)}"
        data = self._request_bytes("GET", url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest

    # -- submissions ---------------------------------------------------------

    def submit_instance_answer(
        self, instance_id: str, question_id: str, answer: str | list[str]
    ) -> SubmissionRecord:
        body = {
            "instance_id": instance_id,
            "question_id": question_id,
            "answer": answer,
        }
        payload = self._post_submission("/submissions", body)
        return _record(payload, question_id)

    def submit_qa_answer(
        self, challenge_id: str, competition_id: str, question_id: str, answer: str | list[str]
    ) -> SubmissionRecord:
        body = {
            "challenge_id": challenge_id,
            "competition_id": competition_id,
            "question_id": question_id,
            "answer": answer,
        }
        payload = self._post_submission("/qa/submissions", body)
        return _record(payload, question_id)

    # -- transport -----------------------------------------------------------

    def _post_submission(self, path: str, body: dict) -> dict:
        """One POST, one attempt. Retrying here could double-submit."""
        url = f"{self._base}/api/v1{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self._submit_timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _api_error(exc, context=path) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Sent-or-not is unknowable from here. Say exactly that.
            raise UncertainSubmission(
                f"{path} outcome unknown after {exc.__class__.__name__}; "
                "treat as unknown — do not resubmit automatically"
            ) from exc
        except json.JSONDecodeError as exc:
            raise UncertainSubmission(f"{path} returned an unparseable body") from exc

    def _request_json(self, method: str, path: str, *, query: dict | None = None) -> dict:
        url = f"{self._base}/api/v1{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = self._request_bytes(method, url)
        if not data:
            return {}
        return json.loads(data.decode("utf-8"))

    def _request_bytes(self, method: str, url: str) -> bytes:
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
                method=method,
            )
            try:
                with self._opener.open(req, timeout=self._get_timeout_s) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                    time.sleep(_retry_delay(exc, attempt))
                    continue
                raise _api_error(exc, context=url) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise CtfyApiError(
                    f"platform unreachable: {exc.__class__.__name__}", status=None
                ) from exc


def _record(payload: dict, question_id: str) -> SubmissionRecord:
    return SubmissionRecord(
        question_id=str(payload.get("question_id", question_id)),
        correct=bool(payload.get("correct")),
        challenge_fully_solved=bool(payload.get("challenge_fully_solved")),
        attempts_remaining=payload.get("attempts_remaining"),
        raw=payload,
    )


def _api_error(exc: urllib.error.HTTPError, *, context: str) -> CtfyApiError:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — the body is best-effort context
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("code") or payload.get("error") if isinstance(payload.get("code") or payload.get("error"), str) else None
    message = payload.get("message") or payload.get("detail") or exc.reason or ""
    return CtfyApiError(
        f"{context} answered {exc.code}: {message}",
        status=exc.code,
        code=code,
        payload=payload,
    )


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    header = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return min(float(header), 30.0) if header else min(2.0 ** attempt, 8.0)
    except ValueError:
        return min(2.0 ** attempt, 8.0)


def _enc(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")
