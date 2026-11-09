"""End-to-end entrypoint runs in a subprocess, exactly as the platform
starts us: an absolute script path, a working directory of the platform's
choosing, and nothing else shared."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .fixtures import instance_task, qa_task, write_work_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "app" / "entrypoint.py"


def run_agent(work_dir: Path, env_over: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CTFY_", "LLM_", "OPENAI_", "ANTHROPIC_"))}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update({k: v for k, v in env_over.items() if v is not None})
    env = {k: v for k, v in env.items() if v is not None}
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        cwd=str(cwd or Path(tempfile.mkdtemp())),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class DryRunTest(unittest.TestCase):
    def test_env_flag_writes_v3_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_work_dir(Path(tmp), instance_task(dry_run=True))
            proc = run_agent(work, {"CTFY_EVAL_DRY_RUN": "1", "CTFY_WORK_DIR": str(work)})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads((work / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["schema_version"], 3)
            self.assertEqual(result["stop_reason"], "dry_run")
            self.assertEqual(result["solved"], [])
            self.assertFalse(result["fully_solved"])

    def test_task_flag_alone_is_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_work_dir(Path(tmp), instance_task(dry_run=True))
            proc = run_agent(work, {"CTFY_WORK_DIR": str(work)})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads((work / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["stop_reason"], "dry_run")

    def test_no_credentials_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_work_dir(Path(tmp), instance_task(dry_run=True))
            env = {"CTFY_EVAL_DRY_RUN": "1", "CTFY_WORK_DIR": str(work)}
            proc = run_agent(work, env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # Nothing credential-shaped was written into the collected dir.
            collected = "".join(
                p.read_text(encoding="utf-8", errors="replace")
                for p in work.rglob("*")
                if p.is_file()
            )
            self.assertNotIn("Bearer ", collected)


class HostedShapeTest(unittest.TestCase):
    HOSTED_ENV = {
        "CTFY_WORK_DIR": "{work}",
        "CTFY_PLATFORM_URL": "https://ctfy.example",
        "CTFY_TOKEN": "run-token",
        "CTFY_EVAL_CHALLENGE_ID": "CTFARENA-001",
        "CTFY_EVAL_COMPETITION_ID": "tpctf2026",
        "CTFY_EVAL_TARGET_INSTANCE": "3f9c0f12",
        "LLM_FLAVOR": "openai",
        "LLM_BASE_URL": "https://llm.ctfy.example/v1",
        "LLM_API_KEY": "funded-handle",
        "LLM_MODEL": "gpt-test",
    }

    def hosted(self, work: Path) -> subprocess.CompletedProcess:
        env = {k: v.format(work=work) for k, v in self.HOSTED_ENV.items()}
        return run_agent(work, env)

    def test_skeleton_round_completes_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_work_dir(Path(tmp), instance_task())
            proc = self.hosted(work)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads((work / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["schema_version"], 3)
            self.assertEqual(result["turns"], 0)
            self.assertEqual(result["answers"], {})

    def test_config_error_writes_result_and_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_work_dir(Path(tmp), instance_task())
            env = {k: v.format(work=work) for k, v in self.HOSTED_ENV.items()}
            env.pop("CTFY_TOKEN")
            proc = run_agent(work, env)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            result = json.loads((work / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["stop_reason"], "config_error")
            self.assertNotIn("run-token", json.dumps(result))

    def test_bad_task_version_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_work_dir(Path(tmp), instance_task(schema_version=4))
            proc = self.hosted(work)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            result = json.loads((work / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["stop_reason"], "config_error")

    def test_unimplemented_run_mode_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = write_work_dir(Path(tmp), instance_task(run_mode="awd"))
            env = {k: v.format(work=work) for k, v in self.HOSTED_ENV.items()}
            env["CTFY_EVAL_TARGET_INSTANCE"] = None
            proc = run_agent(work, env)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
