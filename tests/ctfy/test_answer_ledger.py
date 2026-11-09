"""Ledger state-machine tests: every path a candidate's life can take."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from ctfy.answers import (
    AnswerCandidate,
    AnswerLedger,
    LedgerError,
    LedgerQuarantined,
)
from ctfy.client import CtfyClient
from ctfy.schema import build_snapshot

from .fixtures import instance_task, qa_task
from .test_platform_client import FakeCtfyServer, ProxyCleanTest

SUBMIT_KEY = ("POST", "/api/v1/submissions")
QA_SUBMIT_KEY = ("POST", "/api/v1/qa/submissions")


def ok(correct=True, remaining=2, qid="flag"):
    return (
        200,
        {
            "id": "sub-1",
            "correct": correct,
            "question_id": qid,
            "challenge_fully_solved": False,
            "attempts_remaining": remaining,
        },
    )


class LedgerHarness(ProxyCleanTest):
    def make_ledger(self, task_dict=None, server_routes=None, scripts=None):
        self.server = FakeCtfyServer()
        for key, value in (server_routes or {}).items():
            self.server.routes[key] = value
        for key, value in (scripts or {}).items():
            self.server.scripts[key] = value
        self.tmp = tempfile.TemporaryDirectory()
        self.task = build_snapshot(task_dict or instance_task())
        self.ledger = AnswerLedger(
            CtfyClient(self.server.base, "tok-1"),
            self.task,
            Path(self.tmp.name) / ".sapper" / "answers.json",
        )
        return self.ledger

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()
        super().tearDown()

    def propose_validated(self, ledger, qid="flag", value="ctfy{x}", confidence=0.9):
        entry_id = ledger.propose(
            AnswerCandidate(question_id=qid, value=value, confidence=confidence)
        )
        ledger.mark_validated(entry_id)
        return entry_id


class HappyPathTest(LedgerHarness):
    def test_correct_submission_closes_question(self):
        ledger = self.make_ledger(server_routes={SUBMIT_KEY: ok()})
        entry_id = self.propose_validated(ledger)
        entry = ledger.submit(entry_id)
        self.assertEqual(entry.status, "correct")
        self.assertTrue(ledger.question_closed("flag"))

    def test_wrong_with_attempts_left_allows_new_value(self):
        ledger = self.make_ledger(server_routes={SUBMIT_KEY: ok(correct=False, remaining=1)})
        first = self.propose_validated(ledger, value="ctfy{wrong}")
        entry = ledger.submit(first)
        self.assertEqual(entry.status, "wrong")
        self.assertFalse(ledger.question_closed("flag"))
        second = self.propose_validated(ledger, value="ctfy{right}")
        self.assertEqual(ledger.submit(second).status, "wrong")

    def test_wrong_exhausted_closes_question(self):
        ledger = self.make_ledger(server_routes={SUBMIT_KEY: ok(correct=False, remaining=0)})
        entry_id = self.propose_validated(ledger)
        ledger.submit(entry_id)
        self.assertTrue(ledger.question_closed("flag"))
        with self.assertRaises(LedgerError):
            self.propose_validated(ledger, value="ctfy{never}")


class QuarantineTest(LedgerHarness):
    def test_unknown_submission_is_quarantined(self):
        ledger = self.make_ledger(server_routes={SUBMIT_KEY: "HANG"})
        entry_id = self.propose_validated(ledger)
        with self.assertRaises(LedgerQuarantined):
            ledger.submit(entry_id)
        entry = ledger._entries[entry_id]
        self.assertEqual(entry.status, "unknown")

        # Never resubmit: not by the same entry…
        with self.assertRaises(LedgerError):
            ledger.submit(entry_id)
        # …not by proposing the same value again…
        with self.assertRaises(LedgerError):
            ledger.propose(
                AnswerCandidate(question_id="flag", value="ctfy{x}")
            )
        # …and not through the fallback channel.
        self.assertEqual(ledger.fallback_answers(), {})
        self.assertEqual(len([r for r in self.server.requests if (r["method"], r["path"]) == SUBMIT_KEY]), 1)

    def test_unknown_excluded_but_others_kept_in_fallback(self):
        ledger = self.make_ledger(server_routes={SUBMIT_KEY: "HANG"})
        quarantined = self.propose_validated(ledger, qid="flag", value="ctfy{lost}")
        with self.assertRaises(LedgerQuarantined):
            ledger.submit(quarantined)
        kept = self.propose_validated(ledger, qid="admin_pass", value="hunter2")
        fallback = ledger.fallback_answers()
        self.assertEqual(fallback, {"admin_pass": "hunter2"})


class ProposalRulesTest(LedgerHarness):
    def test_invisible_question_refused(self):
        ledger = self.make_ledger()
        with self.assertRaises(LedgerError):
            ledger.propose(AnswerCandidate(question_id="ghost", value="v"))

    def test_duplicate_value_refused_even_if_unspent(self):
        ledger = self.make_ledger()
        ledger.propose(AnswerCandidate(question_id="flag", value="ctfy{a}"))
        with self.assertRaises(LedgerError):
            ledger.propose(AnswerCandidate(question_id="flag", value="ctfy{a}"))

    def test_empty_value_refused(self):
        ledger = self.make_ledger()
        with self.assertRaises(LedgerError):
            ledger.propose(AnswerCandidate(question_id="flag", value=""))
        with self.assertRaises(LedgerError):
            ledger.propose(AnswerCandidate(question_id="flag", value=[]))

    def test_fallback_picks_highest_confidence_per_question(self):
        ledger = self.make_ledger()
        low = ledger.propose(AnswerCandidate(question_id="flag", value="ctfy{low}", confidence=0.4))
        ledger.mark_validated(low)
        high = ledger.propose(AnswerCandidate(question_id="flag", value="ctfy{high}", confidence=0.9))
        ledger.mark_validated(high)
        self.assertEqual(ledger.fallback_answers(), {"flag": "ctfy{high}"})


class PausedTest(LedgerHarness):
    def test_paused_returns_value_unspent(self):
        paused = (
            403,
            {"code": "competition_paused", "message": "wait", "retry_after_s": 60},
        )
        ledger = self.make_ledger(
            server_routes={SUBMIT_KEY: paused},
            scripts={SUBMIT_KEY: [paused, ok()]},
        )
        entry_id = self.propose_validated(ledger)
        from ctfy.client import CtfyApiError

        with self.assertRaises(CtfyApiError) as ctx:
            ledger.submit(entry_id)  # session paused: wait, don't burn it
        self.assertTrue(ctx.exception.paused)
        entry = ledger._entries[entry_id]
        self.assertEqual(entry.status, "validated")
        self.assertEqual(entry.attempts, 0)
        # After the pause, the same spend goes through.
        self.assertEqual(ledger.submit(entry_id).status, "correct")


class PersistenceTest(LedgerHarness):
    def test_state_survives_to_next_round(self):
        ledger = self.make_ledger(server_routes={SUBMIT_KEY: ok()})
        entry_id = self.propose_validated(ledger)
        ledger.submit(entry_id)
        state_path = Path(self.tmp.name) / ".sapper" / "answers.json"

        # A "next round" ledger over the same working directory.
        task2 = build_snapshot(instance_task())
        ledger2 = AnswerLedger(
            CtfyClient(self.server.base, "tok-1"), task2, state_path
        )
        self.assertTrue(ledger2.question_closed("flag"))
        with self.assertRaises(LedgerError):
            ledger2.propose(AnswerCandidate(question_id="flag", value="ctfy{x}"))

    def test_submitting_resumed_as_unknown(self):
        ledger = self.make_ledger()
        entry_id = self.propose_validated(ledger)
        ledger._entries[entry_id].status = "submitting"
        ledger._save()
        state_path = Path(self.tmp.name) / ".sapper" / "answers.json"

        ledger2 = AnswerLedger(
            CtfyClient(self.server.base, "tok-1"),
            build_snapshot(instance_task()),
            state_path,
        )
        self.assertEqual(ledger2._entries[entry_id].status, "unknown")
        self.assertEqual(ledger2.fallback_answers(), {})

    def test_foreign_challenge_state_ignored(self):
        ledger = self.make_ledger()
        entry_id = self.propose_validated(ledger)
        ledger._save()
        state_path = Path(self.tmp.name) / ".sapper" / "answers.json"

        other = AnswerLedger(
            CtfyClient(self.server.base, "tok-1"),
            build_snapshot(instance_task(challenge={"id": "OTHER-1", "name": "x"})),
            state_path,
        )
        self.assertEqual(other._entries, {})


class QaRoutingTest(LedgerHarness):
    def test_qa_uses_qa_endpoint_and_body(self):
        ledger = self.make_ledger(
            task_dict=qa_task(),
            server_routes={QA_SUBMIT_KEY: ok(qid="answer")},
        )
        entry_id = self.propose_validated(ledger, qid="answer", value="A")
        ledger.submit(entry_id)
        body = self.server.requests[0]["body"]
        self.assertEqual(body["challenge_id"], "QA-001")
        self.assertEqual(body["competition_id"], "tpctf2026")
        self.assertNotIn("instance_id", body)


if __name__ == "__main__":
    unittest.main()
