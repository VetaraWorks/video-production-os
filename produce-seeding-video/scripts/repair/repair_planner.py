"""Repair planner (Phase 3): review/QA issues -> repair_plan.json (no file mutation)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .repair_rules import action_for_issue, validate_repair_plan


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from video_pipeline.config import load_config  # noqa: E402


def plan_repair(
    project_dir: Path,
    review: dict[str, Any] | None,
    qa_report: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a repair_plan from review.json / qa_report.json. Read-only."""
    project_dir = Path(project_dir).resolve()
    config = config or load_config(project_dir)
    plan_path = project_dir / "output" / "edit_plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"edit_plan.json not found: {plan_path}")
    edit_plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))

    if "base_video" in edit_plan and "fullscreen_events" in edit_plan:
        return {
            "schema_version": 1,
            "project": project_dir.name,
            "source_reports": _source_reports(review, qa_report),
            "actions": [],
            "needs_human": [
                "fullscreen plan repair is not supported in Phase 3; use the standard edit_plan pipeline"
            ],
        }

    issues = _collect_issues(review, qa_report)
    actions: list[dict[str, Any]] = []
    needs_human: list[str] = []
    picture_issues_covered_by_replace: list[tuple[str, str]] = []
    candidates = _perception_candidates(project_dir, config)
    analysis = _load_json(project_dir / "output" / "analysis.json") or {}
    media_by_path = {
        str(item.get("path")): item for item in analysis.get("media", []) if item.get("has_video")
    }
    used_fingerprints = _used_fingerprints(edit_plan)

    for index, issue in enumerate(issues, start=1):
        category = str(issue.get("category") or "").strip()
        action_type = action_for_issue(category)
        if action_type == "needs_human":
            needs_human.append(
                f"{category}: {issue.get('description') or issue.get('suggestion') or 'not auto-fixable in v1'}"
            )
            continue
        segment = _resolve_segment(edit_plan, issue)
        if segment is None:
            needs_human.append(
                f"{category}: cannot resolve segment for issue {issue.get('id') or index}"
            )
            continue

        if action_type == "fix_subtitle":
            action = _plan_fix_subtitle(issue)
            if action is None:
                needs_human.append(
                    f"subtitle_error: issue {issue.get('id') or index} lacks text_from/text_to or cue timing data"
                )
                continue
            action["id"] = f"repair-{index:03d}"
            action["segment_id"] = str(segment.get("id") or "")
            action["reason"] = issue.get("description") or issue.get("suggestion") or category
            actions.append(action)
            continue

        if action_type == "adjust_trim":
            action = _plan_adjust_trim(segment, issue)
            if action is None:
                message = (
                    f"adjust_trim: issue {issue.get('id') or index} "
                    "needs explicit suggested start/end"
                )
                if category == "picture":
                    # A deterministic whole-segment replacement planned for
                    # another issue on this exact segment also removes this
                    # picture defect. Do not invent trim values.
                    picture_issues_covered_by_replace.append(
                        (str(segment.get("id") or ""), message)
                    )
                else:
                    needs_human.append(message)
                continue
            action["id"] = f"repair-{index:03d}"
            action["segment_id"] = str(segment.get("id") or "")
            action["reason"] = issue.get("description") or issue.get("suggestion") or category
            actions.append(action)
            continue

        # replace_clip
        action = _plan_replace_clip(
            edit_plan,
            segment,
            issue,
            candidates,
            media_by_path,
            used_fingerprints,
            config,
        )
        if action is None:
            needs_human.append(
                f"{category}: no suitable replacement candidate for segment "
                f"{segment.get('id')} (issue {issue.get('id') or index})"
            )
            continue
        action["id"] = f"repair-{index:03d}"
        action["segment_id"] = str(segment.get("id") or "")
        action["reason"] = issue.get("description") or issue.get("suggestion") or category
        actions.append(action)

    replacement_segments = {
        str(action.get("segment_id") or "")
        for action in actions
        if action.get("type") == "replace_clip"
    }
    needs_human.extend(
        message
        for segment_id, message in picture_issues_covered_by_replace
        if segment_id not in replacement_segments
    )

    plan = {
        "schema_version": 1,
        "project": project_dir.name,
        "source_reports": _source_reports(review, qa_report),
        "actions": actions,
        "needs_human": needs_human,
    }
    errors = validate_repair_plan(plan)
    if errors:
        raise ValueError("Invalid repair plan: " + "; ".join(errors))
    return plan


def _source_reports(review: dict[str, Any] | None, qa_report: dict[str, Any] | None) -> list[str]:
    reports: list[str] = []
    if review is not None:
        reports.append("review.json")
    if qa_report is not None:
        reports.append("qa_report.json")
    return reports


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _collect_issues(
    review: dict[str, Any] | None,
    qa_report: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if review is not None and review.get("verdict") == "fix":
        issues.extend(review.get("issues", []) or [])
    if qa_report is not None and qa_report.get("ok") is not True:
        for item in qa_report.get("errors", []) or []:
            issues.append({"category": "qa_error", "description": str(item)})
    return issues


def _resolve_segment(edit_plan: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any] | None:
    segments = edit_plan.get("segments", [])
    segment_id = str(issue.get("segment_id") or issue.get("segment") or "").strip()
    if segment_id:
        for segment in segments:
            if str(segment.get("id")) == segment_id:
                return segment
        return None
    try:
        start = float(issue.get("start", -1))
        end = float(issue.get("end", -1))
    except (TypeError, ValueError):
        return None
    if start < 0 or end < start:
        return None
    for segment in segments:
        segment_start = float(segment.get("timeline_start", 0))
        segment_end = float(segment.get("timeline_end", segment_start))
        if segment_start <= start and end <= segment_end:
            return segment
        if segment_start <= start < segment_end:
            return segment
    return None


def _plan_fix_subtitle(issue: dict[str, Any]) -> dict[str, Any] | None:
    subtitle = issue.get("subtitle") if isinstance(issue.get("subtitle"), dict) else {}
    text_from = str(subtitle.get("text_from") or issue.get("text_from") or "").strip()
    text_to = str(subtitle.get("text_to") or issue.get("text_to") or "").strip()
    cue_index = subtitle.get("cue_index")
    new_start = subtitle.get("new_start")
    new_end = subtitle.get("new_end")
    shift = subtitle.get("shift_seconds")
    if text_from and text_to and text_from != text_to:
        return {"type": "fix_subtitle", "kind": "text", "text_from": text_from, "text_to": text_to}
    if cue_index is not None or new_start is not None or new_end is not None or shift is not None:
        return {
            "type": "fix_subtitle",
            "kind": "timing",
            "cue_index": cue_index,
            "new_start": new_start,
            "new_end": new_end,
            "shift_seconds": shift,
        }
    return None


def _plan_adjust_trim(segment: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any] | None:
    try:
        new_start = float(issue.get("new_start", issue.get("suggested_source_start")))
        duration = float(issue.get("new_duration", segment.get("duration")))
    except (TypeError, ValueError):
        return None
    if new_start is None or new_start < 0 or duration <= 0:
        return None
    return {
        "type": "adjust_trim",
        "after": {
            "source": str(segment.get("source")),
            "source_start": round(new_start, 3),
            "source_duration": round(float(segment.get("source_duration", 0)), 3),
            "duration": round(duration, 3),
        },
    }


def _plan_replace_clip(
    edit_plan: dict[str, Any],
    segment: dict[str, Any],
    issue: dict[str, Any],
    candidates: list[dict[str, Any]],
    media_by_path: dict[str, dict[str, Any]],
    used_fingerprints: set[str],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    segment_id = str(segment.get("id"))
    duration = float(segment.get("duration", 0))
    if duration <= 0:
        return None
    preferred = _preferred_tags(segment_id, config)
    current_source = str(segment.get("source"))
    current_fingerprint = str(
        (segment.get("selection") or {}).get("visual_fingerprint") or ""
    )
    eligible = [
        candidate
        for candidate in candidates
        if candidate["available_duration"] + 0.02 >= duration
        and candidate["visual_fingerprint"] not in used_fingerprints
        and candidate["visual_fingerprint"] != current_fingerprint
        and candidate["source"] != current_source
    ]
    if not eligible:
        return None
    preferred_set = set(preferred)

    def rank(candidate: dict[str, Any]) -> tuple[int, int, float, str, str]:
        tags = set(candidate.get("tags", []))
        match_count = len(preferred_set & tags)
        has_audio = 1 if (media_by_path.get(candidate["source"]) or {}).get("has_audio") else 0
        return (
            -match_count,
            -has_audio,
            -float(candidate["available_duration"]),
            str(candidate["source"]),
            str(candidate["segment_id"]),
        )

    selected = min(eligible, key=rank)
    source = str(selected["source"])
    media = media_by_path.get(source) or {}
    has_audio = bool(media.get("has_audio"))
    loop = float(selected["available_duration"]) + 0.02 < duration
    return {
        "type": "replace_clip",
        "after": {
            "source": source,
            "source_start": round(float(selected["safe_start"]), 3),
            "source_duration": round(float(media.get("duration", selected["source_duration"])), 3),
            "duration": round(duration, 3),
            "has_audio": has_audio,
            "loop": loop,
        },
        "candidate": {
            "perception_segment_id": selected.get("perception_segment_id"),
            "summary": selected.get("summary"),
            "safe_start": selected["safe_start"],
            "safe_end": selected["safe_end"],
            "confidence": selected.get("confidence"),
            "visual_fingerprint": selected["visual_fingerprint"],
            "tags": selected.get("tags", []),
        },
    }


def _preferred_tags(segment_id: str, config: dict[str, Any]) -> list[str]:
    for segment in config.get("template_segments", []):
        if str(segment.get("id")) == segment_id:
            return [str(tag) for tag in segment.get("preferred_tags", [])]
    return []


def _used_fingerprints(edit_plan: dict[str, Any]) -> set[str]:
    fingerprints: set[str] = set()
    for segment in edit_plan.get("segments", []):
        fingerprint = str((segment.get("selection") or {}).get("visual_fingerprint") or "")
        if fingerprint:
            fingerprints.add(fingerprint)
    return fingerprints


def _perception_candidates(project_dir: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    perception_config = config.get("perception", {})
    if not perception_config.get("enabled", True):
        return []
    relative = str(perception_config.get("path", "perception/perception.json"))
    path = project_dir / relative
    payload = _load_json(path)
    if not payload or payload.get("status") != "done":
        return []
    min_confidence = float(perception_config.get("minimum_confidence", 0.55))
    min_quality = float(perception_config.get("minimum_quality_score", 0.55))
    candidates: list[dict[str, Any]] = []
    for source in payload.get("sources", []):
        source_path = str(source.get("source"))
        for segment in source.get("segments", []):
            quality = segment.get("quality") or {}
            if not quality.get("usable", True):
                continue
            confidence = float(segment.get("confidence", 0.0))
            quality_score = float(quality.get("score", 0.0))
            if confidence < min_confidence or quality_score < min_quality:
                continue
            tags: set[str] = set()
            for field in ("semantic_tags", "subjects", "objects", "actions"):
                tags.update(str(value) for value in segment.get(field, []))
            safe_start = float(segment.get("safe_start", segment.get("start", 0)))
            safe_end = float(segment.get("safe_end", segment.get("end", safe_start)))
            candidates.append(
                {
                    "source": source_path,
                    "source_duration": float(source.get("duration", 0)),
                    "segment_id": str(segment.get("id")),
                    "perception_segment_id": str(segment.get("id")),
                    "safe_start": safe_start,
                    "safe_end": safe_end,
                    "available_duration": round(safe_end - safe_start, 3),
                    "visual_fingerprint": str(
                        segment.get("visual_fingerprint") or segment.get("id")
                    ),
                    "tags": sorted(tags),
                    "summary": str(segment.get("summary", "")),
                    "confidence": confidence,
                    "quality_score": quality_score,
                }
            )
    return candidates
