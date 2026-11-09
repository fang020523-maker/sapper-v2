"""Single-challenge coordinator: one hosted round, start to result.

The entrypoint hands over a validated config and task snapshot; this
module owns everything between "the run is real" and "here is the
summary". The reasoning loop (reason → explore → verify over the fact
graph), the model gateway and the answer ledger plug in here as they
land; until then the coordinator reports honestly that no solver ran
rather than fabricating turns or answers.
"""
from __future__ import annotations

from app.config import RuntimeConfig
from ctfy.result import RunSummary
from ctfy.schema import TaskSnapshot


def run_round(config: RuntimeConfig, task: TaskSnapshot) -> RunSummary:
    """Run one hosted round against this round's challenge snapshot.

    S0 skeleton: the contract is validated, state restoration and the
    reasoning loop are not wired in yet. A summary with a truthful
    stop_reason and zero counters is the honest output of this stage.
    """
    return RunSummary(
        stop_reason="solver_not_implemented",
        writeup=(
            f"Challenge {task.challenge.id} ({task.run_mode}) loaded and "
            "validated, but this build has no solver yet: nothing was "
            "attempted and nothing was submitted."
        ),
    )
