from __future__ import annotations

import unittest
from pathlib import Path

from app.config import ConfigError, from_env, validate_contract
from ctfy.schema import build_snapshot

from .fixtures import instance_task, qa_task


def base_env(work_dir: Path, **over) -> dict:
    env = {
        "CTFY_WORK_DIR": str(work_dir),
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
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


class FromEnvTest(unittest.TestCase):
    def test_full_hosted_env(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp)))
            self.assertEqual(config.challenge_id, "CTFARENA-001")
            self.assertEqual(config.target_instance, "3f9c0f12")
            self.assertTrue(config.llm.funded)

    def test_repr_never_leaks_secrets(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp)))
            text = repr(config) + repr(config.llm)
            self.assertNotIn("run-token", text)
            self.assertNotIn("funded-handle", text)
            self.assertIn("<set>", text)

    def test_qa_has_no_instance(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = base_env(Path(tmp), CTFY_EVAL_TARGET_INSTANCE=None)
            config = from_env(env)
            self.assertIsNone(config.target_instance)

    def test_dry_run_flag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp), CTFY_EVAL_DRY_RUN="1"))
            self.assertTrue(config.dry_run)

    def test_bad_dry_run_value_refused(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                from_env(base_env(Path(tmp), CTFY_EVAL_DRY_RUN="true"))

    def test_bad_flavor_refused(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                from_env(base_env(Path(tmp), LLM_FLAVOR="google"))

    def test_max_turns_parsed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp), CTFY_EVAL_MAX_TURNS="25"))
            self.assertEqual(config.max_turns, 25)
            config2 = from_env(base_env(Path(tmp)))
            self.assertIsNone(config2.max_turns)

    def test_max_turns_non_positive_refused(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                from_env(base_env(Path(tmp), CTFY_EVAL_MAX_TURNS="0"))


class ValidateContractTest(unittest.TestCase):
    def test_instance_round_trip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp)))
            validate_contract(config, build_snapshot(instance_task()))

    def test_challenge_mismatch(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp)))
            task = build_snapshot(instance_task(challenge={"id": "OTHER-9"}))
            with self.assertRaises(ConfigError):
                validate_contract(config, task)

    def test_missing_token_on_hosted(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp), CTFY_TOKEN=None))
            with self.assertRaises(ConfigError):
                validate_contract(config, build_snapshot(instance_task()))

    def test_empty_llm_key_refused_no_fallback(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp), LLM_API_KEY=""))
            with self.assertRaises(ConfigError) as ctx:
                validate_contract(config, build_snapshot(instance_task()))
            self.assertIn("fall back", str(ctx.exception))

    def test_dry_run_needs_nothing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = base_env(
                Path(tmp),
                CTFY_TOKEN=None,
                LLM_API_KEY=None,
                LLM_FLAVOR=None,
                CTFY_EVAL_DRY_RUN="1",
            )
            config = from_env(env)
            # A dry-run task carries dry_run: true and no platform fields.
            task = build_snapshot(instance_task(dry_run=True, platform_url=""))
            validate_contract(config, task)

    def test_qa_contract(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = base_env(
                Path(tmp),
                CTFY_EVAL_TARGET_INSTANCE=None,
                CTFY_EVAL_CHALLENGE_ID="QA-001",
            )
            config = from_env(env)
            validate_contract(config, build_snapshot(qa_task()))

    def test_qa_with_stray_instance_refused(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = from_env(base_env(Path(tmp)))
            with self.assertRaises(ConfigError):
                validate_contract(config, build_snapshot(qa_task()))


if __name__ == "__main__":
    unittest.main()
