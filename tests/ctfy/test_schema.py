from __future__ import annotations

import json
import unittest

from ctfy.schema import (
    MalformedTask,
    UnsupportedRunMode,
    UnsupportedSchemaVersion,
    build_snapshot,
    load_task,
)

from .fixtures import instance_task, qa_task, write_work_dir


class InstanceSnapshotTest(unittest.TestCase):
    def test_round_trip_fields(self):
        snap = build_snapshot(instance_task())
        self.assertEqual(snap.schema_version, 3)
        self.assertEqual(snap.run_mode, "instance")
        self.assertEqual(snap.challenge.id, "CTFARENA-001")
        self.assertEqual([q.id for q in snap.questions], ["flag", "admin_pass"])
        self.assertEqual(snap.gated_questions, 1)
        self.assertEqual(snap.target_instance, "3f9c0f12")
        self.assertEqual(snap.target_endpoints[0].type, "http")
        self.assertEqual(snap.attachments[1].path, None)
        self.assertEqual(snap.max_turns, 40)
        self.assertFalse(snap.dry_run)
        self.assertFalse(snap.is_qa)
        # Unknown keys survive verbatim.
        self.assertIn("deliverable", snap.raw)

    def test_endpoint_type_validated(self):
        task = instance_task(
            target_endpoints=[{"label": "x", "type": "gopher", "url": "u"}]
        )
        with self.assertRaises(MalformedTask):
            build_snapshot(task)


class QaSnapshotTest(unittest.TestCase):
    def test_qa_shape(self):
        snap = build_snapshot(qa_task())
        self.assertTrue(snap.is_qa)
        self.assertIsNone(snap.target_instance)
        self.assertIsNone(snap.max_turns)  # null, never 0
        self.assertEqual(snap.target_endpoints, ())

    def test_qa_rejects_instance(self):
        with self.assertRaises(MalformedTask):
            build_snapshot(qa_task(target_instance="3f9c"))


class VersionTest(unittest.TestCase):
    def test_unknown_major_version_refused(self):
        with self.assertRaises(UnsupportedSchemaVersion):
            build_snapshot(instance_task(schema_version=4))

    def test_version_two_refused(self):
        with self.assertRaises(UnsupportedSchemaVersion):
            build_snapshot(instance_task(schema_version=2))


class RunModeTest(unittest.TestCase):
    def test_unimplemented_mode_refused(self):
        for mode in ("defence", "awd", "engagement"):
            with self.assertRaises(UnsupportedRunMode, msg=mode):
                build_snapshot(instance_task(run_mode=mode))

    def test_unknown_mode_refused(self):
        with self.assertRaises(MalformedTask):
            build_snapshot(instance_task(run_mode="king-of-the-hill"))


class MalformedTest(unittest.TestCase):
    def test_missing_challenge_id(self):
        task = instance_task(challenge={"id": "", "name": "x"})
        with self.assertRaises(MalformedTask):
            build_snapshot(task)

    def test_instance_without_target(self):
        with self.assertRaises(MalformedTask):
            build_snapshot(instance_task(target_instance=None))

    def test_max_turns_zero_refused(self):
        with self.assertRaises(MalformedTask):
            build_snapshot(instance_task(max_turns=0))

    def test_max_turns_string_refused(self):
        with self.assertRaises(MalformedTask):
            build_snapshot(instance_task(max_turns="40"))

    def test_gated_questions_negative_refused(self):
        with self.assertRaises(MalformedTask):
            build_snapshot(instance_task(gated_questions=-1))

    def test_question_without_id(self):
        task = instance_task(questions=[{"prompt": "no id"}])
        with self.assertRaises(MalformedTask):
            build_snapshot(task)


class LoadTaskTest(unittest.TestCase):
    def test_load_from_work_dir(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = write_work_dir(Path(tmp), instance_task())
            snap = load_task(root)
            self.assertEqual(snap.challenge.id, "CTFARENA-001")
            self.assertIsNotNone(snap.raw_readme_sha256)

    def test_missing_task_json(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MalformedTask):
                load_task(Path(tmp))

    def test_not_json(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(MalformedTask):
                load_task(root)


if __name__ == "__main__":
    unittest.main()
