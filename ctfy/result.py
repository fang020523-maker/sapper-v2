"""The result.json we hand back (CTFY-0001 v3 §3) and how it is written.

The file is the run's receipt: the platform reads answers it should submit
for us, the counters that make runs comparable, and our own account of the
work. It is rewritten fresh every round in the same working directory, so
the write itself must never leave a torn or half-empty file behind —
temp file, fsync, atomic replace.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .schema import RESULT_SCHEMA_VERSION


@dataclass
class RunSummary:
    """Everything one round wants to say. Counters accumulate per round."""

    answers: dict[str, str | list[str]] = field(default_factory=dict)
    solved: list[str] = field(default_factory=list)
    fully_solved: bool = False
    turns: int = 0
    tool_calls_made: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "done"
    writeup: str = ""
    # A short, scrubbed error note for crashed runs. Never a full traceback:
    # /ctfy/work is collected and read by a human, but a traceback can carry
    # paths, headers or other context better left out of the archive.
    error: str | None = None

    @classmethod
    def dry_run(cls) -> "RunSummary":
        return cls(stop_reason="dry_run")

    def with_safe_error(self, exc: BaseException) -> "RunSummary":
        self.error = _scrub_error(exc)
        return self

    def to_json(self) -> dict:
        out: dict = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "answers": self.answers,
            "solved": list(self.solved),
            "fully_solved": self.fully_solved,
            "turns": self.turns,
            "tool_calls_made": self.tool_calls_made,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "stop_reason": self.stop_reason,
            "writeup": self.writeup,
        }
        if self.error is not None:
            out["error"] = self.error
        return out


_SECRET_MARKERS = ("authorization", "bearer", "api_key", "apikey", "token",
                   "secret", "password")


def _scrub_error(exc: BaseException) -> str:
    """One line, capped length, no secret-looking content."""
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    if len(text) > 300:
        text = text[:300] + "…"
    lowered = text.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        text = exc.__class__.__name__ + " (details omitted: secret-looking content)"
    return text


class ResultWriter:
    """Atomic writer for ``<work_dir>/result.json``.

    Every round rebuilds the file from scratch; the platform removes the
    previous round's copy before starting us again.
    """

    def __init__(self, path: Path):
        self.path = path

    def write(self, summary: RunSummary) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(summary.to_json(), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
