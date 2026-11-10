"""D01: a real local web challenge, solved end to end by the explorer.

A tiny vulnerable site (index hints at a backup file that carries the
flag) stands in for a jeopardy web challenge. The "model" is scripted
but honest about its inputs: each turn it reads the prompt's evidence
section and decides from what is actually there — the flag value it
submits is parsed out of the target's real response, never hardcoded
by the test.

Run the round twice against a fresh workdir each time: the same image
logic must solve it repeatably (D02's shape at test scale).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .fixtures import instance_task, write_work_dir
from .test_llm_gateway import FakeLLMServer
from .test_platform_client import FakeCtfyServer

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "app" / "entrypoint.py"
FLAG = "FLAG{buried_backup_8f3a91}"

FLAG_RE = re.compile(r"FLAG\{[^}\s]+\}")


class TinySite(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            body = (
                "<html><body>DocuVault docs — see /backup.txt for the old "
                "deployment notes.</body></html>"
            ).encode()
            status = 200
        elif self.path == "/backup.txt":
            body = f"# old notes\nmaster secret: {FLAG}\n".encode()
            status = 200
        else:
            body = b"404 nothing here"
            status = 404
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def llm_decision(body: dict):
    """One honest scripted solver: decide from the evidence in the prompt."""
    prompt = body["messages"][-1]["content"]

    def reply(payload):
        return (
            200,
            {
                "choices": [{"message": {"role": "assistant",
                                         "content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 40},
            },
        )

    seen_flag = FLAG_RE.search(prompt)
    if seen_flag:
        return reply({"thought": "flag found in backup notes",
                      "action": {"type": "answer", "question_id": "flag",
                                 "value": seen_flag.group(0),
                                 "confidence": 0.95, "evidence": ["EV-002"]}})
    if "backup.txt" in prompt:
        return reply({"thought": "fetch the hinted backup file",
                      "action": {"type": "http", "method": "GET",
                                 "url": prompt.splitlines()[
                                     [i for i, l in enumerate(prompt.splitlines())
                                      if l.startswith("- http ")][0]
                                 ].split(" ")[2].rstrip("/") + "/backup.txt"}})
    return reply({"thought": "start with the index page",
                  "action": {"type": "http", "method": "GET",
                             "url": prompt.splitlines()[
                                 [i for i, l in enumerate(prompt.splitlines())
                                  if l.startswith("- http ")][0]
                             ].split(" ")[2].rstrip("/") + "/"}})


class WebBaselineTest(unittest.TestCase):
    def setUp(self):
        self._proxies = {
            k: os.environ.get(k)
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
        }
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ.pop(k, None)

    def tearDown(self):
        for key, value in self._proxies.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run_round(self) -> dict:
        site = ThreadingHTTPServer(("127.0.0.1", 0), TinySite)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        site_port = site.server_address[1]
        llm = FakeLLMServer(None)
        llm.script = [llm_decision] * 10
        platform = FakeCtfyServer()
        try:
            challenge_key = ("GET", "/api/v1/challenges/CTFARENA-001")
            platform.routes[challenge_key] = (
                200,
                {"id": "CTFARENA-001",
                 "questions": [{"id": "flag", "prompt": "The vault master secret"}]},
            )
            platform.routes[("POST", "/api/v1/submissions")] = (
                200,
                {"id": "s1", "correct": True, "question_id": "flag",
                 "challenge_fully_solved": True, "attempts_remaining": None},
            )
            with tempfile.TemporaryDirectory() as tmp:
                work = write_work_dir(
                    Path(tmp),
                    instance_task(
                        target_endpoints=[
                            {"label": "web", "type": "http",
                             "url": f"http://127.0.0.1:{site_port}"}
                        ]
                    ),
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
                        "CTFY_TOKEN": "round-token",
                        "CTFY_EVAL_CHALLENGE_ID": "CTFARENA-001",
                        "CTFY_EVAL_COMPETITION_ID": "tpctf2026",
                        "CTFY_EVAL_TARGET_INSTANCE": "3f9c0f12",
                        "LLM_FLAVOR": "openai",
                        "LLM_BASE_URL": llm.base,
                        "LLM_API_KEY": "funded-handle",
                        "LLM_MODEL": "gpt-test",
                        "PYTHONIOENCODING": "utf-8",
                        "no_proxy": "localhost,127.0.0.1",
                        "NO_PROXY": "localhost,127.0.0.1",
                    }
                )
                for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                    env.pop(key, None)
                for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                    env.pop(key, None)
                proc = subprocess.run(
                    [sys.executable, str(ENTRYPOINT)],
                    cwd=tempfile.mkdtemp(),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(
                    (work / "result.json").read_text(encoding="utf-8")
                )
                posts = [r for r in platform.requests if r["method"] == "POST"]
                evidence_state = json.loads(
                    (work / ".sapper" / "evidence.json").read_text(encoding="utf-8")
                )
                return {
                    "result": result,
                    "submitted": posts[0]["body"]["answer"] if posts else None,
                    "posts": posts,
                    "evidence": evidence_state["records"],
                    "llm_calls": len(llm.requests),
                }
        finally:
            llm.close()
            platform.close()
            site.shutdown()
            site.server_close()

    def test_solves_it_end_to_end_twice(self):
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                outcome = self._run_round()
                self.assertEqual(
                    outcome["submitted"], FLAG,
                    "the submitted value must come from the target's bytes",
                )
                self.assertEqual(outcome["result"]["solved"], ["flag"])
                self.assertTrue(outcome["result"]["fully_solved"])
                self.assertEqual(outcome["llm_calls"], 3)
                kinds = [r["kind"] for r in outcome["evidence"]]
                self.assertEqual(kinds, ["http", "http"])


if __name__ == "__main__":
    unittest.main()
