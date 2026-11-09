"""Whole-round integration: subprocess entrypoint, fake model gateway,
fake platform. Proves the loop the protocol actually grades: a hosted
run reaches its model through LLM_BASE_URL, submits through the
platform, refreshes gated questions and writes a truthful result.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .fixtures import instance_task, qa_task, write_work_dir
from .test_llm_gateway import FakeLLMServer
from .test_platform_client import FakeCtfyServer

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "app" / "entrypoint.py"


def run_agent(work_dir: Path, env_over: dict) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CTFY_", "LLM_", "OPENAI_", "ANTHROPIC_"))
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env["no_proxy"] = env["NO_PROXY"] = "localhost,127.0.0.1"
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)
    env.update(env_over)
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        cwd=str(Path(tempfile.mkdtemp())),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class RoundHarness(unittest.TestCase):
    def setUp(self):
        self._proxies = {
            k: os.environ.get(k)
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
        }
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for key, value in self._proxies.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for attr in ("llm", "platform"):
            server = getattr(self, attr, None)
            if server:
                server.close()
        self.tmp.cleanup()

    def hosted_env(self, work: Path, llm_base: str, platform_base: str,
                   challenge_id: str, qa: bool = False) -> dict:
        env = {
            "CTFY_WORK_DIR": str(work),
            "CTFY_PLATFORM_URL": platform_base,
            "CTFY_TOKEN": "round-token",
            "CTFY_EVAL_CHALLENGE_ID": challenge_id,
            "CTFY_EVAL_COMPETITION_ID": "tpctf2026",
            "LLM_FLAVOR": "openai",
            "LLM_BASE_URL": llm_base,
            "LLM_API_KEY": "funded-handle",
            "LLM_MODEL": "gpt-test",
        }
        if not qa:
            env["CTFY_EVAL_TARGET_INSTANCE"] = "3f9c0f12"
        return env

    def result_of(self, work: Path) -> dict:
        return json.loads((work / "result.json").read_text(encoding="utf-8"))


class QaRoundTest(RoundHarness):
    def test_answered_and_submitted(self):
        model_reply = json.dumps(
            {"answers": [{"question_id": "answer", "value": "A", "confidence": 0.95}]}
        )
        llm = FakeLLMServer({
            "choices": [{"message": {"role": "assistant", "content": model_reply}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 20},
        })
        self.llm = llm
        platform = FakeCtfyServer()
        self.platform = platform
        platform.routes[("GET", "/api/v1/challenges/QA-001")] = (
            200, {"id": "QA-001", "questions": [{"id": "answer", "prompt": "pick"}]}
        )
        platform.routes[("POST", "/api/v1/qa/submissions")] = (
            200,
            {"id": "s1", "correct": True, "question_id": "answer",
             "challenge_fully_solved": True, "attempts_remaining": None},
        )
        work = write_work_dir(Path(self.tmp.name), qa_task())

        proc = run_agent(
            work,
            self.hosted_env(work, llm.base, platform.base, "QA-001", qa=True),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        result = self.result_of(work)
        self.assertEqual(result["solved"], ["answer"])
        self.assertTrue(result["fully_solved"])
        self.assertGreaterEqual(result["turns"], 1)  # the run reached its model
        self.assertGreater(result["input_tokens"], 0)
        # Already judged correctly: nothing left for the fallback to submit.
        self.assertEqual(result["answers"], {})

        # The model call really went to LLM_BASE_URL with the platform's key.
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(llm.requests[0]["headers"]["Authorization"], "Bearer funded-handle")
        self.assertIn("pick", llm.requests[0]["body"]["messages"][-1]["content"])

        # The submission carried the QA body, not an instance id.
        post = [r for r in platform.requests if r["method"] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["body"]["question_id"], "answer")
        self.assertNotIn("instance_id", post[0]["body"])


class GatedRoundTest(RoundHarness):
    def test_correct_answer_unlocks_next_question(self):
        def reply(payload):
            return (
                200,
                {
                    "choices": [{"message": {"role": "assistant",
                                             "content": json.dumps(payload)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                },
            )

        llm = FakeLLMServer(None)
        llm.script = [
            reply({"answers": [{"question_id": "flag", "value": "ctfy{one}",
                                "confidence": 0.9}]}),
            reply({"answers": [{"question_id": "stage2", "value": "ctfy{two}",
                                "confidence": 0.9}]}),
        ]
        self.llm = llm
        platform = FakeCtfyServer()
        self.platform = platform
        challenge_key = ("GET", "/api/v1/challenges/CTFARENA-001")
        state = {"unlocked": False}

        def challenge_view():
            questions = [{"id": "flag", "prompt": "The vault master secret"}]
            if state["unlocked"]:
                questions.append({"id": "stage2", "prompt": "The second secret"})
            return 200, {"id": "CTFARENA-001", "questions": questions}

        platform.routes[challenge_key] = challenge_view
        submit_key = ("POST", "/api/v1/submissions")

        def grade():
            body = platform.requests[-1]["body"]
            if body["question_id"] == "flag" and body["answer"] == "ctfy{one}":
                state["unlocked"] = True
                return 200, {"id": "s", "correct": True, "question_id": "flag",
                             "challenge_fully_solved": False, "attempts_remaining": 3}
            if body["question_id"] == "stage2" and body["answer"] == "ctfy{two}":
                return 200, {"id": "s2", "correct": True, "question_id": "stage2",
                             "challenge_fully_solved": True, "attempts_remaining": 3}
            return 200, {"id": "s?", "correct": False, "question_id": body["question_id"],
                         "challenge_fully_solved": False, "attempts_remaining": 2}

        platform.routes[submit_key] = grade

        work = write_work_dir(
            Path(self.tmp.name), instance_task(gated_questions=1)
        )
        proc = run_agent(
            work, self.hosted_env(work, llm.base, platform.base, "CTFARENA-001")
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        result = self.result_of(work)
        self.assertEqual(result["solved"], ["flag", "stage2"])
        self.assertTrue(result["fully_solved"])
        self.assertEqual(len(llm.requests), 2)
        # The second pass saw the freshly unlocked question.
        self.assertIn("stage2", llm.requests[1]["body"]["messages"][-1]["content"])


class FailureShapeTest(RoundHarness):
    def test_unreachable_model_still_writes_result(self):
        platform = FakeCtfyServer()
        self.platform = platform
        platform.routes[("GET", "/api/v1/challenges/CTFARENA-001")] = (
            200, {"id": "CTFARENA-001", "questions": [{"id": "flag", "prompt": "x"}]}
        )
        work = write_work_dir(Path(self.tmp.name), instance_task())
        dead_port = "http://127.0.0.1:9"  # closed port, fails fast
        proc = run_agent(
            work, self.hosted_env(work, dead_port, platform.base, "CTFARENA-001")
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)  # degraded, not crashed
        result = self.result_of(work)
        self.assertEqual(result["stop_reason"], "llm_error")
        self.assertEqual(result["solved"], [])


if __name__ == "__main__":
    unittest.main()
