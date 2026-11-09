"""The answer ledger: every candidate's life from proposal to verdict.

One place decides what has been tried, what may be tried and what may
never be retried. The rules that matter:

- a value already submitted for a question is never submitted again,
  whatever happened to it;
- a submission whose outcome is unknown (request sent, verdict never
  arrived) is quarantined — no automatic resubmission, and it is kept
  out of result.json's fallback answers, which would submit it again;
- only candidates a round actually validated but never spent go into
  the fallback: handing over an already-judged value would burn an
  attempt the platform already counted.

State lives beside the run's other scratch data (``.sapper/answers.json``)
so the next round in this working directory starts from the truth.
"""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .client import CtfyApiError, CtfyClient, SubmissionRecord, UncertainSubmission
from .schema import TaskSnapshot

STATE_SCHEMA = 1

PROPOSED = "proposed"
VALIDATED = "validated"
SUBMITTING = "submitting"
CORRECT = "correct"
WRONG = "wrong"
REJECTED = "rejected"
UNKNOWN = "unknown"
CLOSED = "closed"  # question exhausted: correct, or no attempts left


class LedgerError(Exception):
    pass


@dataclass(frozen=True)
class AnswerCandidate:
    question_id: str
    value: str | list[str]
    confidence: float = 0.0
    evidence_ids: tuple[str, ...] = ()
    source_turn: int = 0


@dataclass
class LedgerEntry:
    question_id: str
    value: str | list[str]
    confidence: float
    status: str = PROPOSED
    attempts: int = 0
    last_error: str | None = None
    record: dict | None = None  # last platform response, scrubbed of nothing: it never held secrets

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "LedgerEntry":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class AnswerLedger:
    """Propose, validate, submit, remember — for this challenge only."""

    def __init__(self, client: CtfyClient, task: TaskSnapshot, state_path: Path):
        self._client = client
        self._task = task
        self._path = state_path
        self._entries: dict[str, LedgerEntry] = {}  # entry id → entry
        self._next_id = 1
        self._closed_questions: set[str] = set()
        self._load()

    # -- proposal and validation -------------------------------------------

    def propose(self, candidate: AnswerCandidate) -> str:
        """Record a candidate. Returns its id; raises LedgerError if it
        cannot ever be spent (invisible/duplicate question or value)."""
        question = self._visible_question(candidate.question_id)
        if question is None:
            raise LedgerError(
                f"question {candidate.question_id!r} is not in this round's visible set"
            )
        if candidate.question_id in self._closed_questions:
            raise LedgerError(
                f"question {candidate.question_id!r} is closed (solved or out of attempts)"
            )
        value = candidate.value
        if not isinstance(value, (str, list)) or not value or (
            isinstance(value, list) and not all(isinstance(v, str) for v in value)
        ):
            raise LedgerError("answer value must be a non-empty string or list of strings")
        if not isinstance(value, list) and not question.raw.get("multi_select"):
            pass  # single string is the normal shape
        if self._value_already_tried(candidate.question_id, value):
            raise LedgerError(
                f"value already spent on {candidate.question_id!r} — never resubmit"
            )
        entry = LedgerEntry(
            question_id=candidate.question_id,
            value=value,
            confidence=float(candidate.confidence),
        )
        entry_id = f"cand-{self._next_id}"
        self._next_id += 1
        self._entries[entry_id] = entry
        self._save()
        return entry_id

    def validated(self, entry_id: str) -> bool:
        return entry_id in self._entries and self._entries[entry_id].status == VALIDATED

    def mark_validated(self, entry_id: str) -> None:
        if entry_id not in self._entries:
            raise LedgerError(f"unknown candidate {entry_id}")
        if self._entries[entry_id].status != PROPOSED:
            raise LedgerError(
                f"candidate {entry_id} already {self._entries[entry_id].status}"
            )
        self._entries[entry_id].status = VALIDATED
        self._save()

    # -- submission ---------------------------------------------------------

    def submit(self, entry_id: str) -> LedgerEntry:
        """Spend a validated candidate. Exactly one POST; an unreadable
        outcome lands the entry in ``unknown`` and stays there."""
        entry = self._require(entry_id, expected=VALIDATED)
        entry.status = SUBMITTING
        self._save()
        try:
            record = self._post(entry)
        except UncertainSubmission as exc:
            entry.status = UNKNOWN
            entry.last_error = str(exc)
            self._save()
            raise LedgerQuarantined(entry_id, str(exc)) from exc
        except CtfyApiError as exc:
            entry.status = REJECTED
            entry.attempts += 1
            entry.last_error = f"{exc.code or exc.status}: {exc}"
            if exc.paused:
                # A wait, not a verdict: put the value back, unspent.
                entry.status = VALIDATED
                entry.attempts -= 1
                self._save()
                raise
            self._save()
            raise
        entry.attempts += 1
        entry.record = record.raw
        if record.correct:
            entry.status = CORRECT
            self._closed_questions.add(entry.question_id)
        else:
            entry.status = WRONG
            remaining = record.attempts_remaining
            if remaining is not None and remaining <= 0:
                self._closed_questions.add(entry.question_id)
        self._save()
        return entry

    def question_closed(self, question_id: str) -> bool:
        return question_id in self._closed_questions

    # -- round bookkeeping ---------------------------------------------------

    def reconcile(self, task: TaskSnapshot) -> None:
        """Adopt a newer snapshot: forget questions that vanished, keep
        the rest. Called once per round before work starts."""
        self._task = task
        visible = {q.id for q in task.questions}
        for entry in self._entries.values():
            if entry.question_id not in visible and entry.status not in (
                CORRECT,
                WRONG,
                UNKNOWN,
                CLOSED,
            ):
                # A gated question disappeared from view; its history stays
                # but nothing new may be proposed for it while invisible.
                continue
        self._save()

    def fallback_answers(self) -> dict[str, str | list[str]]:
        """Validated-but-unspent candidates, best per question, for
        result.json. Quarantined and judged values are excluded."""
        best: dict[str, LedgerEntry] = {}
        for entry in self._entries.values():
            if entry.status != VALIDATED:
                continue
            if entry.question_id in self._closed_questions:
                continue
            current = best.get(entry.question_id)
            if current is None or entry.confidence > current.confidence:
                best[entry.question_id] = entry
        return {qid: entry.value for qid, entry in best.items()}

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts

    # -- internals -----------------------------------------------------------

    def _post(self, entry: LedgerEntry) -> SubmissionRecord:
        if self._task.is_qa:
            return self._client.submit_qa_answer(
                self._task.challenge.id,
                self._task.competition_id,
                entry.question_id,
                entry.value,
            )
        return self._client.submit_instance_answer(
            self._task.target_instance or "",
            entry.question_id,
            entry.value,
        )

    def _visible_question(self, question_id: str):
        for question in self._task.questions:
            if question.id == question_id:
                return question
        return None

    def _value_already_tried(self, question_id: str, value) -> bool:
        if isinstance(value, list):
            value = tuple(value)
        for entry in self._entries.values():
            if entry.question_id != question_id:
                continue
            other = entry.value
            if isinstance(other, list):
                other = tuple(other)
            if other == value and entry.status != PROPOSED:
                return True
            if other == value and entry.status == PROPOSED:
                return True  # an identical unspent proposal exists; no need for two
        return False

    def _require(self, entry_id: str, *, expected: str) -> LedgerEntry:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise LedgerError(f"unknown candidate {entry_id}")
        if entry.status != expected:
            raise LedgerError(
                f"candidate {entry_id} is {entry.status}, expected {expected}"
            )
        return entry

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            return
        if data.get("challenge_id") not in (None, self._task.challenge.id):
            return  # a different challenge's ledger is not ours to resume
        for entry_id, raw in (data.get("entries") or {}).items():
            if isinstance(raw, dict):
                self._entries[entry_id] = LedgerEntry.from_json(raw)
        self._closed_questions = set(data.get("closed_questions") or [])
        self._next_id = int(data.get("next_id") or 1)
        # Anything mid-flight from a crashed round is unknowable now.
        for entry in self._entries.values():
            if entry.status == SUBMITTING:
                entry.status = UNKNOWN
                entry.last_error = "round ended while submitting"

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": STATE_SCHEMA,
            "challenge_id": self._task.challenge.id,
            "next_id": self._next_id,
            "closed_questions": sorted(self._closed_questions),
            "entries": {eid: e.to_json() for eid, e in self._entries.items()},
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=str(self._path.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)


class LedgerQuarantined(LedgerError):
    """The submission's fate is unknown; the value is quarantined."""
