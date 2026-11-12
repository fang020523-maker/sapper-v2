#!/usr/bin/env python3
"""Full-dress rehearsal: run the ACTUAL competition image the way the
platform will — container, resource caps, injected env, mounted
/ctfy/work — against a live target and a fake platform, with a real
model behind LLM_BASE_URL. Proves the whole chain end to end before
the event: container → task.json → model calls → target probes →
submission → result.json → exit 0.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FLAG = "flag{" + secrets.token_hex(8) + "}"


class TargetHandler(BaseHTTPRequestHandler):
    """A tiny challenge bound to 0.0.0.0 so the container can reach it."""

    def do_GET(self):  # noqa: N802
        if self.path == "/":
            body = b"<html><body><h1>Acme Notes</h1><p>A tiny notes site.</p></body></html>"
            status = 200
        elif self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /git_backup_2024/\n"
            status = 200
        elif self.path == "/git_backup_2024/":
            body = f"# old deploy notes\nflag = {FLAG}\n".encode()
            status = 200
        else:
            body = b"404 nothing here"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PlatformHandler(BaseHTTPRequestHandler):
    """The fake platform the agent talks to (0.0.0.0, like the real one)."""

    def do_GET(self):  # noqa: N802
        fake = self.server.fake  # type: ignore[attr-defined]
        fake.requests.append(("GET", self.path))
        if self.path.startswith("/api/v1/challenges/"):
            blob = json.dumps(
                {
                    "id": fake.challenge_id,
                    "questions": [{"id": "flag", "prompt": "The flag hidden in this challenge"}],
                }
            ).encode()
            status = 200
        else:
            blob = b"{}"
            status = 404
        self._reply(status, blob)

    def do_POST(self):  # noqa: N802
        fake = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        fake.requests.append(("POST", self.path, body))
        if self.path == "/api/v1/submissions":
            correct = body.get("answer") == fake.flag
            blob = json.dumps(
                {
                    "id": f"s{len(fake.requests)}",
                    "correct": correct,
                    "question_id": body.get("question_id"),
                    "challenge_fully_solved": correct,
                    "attempts_remaining": 4,
                }
            ).encode()
            self._reply(200, blob)
        else:
            self._reply(404, b"{}")

    def _reply(self, status, blob):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):
        pass


class Server:
    def __init__(self, handler, fake=None):
        self._httpd = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        if fake is not None:
            self._httpd.fake = fake  # type: ignore[attr-defined]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self._httpd.server_address[1]

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def _docker_exe() -> str:
    import shutil

    found = shutil.which("docker")
    if found:
        return found
    for candidate in (
        "D:/Docker/resources/bin/docker.exe",
        "C:/Program Files/Docker/Docker/resources/bin/docker.exe",
    ):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("docker CLI not found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="registry.ctfy.io/734c764a-f28e-48cb-8e4d-b9d3b49a4186/sapper-ctfy:v5")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))

    class Fake:
        challenge_id = "REHEARSAL-1"
        flag = FLAG
        requests: list = []

    fake = Fake()
    target = Server(TargetHandler)
    platform = Server(PlatformHandler, fake)
    host = "host.docker.internal"

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        task = {
            "schema_version": 3,
            "run_mode": "instance",
            "challenge": {"id": "REHEARSAL-1", "name": "Acme Notes", "description": "A tiny notes site. Get the flag."},
            "competition_id": "rehearsal",
            "questions": [{"id": "flag", "prompt": "The flag hidden in this challenge"}],
            "gated_questions": 0,
            "scope": [f"http://{host}:{target.port}"],
            "rules_of_engagement": ["Attack only the published endpoint."],
            "platform_url": f"http://{host}:{platform.port}",
            "max_turns": None,
            "target_instance": "rehearsal-inst-1",
            "target_endpoints": [{"label": "web", "type": "http", "url": f"http://{host}:{target.port}"}],
            "attachments": [],
        }
        (work / "task.json").write_text(json.dumps(task), encoding="utf-8")
        (work / "README.md").write_text(
            "# REHEARSAL-1\n\nAcme Notes — a tiny notes site. Question: the flag "
            "hidden in this challenge. Submit it as the answer.\n",
            encoding="utf-8",
        )

        print(f"flag        : {FLAG}")
        print(f"target      : http://{host}:{target.port}")
        print(f"fake platform: http://{host}:{platform.port}")
        print(f"image       : {args.image}")
        print("--- docker run (2cpu/8g/512pids/cap-drop, non-root, real model) ---", flush=True)

        import os

        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CTFY_", "LLM_", "OPENAI_", "ANTHROPIC_"))
        }
        env.update(
            {
                "CTFY_WORK_DIR": "/ctfy/work",
                "CTFY_PLATFORM_URL": f"http://{host}:{platform.port}",
                "CTFY_TOKEN": "rehearsal-token",
                "CTFY_EVAL_CHALLENGE_ID": "REHEARSAL-1",
                "CTFY_EVAL_COMPETITION_ID": "rehearsal",
                "CTFY_EVAL_TARGET_INSTANCE": "rehearsal-inst-1",
                "LLM_FLAVOR": config.get("flavor", "openai"),
                "LLM_BASE_URL": config["base_url"],
                "LLM_API_KEY": config["api_key"],
                "LLM_MODEL": config["model"],
                "LLM_EXTRA_BODY": json.dumps(config.get("extra_body") or {}),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        started = time.monotonic()
        proc = subprocess.run(
            [
                _docker_exe(), "run", "--rm",
                "--cpus", "2", "--memory", "8g", "--pids-limit", "512",
                "--cap-drop", "ALL",
                "--add-host", f"platform.local:{_host_ip()}",
                "-e", "CTFY_WORK_DIR", "-e", "CTFY_PLATFORM_URL", "-e", "CTFY_TOKEN",
                "-e", "CTFY_EVAL_CHALLENGE_ID", "-e", "CTFY_EVAL_COMPETITION_ID",
                "-e", "CTFY_EVAL_TARGET_INSTANCE",
                "-e", "LLM_FLAVOR", "-e", "LLM_BASE_URL", "-e", "LLM_API_KEY",
                "-e", "LLM_MODEL", "-e", "LLM_EXTRA_BODY", "-e", "PYTHONIOENCODING",
                "-v", f"{work}:/ctfy/work", "-w", "/ctfy/work",
                args.image,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
        elapsed = round(time.monotonic() - started, 1)

        print(f"exit code   : {proc.returncode} ({elapsed}s)")
        if proc.stdout.strip():
            print("stdout tail :", proc.stdout.strip()[-200:])
        if proc.stderr.strip():
            print("stderr tail :", proc.stderr.strip()[-300:])

        result_path = work / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
        submissions = [r for r in fake.requests if r[0] == "POST"]
        solved = any(b.get("answer") == FLAG for _, _, b in submissions)

        print(f"submissions : {len(submissions)}")
        print(f"solved      : {solved}")
        if result:
            print(f"result.json : turns={result.get('turns')} stop={result.get('stop_reason')} "
                  f"tokens={result.get('input_tokens')}/{result.get('output_tokens')}")

        target.close()
        platform.close()

        ok = proc.returncode == 0 and solved and result is not None
        print("\n=== REHEARSAL " + ("PASSED ✅" if ok else "FAILED ❌") + " ===")
        return 0 if ok else 1


def _host_ip() -> str:
    # host.docker.internal already resolves inside the container on
    # Docker Desktop; this helper exists only if a custom hosts entry
    # were needed. Not used by default.
    return "192.0.2.1"


if __name__ == "__main__":
    sys.exit(main())
