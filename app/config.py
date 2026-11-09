"""Environment-derived run configuration (CTFY-0001 v3 §1.1).

Everything here comes from the container environment the platform injects
or from the files in the working directory — never from the model, and
never with a private fallback of our own. Credentials live in these
objects but never in their reprs, logs or state files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

KNOWN_FLAVORS = ("openai", "anthropic")


class ConfigError(Exception):
    """The environment cannot support a conforming run."""


@dataclass(frozen=True)
class LLMConfig:
    flavor: str | None = None  # openai | anthropic | None (unset — dry run)
    base_url: str | None = None
    model: str | None = None
    api_key: str = field(default=None, repr=False)

    @property
    def funded(self) -> bool:
        """A run is funded when the platform handed us a key to spend.

        An absent or empty key is a real state, not a bug: nothing funds
        this run. The caller must fail loudly rather than fall back.
        """
        return bool(self.api_key)

    def __repr__(self) -> str:  # keep the key out of any accidental print
        return (
            f"LLMConfig(flavor={self.flavor!r}, base_url={self.base_url!r}, "
            f"api_key=<{'set' if self.api_key else 'unset'}>, model={self.model!r})"
        )


@dataclass(frozen=True)
class RuntimeConfig:
    work_dir: Path
    platform_url: str
    challenge_id: str
    competition_id: str
    target_instance: str | None  # None on a qa run, by design
    max_turns: int | None  # None means no budget — never 0
    dry_run: bool
    llm: LLMConfig
    token: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return (
            f"RuntimeConfig(work_dir={str(self.work_dir)!r}, "
            f"platform_url={self.platform_url!r}, "
            f"challenge_id={self.challenge_id!r}, "
            f"competition_id={self.competition_id!r}, "
            f"target_instance={self.target_instance!r}, "
            f"max_turns={self.max_turns!r}, dry_run={self.dry_run!r}, "
            f"llm={self.llm!r}, token=<{'set' if self.token else 'unset'}>)"
        )


def from_env(environ: dict[str, str] | None = None) -> RuntimeConfig:
    """Build the run config. Raises ConfigError on unusable input.

    Presence rules that are the protocol's, not ours: CTFY_EVAL_DRY_RUN is
    set only on a check run; CTFY_EVAL_TARGET_INSTANCE is absent on a qa
    run; CTFY_EVAL_MAX_TURNS absent means no budget.
    """
    env = dict(environ if environ is not None else os.environ)

    work_dir_raw = env.get("CTFY_WORK_DIR") or os.getcwd()
    work_dir = Path(work_dir_raw)
    if not work_dir.is_dir():
        raise ConfigError(f"CTFY_WORK_DIR is not a directory: {work_dir}")

    dry_raw = env.get("CTFY_EVAL_DRY_RUN", "")
    dry_run = dry_raw == "1"
    if dry_raw not in ("", "1"):
        raise ConfigError(
            f"CTFY_EVAL_DRY_RUN must be '1' or unset, got {dry_raw!r}"
        )

    max_turns_raw = env.get("CTFY_EVAL_MAX_TURNS")
    max_turns: int | None = None
    if max_turns_raw:
        try:
            max_turns = int(max_turns_raw)
        except ValueError:
            raise ConfigError(
                f"CTFY_EVAL_MAX_TURNS must be an int, got {max_turns_raw!r}"
            ) from None
        if max_turns <= 0:
            raise ConfigError(
                f"CTFY_EVAL_MAX_TURNS must be positive, got {max_turns}"
            )

    llm = LLMConfig(
        flavor=(env.get("LLM_FLAVOR") or None),
        base_url=(env.get("LLM_BASE_URL") or None),
        api_key=(env.get("LLM_API_KEY") or None),
        model=(env.get("LLM_MODEL") or None),
    )
    if llm.flavor is not None and llm.flavor not in KNOWN_FLAVORS:
        raise ConfigError(
            f"LLM_FLAVOR must be one of {KNOWN_FLAVORS}, got {llm.flavor!r}"
        )

    return RuntimeConfig(
        work_dir=work_dir,
        platform_url=(env.get("CTFY_PLATFORM_URL") or ""),
        challenge_id=(env.get("CTFY_EVAL_CHALLENGE_ID") or ""),
        competition_id=(env.get("CTFY_EVAL_COMPETITION_ID") or ""),
        target_instance=(env.get("CTFY_EVAL_TARGET_INSTANCE") or None),
        max_turns=max_turns,
        dry_run=dry_run,
        llm=llm,
        token=(env.get("CTFY_TOKEN") or ""),
    )


def validate_contract(config: RuntimeConfig, task) -> None:
    """Cross-check environment against this round's task.json.

    Raises ConfigError with a plain reason; the caller turns it into a
    written result and exit 2 so the run page can say what happened.
    """
    if config.challenge_id and task.challenge.id != config.challenge_id:
        raise ConfigError(
            f"challenge mismatch: env says {config.challenge_id!r}, "
            f"task.json says {task.challenge.id!r}"
        )
    if task.run_mode == "instance":
        if not config.target_instance:
            raise ConfigError(
                "instance run but CTFY_EVAL_TARGET_INSTANCE is absent"
            )
        if config.target_instance != task.target_instance:
            raise ConfigError(
                f"target mismatch: env says {config.target_instance!r}, "
                f"task.json says {task.target_instance!r}"
            )
    else:  # qa
        if config.target_instance:
            raise ConfigError(
                f"qa run but CTFY_EVAL_TARGET_INSTANCE is {config.target_instance!r}"
            )

    if config.dry_run or task.dry_run:
        # A dry run has no credentials and no platform: that is normal.
        return

    if not config.token:
        raise ConfigError("CTFY_TOKEN is absent on a hosted run")
    if not config.platform_url:
        raise ConfigError("CTFY_PLATFORM_URL is absent on a hosted run")
    if not config.llm.flavor:
        raise ConfigError("LLM_FLAVOR is absent on a hosted run")
    if not config.llm.funded:
        raise ConfigError(
            "LLM_API_KEY is absent or empty — nothing funds this run; "
            "refusing to fall back to any other key"
        )
    if not config.llm.model:
        raise ConfigError("LLM_MODEL is absent on a hosted run")
