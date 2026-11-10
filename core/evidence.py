"""Evidence store: what the agent saw, kept short, honest and reusable.

Exploration produces observations (HTTP responses, socket banners,
attachment excerpts). Each becomes one evidence record with a stable id
the model can cite in an answer's justification. Everything is capped —
a hostile target that answers with a megabyte of junk must not eat the
prompt budget or the working directory — and nothing secret is ever
recorded, because nothing secret is reachable from this module.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

STATE_SCHEMA = 1
MAX_RECORD_BYTES = 8 * 1024
MAX_SUMMARY_BYTES = 2 * 1024
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")


@dataclass
class EvidenceRecord:
    evidence_id: str
    kind: str          # http | tcp | attachment | note
    summary: str
    detail: str = field(default="", repr=False)

    def to_json(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "summary": self.summary,
            "detail": self.detail,
        }


class EvidenceStore:
    def __init__(self, state_path: Path, *, max_records: int = 200):
        self._path = state_path
        self._max_records = max_records
        self._records: list[EvidenceRecord] = []
        self._load()

    def record(self, kind: str, summary: str, detail: str = "") -> str:
        evidence_id = f"EV-{len(self._records) + 1:03d}"
        record = EvidenceRecord(
            evidence_id=evidence_id,
            kind=kind[:16],
            summary=_cap(_scrub(summary), MAX_SUMMARY_BYTES),
            detail=_cap(_scrub(detail), MAX_RECORD_BYTES),
        )
        self._records.append(record)
        if len(self._records) > self._max_records:
            self._records = self._records[-self._max_records:]
        self._save()
        return evidence_id

    def render_for_prompt(self, limit: int = 40) -> str:
        """Compact listing, newest last, detail omitted; ids citable."""
        if not self._records:
            return "(no observations yet)"
        lines = []
        for record in self._records[-limit:]:
            first_line = record.summary.splitlines()[0] if record.summary else ""
            lines.append(f"- {record.evidence_id} [{record.kind}] {first_line}")
        return "\n".join(lines)

    def detail_of(self, evidence_id: str) -> str | None:
        for record in self._records:
            if record.evidence_id == evidence_id:
                return record.detail
        return None

    def __len__(self) -> int:
        return len(self._records)

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            return
        for raw in data.get("records") or []:
            if isinstance(raw, dict) and raw.get("evidence_id"):
                self._records.append(
                    EvidenceRecord(
                        evidence_id=str(raw["evidence_id"]),
                        kind=str(raw.get("kind", "note")),
                        summary=str(raw.get("summary", "")),
                        detail=str(raw.get("detail", "")),
                    )
                )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema": STATE_SCHEMA,
            "records": [r.to_json() for r in self._records],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=str(self._path.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        finally:
            tmp.unlink(missing_ok=True)


def _scrub(text: str) -> str:
    return CONTROL_CHARS.sub(" ", text) if text else ""


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[truncated {len(text) - limit} chars]"
