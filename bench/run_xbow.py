#!/usr/bin/env python3
"""Run the agent against real third-party challenges (xbow validation-benchmarks).

Each challenge directory brings its own docker-compose with a FLAG build
arg and a healthcheck; we build it with a fresh flag, wait for the box,
point the agent at it through the same fake-platform harness the local
bench uses, and record whether the flag was actually submitted.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO_ROOT = BENCH.parent
sys.path.insert(0, str(REPO_ROOT))

from bench.fake_platform import FakePlatform  # noqa: E402
from bench.run_local_eval import load_config, make_workdir  # noqa: E402

RUN_TIMEOUT_S = 900  # real challenges think longer than the local bench

HEALTH_TIMEOUT_S = 240


def _docker_exe() -> str:
    import shutil

    found = shutil.which("docker")
    if found:
        return found
    for candidate in (
        r"D:\Docker\resources\bin\docker.exe",
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    ):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("docker CLI not found")


def compose(dir_: Path, *args: str, env_extra: dict | None = None) -> str:
    import os

    env = dict(os.environ)
    env.update(env_extra or {})
    proc = subprocess.run(
        [_docker_exe(), "compose", *args],
        cwd=str(dir_),
        env=env,
        capture_output=True,
        text=True,
        timeout=1500,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker compose {' '.join(args)} failed: {(proc.stderr or '')[-400:]}"
        )
    return proc.stdout


def wait_healthy(url: str) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError:
            return True  # answering is healthy enough
        except Exception:
            time.sleep(3)
    return False


def run_challenge(challenge_dir: Path, config: dict, keep: Path) -> dict:
    meta = json.loads((challenge_dir / "benchmark.json").read_text(encoding="utf-8"))
    name = challenge_dir.name
    flag = "flag{" + secrets.token_hex(8) + "}"
    outcome = {"challenge": name, "title": meta.get("name", ""), "level": meta.get("level"),
               "tags": meta.get("tags", []), "solved": False, "submissions": [],
               "turns": 0, "input_tokens": 0, "output_tokens": 0,
               "stop_reason": "", "error": None}

    compose(challenge_dir, "down", "-v", "--remove-orphans", env_extra={"FLAG": flag})
    print(f"  building {name} (flag={flag[:12]}…)…", flush=True)
    try:
        # No --wait: it blocks forever on a failing healthcheck; our own
        # wait_healthy() enforces the deadline instead.
        compose(challenge_dir, "up", "-d", "--build", env_extra={"FLAG": flag})
    except Exception as exc:
        outcome["error"] = f"compose up failed: {exc}"
        return outcome

    # Find the published port: ask compose directly for each service's
    # port-80 mapping (`docker compose port <svc> 80` -> 0.0.0.0:NNNN).
    port = None
    try:
        services = [
            s.strip()
            for s in compose(
                challenge_dir, "config", "--services", env_extra={"FLAG": flag}
            ).splitlines()
            if s.strip()
        ]
        for service in services:
            try:
                out = compose(challenge_dir, "port", service, "80", env_extra={"FLAG": flag})
            except RuntimeError:
                continue
            candidate = out.strip().rsplit(":", 1)[-1]
            if candidate.isdigit():
                port = int(candidate)
                break
    except RuntimeError as exc:
        outcome["error"] = f"port discovery failed: {exc}"
    if not port:
        outcome.setdefault("error", "no published port found")
        compose(challenge_dir, "down", "-v", "--remove-orphans", env_extra={"FLAG": flag})
        return outcome

    target_url = f"http://127.0.0.1:{port}"
    if not wait_healthy(target_url):
        outcome["error"] = "target never became healthy"
        compose(challenge_dir, "down", "-v", "--remove-orphans", env_extra={"FLAG": flag})
        return outcome

    prompt_text = meta.get("description") or meta.get("name") or "Get the flag."
    platform = FakePlatform(name, "The flag of this challenge", flag)
    import os

    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            work = make_workdir(Path(tmp), name, prompt_text, target_url, flag)
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
                    "CTFY_EVAL_CHALLENGE_ID": name,
                    "CTFY_EVAL_COMPETITION_ID": "xbowbench",
                    "CTFY_EVAL_TARGET_INSTANCE": "bench-instance-1",
                    "LLM_FLAVOR": config.get("flavor", "openai"),
                    "LLM_BASE_URL": config["base_url"],
                    "LLM_API_KEY": config["api_key"],
                    "LLM_MODEL": config["model"],
                    "LLM_EXTRA_BODY": json.dumps(config.get("extra_body") or {}),
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
                if proc.returncode != 0 and proc.stderr:
                    outcome["error"] = proc.stderr.strip()[-400:]
            except subprocess.TimeoutExpired as exc:
                outcome["error"] = f"agent exceeded {RUN_TIMEOUT_S}s"
                (work / "agent-stderr.log").write_text(
                    (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str)
                    else (exc.stderr or b"")[-4000:].decode("utf-8", "replace"),
                    encoding="utf-8",
                )
            result_path = work / "result.json"
            if result_path.exists():
                result = json.loads(result_path.read_text(encoding="utf-8"))
                outcome.update(
                    turns=result.get("turns", 0),
                    input_tokens=result.get("input_tokens", 0),
                    output_tokens=result.get("output_tokens", 0),
                    stop_reason=result.get("stop_reason", ""),
                )
            outcome["solved"] = platform.solved
            outcome["submissions"] = [
                {k: r["body"].get(k) for k in ("question_id", "answer")}
                for r in platform.submissions
            ]
            import shutil

            dest = keep / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(work, dest)
    finally:
        outcome["seconds"] = round(time.monotonic() - started, 1)
        platform.close()
        compose(challenge_dir, "down", "-v", "--remove-orphans", env_extra={"FLAG": flag})
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmarks-root", default=r"D:\AICTF\validation-benchmarks\benchmarks")
    parser.add_argument("--config", default=str(BENCH / "config.json"))
    parser.add_argument("--only", required=True, help="comma-separated XBEN dirs")
    parser.add_argument("--keep", default=str(BENCH / "runs_xbow"))
    args = parser.parse_args()

    config = load_config(Path(args.config))
    keep = Path(args.keep)
    keep.mkdir(parents=True, exist_ok=True)
    root = Path(args.benchmarks_root)

    results = []
    for name in [x.strip() for x in args.only.split(",") if x.strip()]:
        cdir = root / name
        if not cdir.exists():
            print(f"?? {name}: not found under {root}")
            continue
        print(f"--- {name}", flush=True)
        outcome = run_challenge(cdir, config, keep)
        results.append(outcome)
        mark = "✅" if outcome["solved"] else "❌"
        print(
            f"{mark} {name}: solved={outcome['solved']} turns={outcome['turns']} "
            f"tokens={outcome['input_tokens']}/{outcome['output_tokens']} "
            f"{outcome.get('seconds','?')}s stop={outcome['stop_reason']}"
            + (f" err={outcome['error']}" if outcome.get("error") else ""),
            flush=True,
        )
        (keep / "report.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    solved = sum(1 for r in results if r["solved"])
    print(f"\n=== {solved}/{len(results)} solved ===")
    return 0


if __name__ == "__main__":
    main()
