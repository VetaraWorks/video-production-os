"""Repair executor (Phase 3): apply repair_plan -> new edit_plan -> re-render -> QA -> version."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .repair_rules import SUPPORTED_ACTIONS, validate_repair_plan


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from video_pipeline.config import load_config  # noqa: E402
from video_os_core.version_manager import archive_repair_version  # noqa: E402


class RepairExecutionError(RuntimeError):
    """Raised when a repair action cannot be applied safely."""


class RepairQAError(RuntimeError):
    """Raised when re-render QA does not pass."""


def apply_repair_plan(
    project_dir: Path,
    repair_plan: dict[str, Any],
    config: dict[str, Any],
    projects_root: Path | None,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    run_pipeline: Callable[[Path, str | None, str | None], None] | None = None,
    verify_outputs: Callable[[Path, str | None, str | None], None] | None = None,
    version: str | None = None,
    defer_render: bool = False,
) -> dict[str, Any]:
    """Apply a validated repair_plan. Never mutates the original plan in place;
    writes a modified copy to config/edit_plan.json (existing v6 override) and
    archives the result as a new version snapshot."""
    project_dir = Path(project_dir).resolve()
    errors = validate_repair_plan(repair_plan)
    if errors:
        raise ValueError("Invalid repair plan: " + "; ".join(errors))
    actions = repair_plan.get("actions", [])
    if not actions:
        return {"applied": False, "reason": "no_actions", "version": version}

    plan_path = project_dir / "output" / "edit_plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError(f"edit_plan.json not found: {plan_path}")
    original_plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    if "base_video" in original_plan and "fullscreen_events" in original_plan:
        raise RepairExecutionError(
            "fullscreen plan repair is not supported in Phase 3"
        )

    plan_next = deepcopy(original_plan)
    changes: list[dict[str, Any]] = []
    script_changed = False
    timeline_changed = False
    segment_changed = False

    for action in actions:
        action_type = str(action.get("type"))
        if action_type not in SUPPORTED_ACTIONS:
            raise RepairExecutionError(f"Unsupported action type: {action_type}")
        if action_type in ("replace_clip", "adjust_trim"):
            segment = _find_segment(plan_next, str(action.get("segment_id")))
            before = _segment_snapshot(segment)
            after = _apply_segment_change(segment, action, project_dir)
            segment["selection"] = {
                "mode": "repair",
                "repair_plan_id": str(action.get("id") or ""),
                "reason": str(action.get("reason") or ""),
            }
            segment_changed = True
            changes.append(
                {
                    "action_id": action.get("id"),
                    "type": action_type,
                    "segment_id": segment.get("id"),
                    "before": before,
                    "after": after,
                    "reason": action.get("reason"),
                }
            )
        elif action_type == "fix_subtitle":
            detail = _apply_subtitle_fix(project_dir, action)
            if detail.get("script_changed"):
                script_changed = True
            if detail.get("timeline_changed"):
                timeline_changed = True
            changes.append(
                {
                    "action_id": action.get("id"),
                    "type": action_type,
                    "segment_id": action.get("segment_id"),
                    "detail": detail,
                    "reason": action.get("reason"),
                }
            )

    if segment_changed:
        repair_dir = project_dir / "repair"
        repair_dir.mkdir(parents=True, exist_ok=True)
        (repair_dir / "plan_next.json").write_text(
            json.dumps(plan_next, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        override = project_dir / "config" / "edit_plan.json"
        if override.exists() and not (project_dir / "config" / "edit_plan.pre-repair.json").exists():
            shutil.copy2(override, project_dir / "config" / "edit_plan.pre-repair.json")
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(
            json.dumps(plan_next, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        changes.append(
            {
                "action_id": "system",
                "type": "write_override",
                "detail": "wrote config/edit_plan.json override for v6 pipeline",
            }
        )

    if not (segment_changed or script_changed or timeline_changed):
        return {"applied": False, "reason": "no_effective_changes", "version": version}

    diff_payload = {
        "schema_version": 1,
        "project": project_dir.name,
        "changes": changes,
        "script_changed": script_changed,
        "timeline_changed": timeline_changed,
        "plan_changed": segment_changed,
    }
    repair_dir = project_dir / "repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    (repair_dir / "repair_diff.json").write_text(
        json.dumps(diff_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (repair_dir / "repair_plan.json").write_text(
        json.dumps(repair_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if defer_render:
        # The Director owns RENDER and QA. Make the repaired plan immediately
        # executable, then return the earliest stage that must be revisited.
        if segment_changed:
            plan_path.write_text(
                json.dumps(plan_next, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        rerun_from = (
            "ANALYZE"
            if script_changed
            else "PLAN"
            if timeline_changed
            else "RENDER"
        )
        return {
            "applied": True,
            "deferred": True,
            "rerun_from": rerun_from,
            "change_count": len(changes),
            "plan_changed": segment_changed,
            "script_changed": script_changed,
            "timeline_changed": timeline_changed,
            "qa_ok": None,
            "diff_file": str(repair_dir / "repair_diff.json"),
        }

    rerun_from = (
        "ANALYZE"
        if script_changed
        else "PLAN"
        if timeline_changed
        else "RENDER"
    )
    if run_pipeline is None:
        _run_pipeline_command(project_dir, ffmpeg, ffprobe, rerun_from=rerun_from)
    else:
        run_pipeline(project_dir, ffmpeg, ffprobe)
    qa_path = project_dir / "output" / "qa_report.json"
    try:
        qa = json.loads(qa_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepairQAError(f"QA report missing or invalid after repair: {exc}") from exc
    if qa.get("ok") is not True:
        raise RepairQAError(
            "Repair QA failed: " + "; ".join(qa.get("errors", []) or ["unknown"])
        )
    if verify_outputs is not None:
        verify_outputs(project_dir, ffmpeg, ffprobe)

    project_name = str(repair_plan.get("project") or project_dir.name)
    if projects_root is None:
        raise ValueError("projects_root is required when repair rendering is not deferred")
    resolved_version = version or next_repair_version(projects_root, project_name)
    archive = archive_repair_version(
        project_dir,
        projects_root,
        project_name,
        resolved_version,
        repair_plan,
        diff_payload,
        qa_summary=qa,
    )
    return {
        "applied": True,
        "version": resolved_version,
        "snapshot_dir": archive["snapshot_dir"],
        "change_count": len(changes),
        "plan_changed": segment_changed,
        "script_changed": script_changed,
        "timeline_changed": timeline_changed,
        "qa_ok": True,
        "qa_errors": [],
        "diff_file": str(repair_dir / "repair_diff.json"),
    }


def _find_segment(plan: dict[str, Any], segment_id: str) -> dict[str, Any]:
    for segment in plan.get("segments", []):
        if str(segment.get("id")) == segment_id:
            return segment
    raise RepairExecutionError(f"segment not found in edit_plan: {segment_id}")


def _segment_snapshot(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": segment.get("source"),
        "source_start": segment.get("source_start"),
        "source_duration": segment.get("source_duration"),
        "duration": segment.get("duration"),
        "has_audio": segment.get("has_audio"),
        "loop": segment.get("loop"),
    }


def _apply_segment_change(
    segment: dict[str, Any],
    action: dict[str, Any],
    project_dir: Path,
) -> dict[str, Any]:
    after = action.get("after") or {}
    action_type = str(action.get("type"))
    source = str(after.get("source") or segment.get("source") or "")
    source_path = (project_dir / source).resolve()
    try:
        source_path.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise RepairExecutionError(
            f"replacement source escapes project directory: {source}"
        ) from exc
    if not source_path.is_file():
        raise RepairExecutionError(f"replacement source missing: {source}")

    try:
        source_start = float(after.get("source_start", segment.get("source_start", 0.0)))
        source_duration = float(
            after.get("source_duration", segment.get("source_duration", 0.0))
        )
        duration = float(after.get("duration", segment.get("duration", 0.0)))
    except (TypeError, ValueError) as exc:
        raise RepairExecutionError(f"invalid numeric trim values: {exc}") from exc
    if source_start < 0 or duration <= 0 or source_duration <= 0:
        raise RepairExecutionError(
            f"invalid trim: start={source_start}, duration={duration}, "
            f"source_duration={source_duration}"
        )
    if action_type == "adjust_trim" and str(after.get("source") or segment.get("source")) != str(
        segment.get("source")
    ):
        raise RepairExecutionError("adjust_trim must keep the same source clip")
    if source_start + duration > source_duration + 0.05:
        raise RepairExecutionError(
            f"trim exceeds source duration: start={source_start}, "
            f"need={duration}s but source has {source_duration}s"
        )

    has_audio = bool(after.get("has_audio", segment.get("has_audio", False)))
    loop = bool(after.get("loop", source_duration + 0.02 < duration))
    segment["source"] = source
    segment["source_start"] = round(source_start, 3)
    segment["source_duration"] = round(source_duration, 3)
    segment["duration"] = round(duration, 3)
    segment["has_audio"] = has_audio
    segment["loop"] = loop
    return {
        "source": source,
        "source_start": segment["source_start"],
        "source_duration": segment["source_duration"],
        "duration": segment["duration"],
        "has_audio": has_audio,
        "loop": loop,
    }


def _apply_subtitle_fix(project_dir: Path, action: dict[str, Any]) -> dict[str, Any]:
    kind = str(action.get("kind"))
    detail: dict[str, Any] = {"kind": kind, "script_changed": False, "timeline_changed": False}
    if kind == "text":
        text_from = str(action.get("text_from") or "")
        text_to = str(action.get("text_to") or "")
        script_path = project_dir / "script" / "script.txt"
        if not text_from or not text_to or text_from == text_to:
            raise RepairExecutionError("fix_subtitle text requires text_from and text_to")
        if not script_path.is_file():
            raise RepairExecutionError("script.txt missing")
        content = script_path.read_text(encoding="utf-8-sig")
        if text_from not in content:
            raise RepairExecutionError(
                f"fix_subtitle text_from not found in script.txt: {text_from}"
            )
        updated = content.replace(text_from, text_to, 1)
        script_path.write_text(updated, encoding="utf-8")
        detail["script_changed"] = True
        detail["text_from"] = text_from
        detail["text_to"] = text_to
        return detail

    # timing
    timeline_path = project_dir / "speech_timeline.json"
    if not timeline_path.is_file():
        raise RepairExecutionError(
            "fix_subtitle timing requires speech_timeline.json in the project"
        )
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepairExecutionError(f"invalid speech_timeline.json: {exc}") from exc
    cues = timeline.get("cues")
    if not isinstance(cues, list) or not cues:
        raise RepairExecutionError("speech_timeline.json has no cues")
    cue_index = action.get("cue_index")
    new_start = action.get("new_start")
    new_end = action.get("new_end")
    shift = action.get("shift_seconds")
    if cue_index is not None:
        try:
            cue = cues[int(cue_index)]
        except (TypeError, ValueError, IndexError) as exc:
            raise RepairExecutionError(f"cue_index out of range: {cue_index}") from exc
    else:
        cue = None
    if cue is None:
        raise RepairExecutionError("fix_subtitle timing requires cue_index")
    before_start = float(cue.get("start", 0))
    before_end = float(cue.get("end", before_start))
    if shift is not None:
        shift_value = float(shift)
        new_start = before_start + shift_value
        new_end = before_end + shift_value
    if new_start is None or new_end is None:
        raise RepairExecutionError("fix_subtitle timing requires new_start/new_end or shift_seconds")
    new_start = float(new_start)
    new_end = float(new_end)
    if new_start < 0 or new_end <= new_start:
        raise RepairExecutionError(
            f"invalid subtitle timing: {new_start}-{new_end}"
        )
    cue["start"] = round(new_start, 3)
    cue["end"] = round(new_end, 3)
    timeline_path.write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    detail["timeline_changed"] = True
    detail["before"] = {"start": before_start, "end": before_end}
    detail["after"] = {"start": new_start, "end": new_end}
    return detail


def _run_pipeline_command(
    project_dir: Path,
    ffmpeg: str | None,
    ffprobe: str | None,
    *,
    rerun_from: str = "RENDER",
) -> None:
    stage_order = ("ANALYZE", "PLAN", "RENDER", "QA")
    if rerun_from not in stage_order:
        raise RepairExecutionError(f"invalid repair rerun stage: {rerun_from}")
    for stage in stage_order[stage_order.index(rerun_from) :]:
        command = [
            sys.executable,
            str(SCRIPT_DIR / "run_pipeline.py"),
            str(project_dir),
            "--stage",
            stage.lower(),
        ]
        if ffmpeg:
            command += ["--ffmpeg", ffmpeg]
        if ffprobe:
            command += ["--ffprobe", ffprobe]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(SCRIPT_DIR),
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RepairExecutionError(
                f"pipeline {stage} after repair failed: {detail[-1500:]}"
            )


def next_repair_version(projects_root: Path, project_name: str) -> str:
    snapshots_dir = projects_root.resolve() / project_name / "snapshots"
    if not snapshots_dir.is_dir():
        return "v001"
    highest = 0
    for child in snapshots_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("v") and name[1:].isdigit():
            highest = max(highest, int(name[1:]))
    return f"v{highest + 1:03d}"
