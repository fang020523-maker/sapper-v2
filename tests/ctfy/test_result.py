from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ctfy.result import ResultWriter, RunSummary


class RunSummaryTest(unittest.TestCase):
    def test_dry_run_factory(self):
        summary = RunSummary.dry_run()
        self.assertEqual(summary.stop_reason, "dry_run")
        out = summary.to_json()
        self.assertEqual(out["schema_version"], 3)
        self.assertEqual(out["solved"], [])
        self.assertFalse(out["fully_solved"])

    def test_full_shape(self):
        summary = RunSummary(
            answers={"flag": "ctfy{x}"},
            solved=["flag"],
            turns=17,
            tool_calls_made=41,
            input_tokens=120000,
            output_tokens=8000,
            stop_reason="done",
            writeup="## What I did\n",
        )
        out = summary.to_json()
        self.assertEqual(out["answers"], {"flag": "ctfy{x}"})
        self.assertEqual(out["solved"], ["flag"])
        self.assertNotIn("error", out)

    def test_error_scrubbed(self):
        summary = RunSummary().with_safe_error(
            RuntimeError("Authorization: Bearer sk-secret-123 failed")
        )
        self.assertIn("omitted", summary.error)
        self.assertNotIn("sk-secret", summary.error)
        self.assertIn("error", summary.to_json())

    def test_error_capped(self):
        summary = RunSummary().with_safe_error(ValueError("x" * 10_000))
        self.assertLessEqual(len(summary.error), 301)


class ResultWriterTest(unittest.TestCase):
    def test_atomic_write_and_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            writer = ResultWriter(path)
            writer.write(RunSummary.dry_run())
            first = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(first["stop_reason"], "dry_run")
            # A round rewrite replaces the whole file, not merges into it.
            writer.write(RunSummary(stop_reason="done", turns=3))
            second = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(second["stop_reason"], "done")
            self.assertNotIn("dry_run", path.read_text(encoding="utf-8"))

    def test_no_temp_files_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            ResultWriter(path).write(RunSummary.dry_run())
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "result.json"]
            self.assertEqual(leftovers, [])

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "result.json"
            ResultWriter(path).write(RunSummary.dry_run())
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
