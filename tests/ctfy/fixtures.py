"""Shared task.json fixtures shaped after CTFY-0001 v3 §2."""
from __future__ import annotations

import json
from pathlib import Path


def instance_task(**over) -> dict:
    task = {
        "schema_version": 3,
        "run_mode": "instance",
        "challenge": {
            "id": "CTFARENA-001",
            "name": "DocuVault",
            "description": "A document vault with a leaky API.",
        },
        "competition_id": "tpctf2026",
        "questions": [
            {"id": "flag", "prompt": "The vault master secret"},
            {"id": "admin_pass", "prompt": "The admin password"},
        ],
        "gated_questions": 1,
        "scope": [],
        "rules_of_engagement": [],
        "deliverable": "an answer per question, submitted to POST /api/v1/submissions",
        "platform_url": "https://ctfy.example",
        "max_turns": 40,
        "result": {"path": "/ctfy/work/result.json", "stdout_fallback": False},
        "target_instance": "3f9c0f12",
        "target_endpoints": [
            {"label": "web", "type": "http", "url": "http://3f9c.ctfy.example:80"}
        ],
        "attachments": [
            {"name": "crackme", "size": 14120, "path": "/ctfy/work/attachments/crackme"},
            {"name": "dump.bin", "size": 9000000, "path": None},
        ],
    }
    task.update(over)
    return task


def qa_task(**over) -> dict:
    task = {
        "schema_version": 3,
        "run_mode": "qa",
        "challenge": {
            "id": "QA-001",
            "name": "Which cipher",
            "description": "Pick the stream cipher.",
        },
        "competition_id": "tpctf2026",
        "questions": [
            {
                "id": "answer",
                "prompt": "Which of these is a stream cipher?",
                "choices": ["AES", "RC4", "RSA", "DSA"],
            }
        ],
        "gated_questions": 0,
        "scope": [],
        "rules_of_engagement": [],
        "platform_url": "https://ctfy.example",
        "max_turns": None,
        "target_instance": None,
        "target_endpoints": [],
        "attachments": [],
    }
    task.update(over)
    return task


def write_work_dir(root: Path, task: dict, readme: str = "# challenge\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task.json").write_text(json.dumps(task), encoding="utf-8")
    (root / "README.md").write_text(readme, encoding="utf-8")
    return root
