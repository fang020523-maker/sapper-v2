#!/usr/bin/env python3
"""Scan a working directory (and optionally the repo) for leaked secrets.

The platform collects /ctfy/work verbatim; a credential written there is
handed to whoever operates the deployment. This script is the last gate
before that: it pattern-matches every file the round produced plus the
image's build inputs, and exits non-zero on the first hit it can stand
behind. It never reads the environment beyond its arguments, so running
it cannot itself leak anything.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Ordered (name, regex). Match on plausible full credentials, not on the
# bare words, or every comment saying "api key" would trip it.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bearer-header", re.compile(r"Authorization\s*[:=]\s*Bearer\s+\S{8,}", re.I)),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic-token-assign", re.compile(
        r"\b(token|api[_-]?key|secret)\b\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{24,}['\"]?", re.I)),
]

# Test fixtures use these fake values; they are fine in tests/, fatal in
# produced artifacts.
TEST_VALUES = ("round-token", "funded-handle", "run-token")

TEXT_SUFFIXES = {
    ".json", ".md", ".txt", ".log", ".py", ".toml", ".yaml", ".yml",
    ".env", ".cfg", ".ini", ".sh", "",
}


def scan_file(path: Path, in_repo_tests: bool, *, is_scanner_self: bool = False) -> list[str]:
    findings = []
    if is_scanner_self:
        return findings  # this file defines the patterns and fake values
    try:
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 5_000_000:
            return findings
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for name, pattern in PATTERNS:
        if in_repo_tests:
            break  # test fixtures legitimately carry fake leak samples
        for match in pattern.finditer(text):
            snippet = text[max(0, match.start() - 20):match.end() + 10]
            findings.append(f"{path}: {name}: …{' '.join(snippet.split())[:120]}…")
    if not in_repo_tests:
        for value in TEST_VALUES:
            if value in text:
                findings.append(f"{path}: test credential {value!r} leaked into an artifact")
    return findings


def scan(root: Path, *, is_repo: bool) -> list[str]:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if is_repo and any(
            part in (".git", ".venv", "__pycache__", "node_modules") for part in path.parts
        ):
            continue
        # In the repo, tests legitimately carry fake tokens; everywhere
        # else (artifacts, work dirs) they must not appear.
        in_tests = is_repo and (rel.startswith("tests/") or rel.startswith("docs/"))
        findings.extend(
            scan_file(
                path,
                in_repo_tests=in_tests,
                is_scanner_self=is_repo and rel == "scripts/check_secrets.py",
            )
        )
    return findings


def main(argv: list[str]) -> int:
    targets = argv[1:] or ["."]
    all_findings = []
    for target in targets:
        root = Path(target)
        if not root.exists():
            print(f"scan: {root} does not exist", file=sys.stderr)
            return 2
        all_findings.extend(scan(root, is_repo=(root / ".git").exists() or (root / ".gitignore").exists()))
    if all_findings:
        print("SECRET FINDINGS:")
        for finding in all_findings:
            print(f"  {finding}")
        return 1
    print(f"clean: no secret patterns in {', '.join(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
