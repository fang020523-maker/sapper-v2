"""Typed views over ``task.json`` (CTFY-0001 v3) and the result we return.

The task file is a snapshot: unknown keys are preserved verbatim so a
compatible additive change upstream cannot lose data here, while the keys
this agent depends on are validated strictly — a wrong type or an unknown
schema version fails loudly instead of being misread.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

TASK_SCHEMA_VERSION = 3
RESULT_SCHEMA_VERSION = 3

# Run modes this build can execute. The protocol defines five; the ones we
# do not implement must be refused explicitly, never guessed at.
SUPPORTED_RUN_MODES = ("instance", "qa")
KNOWN_RUN_MODES = ("instance", "qa", "defence", "awd", "engagement")

ENDPOINT_TYPES = ("http", "https", "tcp", "udp", "ssh")

QUESTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class TaskError(Exception):
    """task.json cannot be honoured as written."""


class UnsupportedSchemaVersion(TaskError):
    def __init__(self, found: object):
        super().__init__(
            f"unsupported task schema_version {found!r}; "
            f"this agent speaks v{TASK_SCHEMA_VERSION} — refusing to guess"
        )


class UnsupportedRunMode(TaskError):
    def __init__(self, found: object):
        super().__init__(
            f"run_mode {found!r} is not implemented by this agent "
            f"(supported: {', '.join(SUPPORTED_RUN_MODES)})"
        )


class MalformedTask(TaskError):
    pass


@dataclass(frozen=True)
class QuestionView:
    id: str
    prompt: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Endpoint:
    label: str
    type: str
    url: str


@dataclass(frozen=True)
class Attachment:
    name: str
    size: int | None
    path: str | None  # None: not copied — download via instance attachments API


@dataclass(frozen=True)
class ChallengeView:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class TaskSnapshot:
    schema_version: int
    run_mode: str
    dry_run: bool
    challenge: ChallengeView
    competition_id: str
    questions: tuple[QuestionView, ...]
    gated_questions: int
    scope: tuple[str, ...]
    rules_of_engagement: tuple[str, ...]
    target_instance: str | None
    target_endpoints: tuple[Endpoint, ...]
    attachments: tuple[Attachment, ...]
    max_turns: int | None
    platform_url: str
    raw: dict = field(default=False, repr=False)
    raw_readme_sha256: str | None = None

    @property
    def is_qa(self) -> bool:
        return self.run_mode == "qa"


# --- task loading -----------------------------------------------------------


def load_task(work_dir: Path) -> TaskSnapshot:
    """Parse and validate ``work_dir/task.json``. Raises TaskError."""
    path = work_dir / "task.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MalformedTask(f"task.json not found at {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise MalformedTask(f"cannot read task.json: {exc}") from None
    if not isinstance(data, dict):
        raise MalformedTask("task.json is not a JSON object")
    snapshot = build_snapshot(data)
    digest = readme_sha256(work_dir)
    if digest is not None:
        snapshot = dataclasses.replace(snapshot, raw_readme_sha256=digest)
    return snapshot


def build_snapshot(data: dict) -> TaskSnapshot:
    version = data.get("schema_version")
    if version != TASK_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(version)

    run_mode = data.get("run_mode")
    if run_mode not in KNOWN_RUN_MODES:
        raise MalformedTask(f"unknown run_mode {run_mode!r}")
    if run_mode not in SUPPORTED_RUN_MODES:
        raise UnsupportedRunMode(run_mode)

    challenge_raw = data.get("challenge")
    if (
        not isinstance(challenge_raw, dict)
        or not isinstance(challenge_raw.get("id"), str)
        or not challenge_raw["id"]
    ):
        raise MalformedTask("challenge.id missing or empty")

    questions = tuple(_question(q, i) for i, q in enumerate(_list(data, "questions")))

    gated = data.get("gated_questions", 0)
    if not isinstance(gated, int) or isinstance(gated, bool) or gated < 0:
        raise MalformedTask(
            f"gated_questions must be a non-negative int, got {gated!r}"
        )

    target_instance = data.get("target_instance")
    if run_mode == "instance" and (
        not isinstance(target_instance, str) or not target_instance
    ):
        raise MalformedTask("instance run requires a non-empty target_instance")
    if run_mode == "qa" and target_instance is not None:
        raise MalformedTask("qa run must not carry a target_instance")

    max_turns = data.get("max_turns")
    if max_turns is not None and (
        not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns <= 0
    ):
        raise MalformedTask(
            f"max_turns must be null or a positive int, got {max_turns!r}"
        )

    endpoints = tuple(_endpoint(e) for e in _list(data, "target_endpoints"))
    attachments = tuple(_attachment(a) for a in _list(data, "attachments"))

    return TaskSnapshot(
        schema_version=version,
        run_mode=run_mode,
        dry_run=bool(data.get("dry_run", False)),
        challenge=ChallengeView(
            id=challenge_raw["id"],
            name=str(challenge_raw.get("name", "")),
            description=str(challenge_raw.get("description", "")),
        ),
        competition_id=str(data.get("competition_id") or ""),
        questions=questions,
        gated_questions=gated,
        scope=tuple(str(s) for s in _list(data, "scope")),
        rules_of_engagement=tuple(
            str(r) for r in _list(data, "rules_of_engagement")
        ),
        target_instance=target_instance,
        target_endpoints=endpoints,
        attachments=attachments,
        max_turns=max_turns,
        platform_url=str(data.get("platform_url") or ""),
        raw=data,
        raw_readme_sha256=None,
    )


def readme_sha256(work_dir: Path) -> str | None:
    """Digest of this round's README.md, or None when absent."""
    try:
        return hashlib.sha256((work_dir / "README.md").read_bytes()).hexdigest()
    except OSError:
        return None


def _list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise MalformedTask(f"{key} must be a list, got {type(value).__name__}")
    return value


def _question(raw: object, index: int) -> QuestionView:
    if not isinstance(raw, dict):
        raise MalformedTask(f"questions[{index}] is not an object")
    qid = raw.get("id")
    if not isinstance(qid, str) or not qid:
        raise MalformedTask(f"questions[{index}].id missing or empty")
    return QuestionView(
        id=qid,
        prompt=str(raw.get("prompt", "")),
        raw=dict(raw),
    )


def _endpoint(raw: object) -> Endpoint:
    if not isinstance(raw, dict):
        raise MalformedTask("target_endpoints[] entry is not an object")
    etype = raw.get("type")
    if etype not in ENDPOINT_TYPES:
        raise MalformedTask(f"target_endpoints[].type must be one of {ENDPOINT_TYPES}")
    return Endpoint(
        label=str(raw.get("label", "")),
        type=etype,
        url=str(raw.get("url", "")),
    )


def _attachment(raw: object) -> Attachment:
    if not isinstance(raw, dict):
        raise MalformedTask("attachments[] entry is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise MalformedTask("attachments[].name missing or empty")
    size = raw.get("size")
    path = raw.get("path")
    return Attachment(
        name=name,
        size=size if isinstance(size, int) else None,
        path=path if isinstance(path, str) else None,
    )
