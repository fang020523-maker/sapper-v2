"""Single-challenge coordinator: one hosted round, start to result.

The shape of a round: restore the answer ledger from this working
directory, re-read the visible questions from the platform (the task
file is a snapshot; gating changes the set), then loop — one model
pass over the open questions, submit what clears the bar, refresh on
every correct answer — until the questions, the budget or the ideas
run out. The fact-graph exploration loop (reason/explore/verify with
live target interaction) extends this in S4; the skeleton below is the
honest version of what this build can actually do.
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from app.config import RuntimeConfig
from app.explorer import Explorer
from app.scope import EndpointScope
from core.evidence import EvidenceStore
from core.llm.router import (
    GatewayError,
    LLMRequest,
    UsageTracker,
    build_gateway,
)
from core.parse import ModelParseError, extract_json_object
from core.tools.target import TargetClient
from ctfy.answers import AnswerCandidate, AnswerLedger, LedgerError, LedgerQuarantined
from ctfy.client import CtfyApiError, CtfyClient
from ctfy.result import RunSummary
from ctfy.schema import QuestionView, TaskSnapshot

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MIN_CONFIDENCE = 0.5
DEFAULT_PASSES = 4
HARD_PASS_CAP = 8
PAUSED_SLEEP_CAP_S = 120


def run_round(config: RuntimeConfig, task: TaskSnapshot) -> RunSummary:
    usage = UsageTracker()
    client = CtfyClient(config.platform_url, config.token)
    ledger = AnswerLedger(
        client, task, config.work_dir / ".sapper" / "answers.json"
    )

    readme = _read_readme(config.work_dir)
    questions = _refresh_questions(client, task) or list(task.questions)
    max_passes = min(task.max_turns or DEFAULT_PASSES, HARD_PASS_CAP)

    summary = RunSummary(stop_reason="done")
    solved: list[str] = []
    stops: list[str] = []
    submissions = 0
    evidence = EvidenceStore(config.work_dir / ".sapper" / "evidence.json")
    explorer = None
    if task.target_endpoints:
        target = TargetClient(EndpointScope.from_endpoints(task.target_endpoints))
        try:
            gateway = build_gateway(config.llm, usage)
            explorer = Explorer(
                gateway=gateway,
                target=target,
                task=task,
                readme=readme,
                evidence=evidence,
                usage=usage,
                work_dir=config.work_dir,
            )
        except GatewayError:
            stops.append("llm_unfunded")

    for _pass in range(max_passes):
        open_questions = [q for q in questions if not ledger.question_closed(q.id)]
        if not open_questions:
            break

        if explorer is not None:
            candidates, explore_stop = explorer.run([q.id for q in open_questions])
            stops.append(explore_stop)
            answers = [
                AnswerCandidate(
                    question_id=c["question_id"],
                    value=c["value"],
                    confidence=c["confidence"],
                    evidence_ids=c["evidence_ids"],
                    source_turn=usage.calls,
                )
                for c in (candidates or [])
            ]
            if not answers:
                break
        else:
            answers = _one_model_pass(config, task, readme, open_questions, usage, stops)
            if answers is None:
                break

        progress = False
        for candidate in answers:
            submissions += 1
            outcome = _spend(ledger, client, task, candidate, stops)
            if outcome == "correct":
                progress = True
                solved.append(candidate.question_id)
                refreshed = _refresh_questions(client, task)
                if refreshed:
                    questions = refreshed
                    # The ledger's visible set must follow the platform's,
                    # or a freshly unlocked question reads as invisible.
                    task = dataclasses.replace(
                        task, questions=tuple(refreshed)
                    )
                    ledger.reconcile(task)
                    if explorer is not None:
                        explorer.task = task  # same for the explorer's view
        if not progress:
            break

    summary.solved = solved
    summary.fully_solved = bool(solved) and all(
        ledger.question_closed(q.id) for q in questions
    )
    summary.turns = usage.calls
    summary.tool_calls_made = submissions
    summary.input_tokens = usage.input_tokens
    summary.output_tokens = usage.output_tokens
    summary.answers = ledger.fallback_answers()
    summary.writeup = _writeup(task, questions, solved, stops, usage, evidence)
    summary.stop_reason = stops[-1] if stops and not solved else "done"
    return summary


# --- model passes ------------------------------------------------------------


def _one_model_pass(
    config, task, readme, open_questions, usage, stops
) -> list[AnswerCandidate] | None:
    try:
        gateway = build_gateway(config.llm, usage)
    except GatewayError as exc:
        stops.append("llm_unfunded")
        return None

    template = (PROMPTS_DIR / "first_pass.md").read_text(encoding="utf-8")
    prompt = (
        template.replace("{min_confidence}", str(MIN_CONFIDENCE))
        .replace("{readme}", readme)
        .replace("{challenge}", _challenge_block(task))
        .replace("{questions}", _questions_block(open_questions))
    )
    request = LLMRequest(
        role="reason",
        system="You are a CTF solver. Output exactly one JSON object.",
        messages=({"role": "user", "content": prompt},),
        json_mode=True,
        turn_id=usage.calls + 1,
    )
    try:
        response = gateway.complete(request)
    except GatewayError as exc:
        stops.append("llm_error")
        return None

    try:
        payload = extract_json_object(response.text)
    except ModelParseError:
        # One repair attempt, counted like any other call.
        repair = LLMRequest(
            role="repair",
            system="Return exactly one JSON object and nothing else.",
            messages=(
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response.text},
                {
                    "role": "user",
                    "content": "That was not a valid JSON object. "
                    'Reply with {"answers": [...]} only.',
                },
            ),
            json_mode=True,
            turn_id=usage.calls + 1,
        )
        try:
            response = gateway.complete(repair)
            payload = extract_json_object(response.text)
        except (GatewayError, ModelParseError):
            stops.append("model_parse_error")
            return None

    candidates = []
    for item in payload.get("answers", []) or []:
        if not isinstance(item, dict):
            continue
        question_id = item.get("question_id")
        value = item.get("value")
        confidence = item.get("confidence", 0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        if not isinstance(question_id, str) or not isinstance(value, (str, list)):
            continue
        if confidence < MIN_CONFIDENCE:
            continue
        candidates.append(
            AnswerCandidate(
                question_id=question_id,
                value=value,
                confidence=confidence,
                source_turn=usage.calls,
            )
        )
    return candidates


def _spend(ledger, client, task, candidate, stops) -> str:
    """propose → validate → submit, tolerating every refusal."""
    try:
        entry_id = ledger.propose(candidate)
        ledger.mark_validated(entry_id)
    except LedgerError:
        return "refused"
    attempt = 0
    while True:
        attempt += 1
        try:
            entry = ledger.submit(entry_id)
        except LedgerQuarantined:
            return "unknown"
        except CtfyApiError as exc:
            if exc.paused and attempt <= 2:
                time.sleep(min(exc.retry_after_s or 30, PAUSED_SLEEP_CAP_S))
                continue
            stops.append("platform_error")
            return "rejected"
        except LedgerError:
            return "rejected"
        return "correct" if entry.status == "correct" else "wrong"


# --- platform reads -----------------------------------------------------------


def _refresh_questions(client: CtfyClient, task: TaskSnapshot) -> list[QuestionView] | None:
    """The visible question set after gating — the platform is the authority.

    Returns None when the read fails; the caller keeps its previous set.
    """
    try:
        data = client.get_challenge(task.challenge.id, task.competition_id)
    except CtfyApiError:
        return None
    raw_questions = data.get("questions")
    if not isinstance(raw_questions, list):
        return None
    questions = []
    for raw in raw_questions:
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            questions.append(
                QuestionView(
                    id=raw["id"], prompt=str(raw.get("prompt", "")), raw=dict(raw)
                )
            )
    return questions or None


def _read_readme(work_dir: Path) -> str:
    try:
        return (work_dir / "README.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _challenge_block(task: TaskSnapshot) -> str:
    lines = [f"id: {task.challenge.id}", f"name: {task.challenge.name}"]
    if task.challenge.description:
        lines.append(f"description: {task.challenge.description}")
    if task.rules_of_engagement:
        lines.append("rules_of_engagement:")
        lines.extend(f"  - {r}" for r in task.rules_of_engagement)
    if task.target_endpoints:
        lines.append("target_endpoints:")
        lines.extend(f"  - {e.type} {e.url}" for e in task.target_endpoints)
    return "\n".join(lines)


def _questions_block(questions) -> str:
    return "\n".join(f"- {q.id} — {q.prompt}" for q in questions)


def _writeup(task, questions, solved, stops, usage, evidence=None) -> str:
    lines = [
        f"Round on {task.challenge.id} ({task.run_mode}).",
        f"Solved: {', '.join(solved) if solved else 'nothing'}.",
        f"Model calls: {usage.calls} ({usage.input_tokens} in / "
        f"{usage.output_tokens} out tokens), {usage.retries} retries.",
    ]
    if evidence is not None and len(evidence):
        lines.append(f"Evidence gathered: {len(evidence)} observations.")
    if stops:
        lines.append("Stops along the way: " + ", ".join(sorted(set(stops))) + ".")
    if task.target_endpoints:
        lines.append(
            "Explored the published endpoints through the scoped target "
            "client; answers cite evidence ids."
        )
    return "\n".join(lines)
