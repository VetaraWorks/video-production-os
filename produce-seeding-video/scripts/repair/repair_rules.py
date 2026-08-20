"""Repair rules: map review/QA issue categories to repair action types (Phase 3)."""

from __future__ import annotations

from typing import Any


# Category -> action type. Categories that cannot be safely auto-fixed in v1
# map to "needs_human".
ISSUE_ACTION_MAP: dict[str, str] = {
    # Current review-contract categories.
    "subtitles": "fix_subtitle",
    "duplicate_shot": "replace_clip",
    # Backward-compatible aliases used by early Phase 3 fixtures.
    "subtitle_error": "fix_subtitle",
    "duplicate_clip": "replace_clip",
    "wrong_clip": "replace_clip",
    "semantic_alignment": "replace_clip",
    "continuity": "adjust_trim",
    "picture": "adjust_trim",
    "jump_frame": "needs_human",
    "freeze_frame": "needs_human",
    "music": "needs_human",
    "voiceover": "needs_human",
    "sound_effect": "needs_human",
    "cover": "needs_human",
}

SUPPORTED_ACTIONS = {"fix_subtitle", "replace_clip", "adjust_trim"}


def action_for_issue(category: str) -> str:
    return ISSUE_ACTION_MAP.get(str(category).strip(), "needs_human")


def validate_repair_plan(plan: dict[str, Any]) -> list[str]:
    """Return a list of schema violations for a repair_plan."""
    errors: list[str] = []
    if int(plan.get("schema_version", 0)) != 1:
        errors.append("schema_version must be 1")
    actions = plan.get("actions")
    if not isinstance(actions, list):
        errors.append("actions must be a list")
        return errors
    seen_ids: set[str] = set()
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(f"actions[{index}] must be an object")
            continue
        action_id = str(action.get("id") or "")
        if not action_id or action_id in seen_ids:
            errors.append(f"actions[{index}] needs a unique id")
        seen_ids.add(action_id)
        action_type = str(action.get("type") or "")
        if action_type not in SUPPORTED_ACTIONS:
            errors.append(f"actions[{index}] unsupported type: {action_type}")
        if action_type in ("replace_clip", "adjust_trim"):
            if not str(action.get("segment_id") or ""):
                errors.append(f"actions[{index}] needs segment_id")
            after = action.get("after")
            if not isinstance(after, dict):
                errors.append(f"actions[{index}] needs an after object")
            elif action_type == "replace_clip":
                if not str(after.get("source") or ""):
                    errors.append(f"actions[{index}] replace_clip.after needs source")
        if action_type == "fix_subtitle":
            if action.get("kind") not in ("text", "timing"):
                errors.append(f"actions[{index}] fix_subtitle.kind must be text|timing")
    return errors
