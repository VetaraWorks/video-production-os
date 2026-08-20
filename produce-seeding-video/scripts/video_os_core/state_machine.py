"""Project-level state machine for Video OS (Phase 2).

Defines the stage order, the whitelist of allowed transitions, and helpers to
validate or inspect stages. This module only describes the machine; execution
and persistence live in project_manager.py.
"""

from __future__ import annotations


STAGE_ORDER = (
    "INIT",
    "ANALYZE",
    "PERCEPTION",
    "PLAN",
    "RENDER",
    "QA",
    "REVIEW",
    "REPAIR",
    "JIANYING_EXPORT",
    "FINAL",
)

# Stages that are not mandatory in every run.
OPTIONAL_STAGES = {"PERCEPTION", "REVIEW", "REPAIR", "JIANYING_EXPORT"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INIT": {"ANALYZE"},
    "ANALYZE": {"PERCEPTION"},
    "PERCEPTION": {"PLAN"},
    "PLAN": {"RENDER"},
    "RENDER": {"QA"},
    "QA": {"REVIEW"},
    "REVIEW": {"REPAIR", "JIANYING_EXPORT", "FINAL"},
    # REPAIR may re-enter the smallest affected production stage. ANALYZE is
    # needed only when a deterministic subtitle text repair changes script.txt.
    "REPAIR": {"ANALYZE", "PLAN", "RENDER", "JIANYING_EXPORT", "FINAL"},
    "JIANYING_EXPORT": {"FINAL"},
    "FINAL": set(),
}

# Statuses that count as completed for "upstream is valid" checks.
DONE_STATES = {"done", "skipped"}

# Statuses that stop the pipeline and require human or external action.
BLOCKER_STATES = {"failed", "needs_human", "needs_login"}


class TransitionError(ValueError):
    """Raised when a stage transition is not allowed by the whitelist."""


def stage_index(stage: str) -> int:
    if stage not in STAGE_ORDER:
        raise ValueError(f"Unknown stage: {stage}")
    return STAGE_ORDER.index(stage)


def next_stage(stage: str) -> str | None:
    if stage not in STAGE_ORDER:
        raise ValueError(f"Unknown stage: {stage}")
    index = STAGE_ORDER.index(stage)
    if index + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[index + 1]


def validate_transition(
    current: str,
    target: str,
    *,
    allow_rewind: bool = False,
) -> bool:
    if current not in STAGE_ORDER or target not in STAGE_ORDER:
        raise TransitionError(f"Unknown stage: {current} -> {target}")
    if allow_rewind and stage_index(target) < stage_index(current):
        return True
    if target not in ALLOWED_TRANSITIONS[current]:
        raise TransitionError(
            f"Illegal stage transition: {current} -> {target}"
        )
    return True


def is_done(status: str) -> bool:
    return status in DONE_STATES


def is_blocker(status: str) -> bool:
    return status in BLOCKER_STATES
