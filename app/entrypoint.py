#!/usr/bin/env python3
"""Absolute-path container entrypoint (CTFY-0001 v3).

Started by the platform as ``ENTRYPOINT ["python", "/…/app/entrypoint.py"]``
with the working directory already at ``/ctfy/work``. This file owns the
run's shape and nothing else: read the environment, take the dry-run fast
path, hand a hosted run to the coordinator, and make sure ``result.json``
is written and the exit code tells the truth on every path out.

Exit codes (§3): 0 the run completed — solved or not; 2 the local config
or task contract is unusable; 70 an unexpected internal fault. A wrong
answer is never a crash, and a crash is never a success.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

# Run as a script (the platform's way), this file's directory is on
# sys.path but the repository root — where the `app` and `ctfy` packages
# live — is not. Add it before importing either.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import (  # noqa: E402
    ConfigError,
    from_env,
    validate_contract,
)
from app.coordinator import run_round  # noqa: E402
from ctfy.result import ResultWriter, RunSummary  # noqa: E402
from ctfy.schema import TaskError, load_task, readme_sha256  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_INTERNAL = 70


def main() -> int:
    writer: ResultWriter | None = None
    summary = RunSummary(stop_reason="starting")
    try:
        config = from_env()
        writer = ResultWriter(config.work_dir / "result.json")

        # Dry run: honour the flag without touching the platform — there
        # is no token, no model key and nothing to spend (§3.1). Writing
        # the file and exiting 0 is the whole obligation.
        if config.dry_run:
            writer.write(RunSummary.dry_run())
            return EXIT_OK

        task = load_task(config.work_dir)
        if task.dry_run:
            writer.write(RunSummary.dry_run())
            return EXIT_OK

        validate_contract(config, task)
        task = dataclasses.replace(
            task, raw_readme_sha256=readme_sha256(config.work_dir)
        )

        summary = run_round(config, task)
        writer.write(summary)
        return EXIT_OK

    except (ConfigError, TaskError) as exc:
        summary.stop_reason = "config_error"
        summary.with_safe_error(exc)
        _best_effort_write(writer, summary)
        print(f"[entrypoint] config/task error: {_safe(exc)}", file=sys.stderr)
        return EXIT_CONFIG
    except BaseException as exc:  # noqa: BLE001 — the run must end cleanly
        summary.stop_reason = "internal_error"
        summary.with_safe_error(exc)
        _best_effort_write(writer, summary)
        print(f"[entrypoint] internal error: {_safe(exc)}", file=sys.stderr)
        return EXIT_INTERNAL


def _safe(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text[:300]


def _best_effort_write(writer: ResultWriter | None, summary: RunSummary) -> None:
    if writer is None:
        return
    try:
        writer.write(summary)
    except Exception:  # noqa: BLE001 — never mask the original failure
        print("[entrypoint] could not write result.json", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
