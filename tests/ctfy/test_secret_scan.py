"""The secret scanner must catch real leaks and stay quiet on clean runs."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "check_secrets.py"


def run_scanner(*targets: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCANNER), *targets],
        capture_output=True,
        text=True,
        timeout=60,
    )


class SecretScannerTest(unittest.TestCase):
    def test_clean_workdir_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "result.json").write_text(
                '{"schema_version": 3, "answers": {"flag": "FLAG{ok}"}}',
                encoding="utf-8",
            )
            (root / ".sapper").mkdir()
            (root / ".sapper" / "evidence.json").write_text(
                '{"records": [{"evidence_id": "EV-001", "kind": "http", '
                '"summary": "GET / -> 200 (12B)"}]}',
                encoding="utf-8",
            )
            proc = run_scanner(str(root))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_leaked_bearer_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.log").write_text(
                "retrying with Authorization: Bearer eyJhbGciOi.J9.x",
                encoding="utf-8",
            )
            proc = run_scanner(tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("bearer-header", proc.stdout)

    def test_leaked_openai_key_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text(
                '{"key": "sk-proj-0123456789abcdefghijklmnopqrst"}',
                encoding="utf-8",
            )
            proc = run_scanner(tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("openai-key", proc.stdout)

    def test_test_credential_in_artifact_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "evidence.json").write_text(
                '{"note": "used round-token here"}', encoding="utf-8"
            )
            proc = run_scanner(tmp)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("test credential", proc.stdout)

    def test_repo_scans_clean(self):
        proc = run_scanner(str(REPO_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
