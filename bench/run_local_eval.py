#!/usr/bin/env python3
"""Local benchmark runner: real challenges, real model, real agent.

For each challenge: start the target with a fresh flag, stand up the
fake platform, run the container entrypoint as a subprocess with the
LLM pointed at the configured gateway, then record whether the flag
was actually submitted — plus the turns, tokens and time it cost.

Config (bench/config.json, git-ignored):
  {"base_url": "...", "api_key": "...", "model": "...", "flavor": "openai"}
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO_ROOT = BENCH.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(BENCH / "challenges"))

from bench.fake_platform import FakePlatform  # noqa: E402

CHALLENGES = [
    # (module name, challenge id, README prose)
    ("t1_robots", "BENCH-T1",
     "Acme Notes — a tiny notes site. Get the flag."),
    ("t2_source", "BENCH-T2",
     "The Gate — a code gate nobody remembers the code for."),
    ("t3_header", "BENCH-T3",
     "Legacy Portal — old browsers only."),
    ("t4_sqli", "BENCH-T4",
     "Member Login — log in as admin."),
    ("t5_cmdi", "BENCH-T5",
     "Network Tools — a ping utility box."),
    ("t6_lfi", "BENCH-T6",
     "Mini CMS — three pages of nothing much."),
]

RUN_TIMEOUT_S = 600


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"missing {path} — create it with "
            '{"base_url","api_key","model","flavor"} (git-ignored)'
        )
    return json.loads(path.read_text(encoding="utf-8"))


def start_target(module: str, flag: str) -> subprocess.Popen:
    script = BENCH / "challenges" / f"{module}.py"
    port = 18000 + (secrets.randbelow(4000))
    proc = subprocess.Popen(
        [sys.executable, str(script), "--port", str(port), "--flag", flag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(BENCH / "challenges"),
    )
    # Cheap readiness wait: the servers bind instantly.
    time.sleep(0.6)
    if proc.poll() is not None:
        raise RuntimeError(f"{module} died on start")
    proc.port = port  # type: ignore[attr-defined]
    return proc


def make_workdir(root: Path, challenge_id: str, readme: str, target_url: str, flag: str) -> Path:
    work = root / challenge_id
    work.mkdir(parents=True)
    task = {
        "schema_version": 3,
        "run_mode": "instance",
        "challenge": {"id": challenge_id, "name": challenge_id, "description": readme},
        "competition_id": "localbench",
        "questions": [{"id": "flag", "prompt": "The flag hidden in this challenge"}],
        "gated_questions": 0,
        "scope": [target_url],
        "rules_of_engagement": ["Attack only the published endpoint."],
        "platform_url": "set-by-env",
        "max_turns": None,
        "target_instance": "bench-instance-1",
        "target_endpoints": [{"label": "web", "type": "http", "url": target_url}],
        "attachments": [],
    }
    (work / "task.json").write_text(json.dumps(task), encoding="utf-8")
    (work / "README.md").write_text(
        f"# {challenge_id}\n\n{readme}\n\nQuestion: the flag hidden in this "
        "challenge. Submit it as the answer.\n",
        encoding="utf-8",
    )
    return work


def run_one(module: str, cid: str, readme: str, config: dict, keep_dir: Path | None) -> dict:
    flag = "flag{" + secrets.token_hex(8) + "}"
    target = start_target(module, flag)
    platform = FakePlatform(cid, "The flag hidden in this challenge", flag)
    started = time.monotonic()
    outcome = {
        "challenge": module,
        "flag_len": len(flag),
        "solved": False,
        "submissions": [],
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "stop_reason": "",
        "error": None,
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = make_workdir(
                Path(tmp), cid, readme, f"http://127.0.0.1:{target.port}", flag
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("CTFY_", "LLM_", "OPENAI_", "ANTHROPIC_"))
            }
            env.update(
                {
                    "CTFY_WORK_DIR": str(work),
                    "CTFY_PLATFORM_URL": platform.base,
                    "CTFY_TOKEN": "bench-token",
                    "CTFY_EVAL_CHALLENGE_ID": cid,
                    "CTFY_EVAL_COMPETITION_ID": "localbench",
                    "CTFY_EVAL_TARGET_INSTANCE": "bench-instance-1",
                    "LLM_FLAVOR": config.get("flavor", "openai"),
                    "LLM_BASE_URL": config["base_url"],
                    "LLM_API_KEY": config["api_key"],
                    "LLM_MODEL": config["model"],
                    "PYTHONIOENCODING": "utf-8",
                    "no_proxy": "localhost,127.0.0.1",
                    "NO_PROXY": "localhost,127.0.0.1",
                }
            )
            try:
                proc = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "app" / "entrypoint.py")],
                    cwd=tmp,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=RUN_TIMEOUT_S,
                )
                outcome["exit_code"] = proc.returncode
                if proc.returncode not in (0,) and proc.stderr:
                    outcome["error"] = proc.stderr.strip()[-500:]
            except subprocess.TimeoutExpired as exc:
                outcome["error"] = f"agent exceeded {RUN_TIMEOUT_S}s"
                # Keep whatever the agent had done and said before the kill.
                (work / "agent-stderr.log").write_text(
                    (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str)
                    else (exc.stderr or b"")[-4000:].decode("utf-8", "replace"),
                    encoding="utf-8",
                )
            result_path = work / "result.json"
            if result_path.exists():
                result = json.loads(result_path.read_text(encoding="utf-8"))
                outcome["turns"] = result.get("turns", 0)
                outcome["input_tokens"] = result.get("input_tokens", 0)
                outcome["output_tokens"] = result.get("output_tokens", 0)
                outcome["stop_reason"] = result.get("stop_reason", "")
            outcome["solved"] = platform.solved
            outcome["submissions"] = [
                {k: r["body"].get(k) for k in ("question_id", "answer")}
                for r in platform.submissions
            ]
            if keep_dir:
                import shutil

                dest = keep_dir / module
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(work, dest)
    except Exception as exc:  # noqa: BLE001 — the bench must report, not crash
        outcome["error"] = f"{exc.__class__.__name__}: {exc}"
    finally:
        outcome["seconds"] = round(time.monotonic() - started, 1)
        platform.close()
        target.terminate()
        target.wait(timeout=5)
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BENCH / "config.json"))
    parser.add_argument("--only", default="", help="comma-separated module filter")
    parser.add_argument("--keep", default=str(BENCH / "runs"), help="dir to copy workdirs into")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    keep = Path(args.keep)
    keep.mkdir(parents=True, exist_ok=True)

    selected = [
        c for c in CHALLENGES
        if not args.only or c[0] in [x.strip() for x in args.only.split(",")]
    ]
    print(f"model={config['model']} challenges={[c[0] for c in selected]}\n")
    results = []
    for module, cid, readme in selected:
        print(f"--- {module} ...", flush=True)
        outcome = run_one(module, cid, readme, config, keep)
        results.append(outcome)
        mark = "✅" if outcome["solved"] else "❌"
        print(
            f"{mark} {module}: solved={outcome['solved']} turns={outcome['turns']} "
            f"tokens={outcome['input_tokens']}/{outcome['output_tokens']} "
            f"{outcome['seconds']}s stop={outcome['stop_reason']}"
            + (f" err={outcome['error']}" if outcome.get("error") else ""),
            flush=True,
        )

    solved = sum(1 for r in results if r["solved"])
    print(f"\n=== {solved}/{len(results)} solved ===")
    report = keep / "report.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    main()
