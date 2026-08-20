"""Repair orchestration for Video OS (Phase 3): plan -> confirm -> apply -> record."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import project_manager as pm


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from repair.repair_executor import apply_repair_plan  # noqa: E402
from repair.repair_planner import plan_repair  # noqa: E402
from video_pipeline.config import load_config  # noqa: E402


class RepairNotNeededError(RuntimeError):
    """Raised when there is nothing to repair."""


class RepairNeedsHumanError(RuntimeError):
    """Raised when the existing deterministic Repair cannot safely act."""


def prepare_automatic_repair(
    project_dir: Path,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    """Plan and apply deterministic changes, leaving RENDER/QA to Director."""
    project_dir = Path(project_dir).expanduser().resolve()
    review = _load_optional(project_dir / "review" / "review.json")
    qa_report = _load_optional(project_dir / "output" / "qa_report.json")
    perception_before = _load_optional(project_dir / "perception" / "perception.json")
    plan_before = _load_optional(project_dir / "output" / "edit_plan.json")
    script_before = None
    script_path = project_dir / "script" / "script.txt"
    if script_path.is_file():
        script_before = script_path.read_text(encoding="utf-8-sig")
    timeline_before = _load_optional(project_dir / "speech_timeline.json")
    if review is None or review.get("verdict") != "fix":
        raise RepairNotNeededError("automatic repair requires review verdict=fix")

    config = load_config(project_dir)
    review_ok, review_errors = pm.artifact_valid(
        project_dir,
        "REVIEW",
        config,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    if not review_ok:
        raise RepairNeedsHumanError(
            "review validation failed: " + "; ".join(review_errors)
        )
    repair_plan = plan_repair(project_dir, review, qa_report, config)
    repair_dir = project_dir / "repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    plan_path = repair_dir / "repair_plan.json"
    plan_path.write_text(
        json.dumps(repair_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    unresolved = [str(item) for item in repair_plan.get("needs_human", [])]
    if unresolved:
        raise RepairNeedsHumanError("; ".join(unresolved))
    if not repair_plan.get("actions"):
        raise RepairNeedsHumanError("review requested fixes but produced no safe repair actions")

    result = apply_repair_plan(
        project_dir,
        repair_plan,
        config,
        None,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        defer_render=True,
    )
    if not result.get("applied"):
        raise RepairNeedsHumanError(
            f"repair produced no effective changes: {result.get('reason') or 'unknown'}"
        )
    if result.get("plan_changed"):
        from .planner_memory import record_post_plan_repair

        plan_after = _load_optional(project_dir / "output" / "edit_plan.json")
        repair_diff_for_plan = _load_optional(project_dir / "repair" / "repair_diff.json")
        if plan_before is None or plan_after is None or repair_diff_for_plan is None:
            raise RepairNeedsHumanError(
                "Repair changed edit_plan but Planner Memory repair provenance is incomplete"
            )
        record_post_plan_repair(
            project_dir,
            plan_before,
            plan_after,
            repair_diff_for_plan,
        )
    evidence_capture: dict[str, Any]
    try:
        from .production_evidence import capture_observed_repair

        repair_diff = _load_optional(project_dir / "repair" / "repair_diff.json")
        if perception_before is None or plan_before is None or repair_diff is None:
            missing = []
            if perception_before is None:
                missing.append("perception/perception.json")
            if plan_before is None:
                missing.append("output/edit_plan.json")
            if repair_diff is None:
                missing.append("repair/repair_diff.json")
            raise ValueError("missing evidence source(s): " + ", ".join(missing))
        captured = capture_observed_repair(
            project_dir,
            review_before=review,
            qa_before=qa_report,
            perception_before=perception_before,
            plan_before=plan_before,
            repair_plan=repair_plan,
            repair_diff=repair_diff,
            script_before=script_before,
            timeline_before=timeline_before,
        )
        evidence_capture = {
            "ok": True,
            "status": "observed",
            "evidence_id": captured["record"]["evidence_id"],
            "path": captured["path"],
            "reused": captured.get("reused", False),
            "warning": None,
        }
    except Exception as exc:  # Evidence capture must not falsify video production failure.
        evidence_capture = {
            "ok": False,
            "status": "capture_failed",
            "evidence_id": None,
            "path": None,
            "reused": False,
            "warning": f"Repair applied, but production evidence capture failed: {exc}",
        }
    return {
        **result,
        "repair_plan": repair_plan,
        "plan_file": str(plan_path),
        "evidence_capture": evidence_capture,
    }


def repair_project(
    project_dir: Path,
    projects_root: Path,
    *,
    apply: bool = False,
    confirm: Callable[[], bool] | None = None,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    project_dir = Path(project_dir).expanduser().resolve()
    state = pm.ensure_project_state(project_dir)
    review = _load_optional(project_dir / "review" / "review.json")
    qa_report = _load_optional(project_dir / "output" / "qa_report.json")

    if review is None and qa_report is None:
        raise RepairNotNeededError("no review.json or qa_report.json found to repair")
    if review is None and qa_report.get("ok") is True:
        raise RepairNotNeededError("qa_report is ok and no review.json; nothing to repair")
    if review is not None and review.get("verdict") == "pass":
        raise RepairNotNeededError("review verdict is pass; nothing to repair")

    config = load_config(project_dir)
    if review is not None:
        review_ok, review_errors = pm.artifact_valid(
            project_dir,
            "REVIEW",
            config,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        if not review_ok:
            raise RepairNeedsHumanError(
                "review validation failed: " + "; ".join(review_errors)
            )
    repair_plan = plan_repair(project_dir, review, qa_report, config)
    repair_dir = project_dir / "repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    plan_path = repair_dir / "repair_plan.json"
    plan_path.write_text(
        json.dumps(repair_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _record_history(state, "planned", len(repair_plan.get("actions", [])))

    if not apply:
        return {
            "ok": True,
            "mode": "planned",
            "project": project_dir.name,
            "repair_plan": repair_plan,
            "plan_file": str(plan_path),
            "message": "run with --apply to confirm and execute",
        }

    unresolved = [str(item) for item in repair_plan.get("needs_human", [])]
    if unresolved:
        record = state["stages"]["REPAIR"]
        record["status"] = "needs_human"
        record["last_error"] = "; ".join(unresolved)
        state["blocked"] = {
            "kind": "needs_human",
            "stage": "REPAIR",
            "error": record["last_error"],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        pm.save_project_state(project_dir, state)
        return {
            "ok": False,
            "mode": "needs_human",
            "project": project_dir.name,
            "repair_plan": repair_plan,
            "message": record["last_error"],
        }

    if repair_plan.get("actions"):
        confirmed = confirm() if confirm is not None else _default_confirm(repair_plan)
        if not confirmed:
            _record_history(state, "aborted", 0)
            pm.save_project_state(project_dir, state)
            return {
                "ok": False,
                "mode": "aborted",
                "project": project_dir.name,
                "message": "repair aborted by user",
            }

    result = apply_repair_plan(
        project_dir,
        repair_plan,
        config,
        projects_root,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        verify_outputs=_verify_real_outputs,
    )
    if not result.get("applied"):
        return {
            "ok": True,
            "mode": "noop",
            "project": project_dir.name,
            "message": result.get("reason"),
        }

    _mark_verified_output_stages(project_dir, state, config)
    _mark_repair_done(project_dir, state, config)
    state["stages"]["REVIEW"]["required"] = True
    state["stages"]["REVIEW"]["status"] = "invalid"
    state["blocked"] = None
    pm.refresh_state_validity(
        project_dir,
        state,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    _record_history(
        state,
        "applied",
        len(repair_plan.get("actions", [])),
        version=result.get("version"),
    )
    pm.save_project_state(project_dir, state)
    # The plan is preserved in the archived version snapshot; remove the work-dir
    # marker so next_action falls back to the blocked-stage resolution.
    plan_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "mode": "applied",
        "project": project_dir.name,
        "version": result.get("version"),
        "snapshot_dir": result.get("snapshot_dir"),
        "change_count": result.get("change_count"),
        "qa_ok": result.get("qa_ok"),
        "plan_file": str(plan_path),
        "diff_file": result.get("diff_file"),
    }


def _load_optional(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _default_confirm(repair_plan: dict[str, Any]) -> bool:
    print("待执行修复：")
    for action in repair_plan.get("actions", []):
        print(
            f"  - [{action.get('id')}] {action.get('type')} "
            f"segment={action.get('segment_id')} reason={action.get('reason')}"
        )
    answer = input("确认执行修复？[y/N] ").strip().lower()
    return answer in ("y", "yes")


def _record_history(
    state: dict[str, Any],
    status: str,
    action_count: int,
    *,
    version: str | None = None,
) -> None:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "stage": "REPAIR",
        "status": status,
        "attempts": action_count,
        "error": None,
        "version": version,
    }
    state.setdefault("history", []).append(entry)
    if len(state["history"]) > 200:
        state["history"] = state["history"][-200:]


def _verify_real_outputs(
    project_dir: Path,
    ffmpeg: str | None,
    ffprobe: str | None,
) -> None:
    config = load_config(project_dir)
    errors: list[str] = []
    for stage in ("RENDER", "QA"):
        valid, stage_errors = pm.artifact_valid(
            project_dir,
            stage,
            config,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        if not valid:
            errors.extend(f"{stage}: {item}" for item in stage_errors)
    if errors:
        raise RuntimeError("repair output authenticity validation failed: " + "; ".join(errors))


def _mark_repair_done(
    project_dir: Path,
    state: dict[str, Any],
    config: dict[str, Any],
) -> None:
    record = state["stages"]["REPAIR"]
    files = pm.input_files(project_dir, "REPAIR", config)
    record["inputs"] = [path.relative_to(project_dir).as_posix() for path in files]
    record["input_fingerprint"] = pm.fingerprint_bundle(files, project_dir)
    record["artifacts"] = [
        path.relative_to(project_dir).as_posix()
        for path in pm.artifact_paths(project_dir, "REPAIR", config)
    ]
    record["status"] = "done"
    record["last_error"] = None
    record["missing_inputs"] = []


def _mark_verified_output_stages(
    project_dir: Path,
    state: dict[str, Any],
    config: dict[str, Any],
) -> None:
    """Record work already executed and independently verified by manual repair."""
    for stage in ("RENDER", "QA"):
        record = state["stages"][stage]
        files = pm.input_files(project_dir, stage, config)
        record["inputs"] = [path.relative_to(project_dir).as_posix() for path in files]
        record["input_fingerprint"] = pm.fingerprint_bundle(files, project_dir)
        record["artifacts"] = [
            path.relative_to(project_dir).as_posix()
            for path in pm.artifact_paths(project_dir, stage, config)
        ]
        record["status"] = "done"
        record["last_error"] = None
        record["missing_inputs"] = []
