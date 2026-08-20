"""Traceable production evidence for the Video OS Repair loop.

This module is deliberately downstream of production.  It records what the
Director actually did, but it never changes an edit plan, executes a repair,
approves a rule, or supplies Planner input.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .knowledge import _atomic_write_json, refresh_counts
from .knowledge_root import require_knowledge_root


SCHEMA_VERSION = 1
TIER_OBSERVED = "observed"
TIER_HUMAN_VERIFIED = "human_verified"
TIER_PRODUCTION_VERIFIED = "production_verified"
EVIDENCE_TIERS = frozenset(
    {TIER_OBSERVED, TIER_HUMAN_VERIFIED, TIER_PRODUCTION_VERIFIED}
)
NON_PROMOTABLE_TIERS = frozenset({"demo", "migrated_unverified"})
_PRODUCTION_WRITE_TOKEN = object()
EVIDENCE_DIR = Path("repair") / "evidence"
REQUIRED_GATE_REFERENCES = frozenset(
    {
        "perception_before",
        "plan_before",
        "review_before",
        "review_task_before",
        "review_result_before",
        "repair_plan",
        "repair_diff",
        "perception_after",
        "plan_after",
        "qa_after",
        "review_after",
        "review_task_after",
        "review_result_after",
        "video_before",
        "video_after",
    }
)


class EvidenceValidationError(ValueError):
    """Raised when an evidence record is incomplete or internally inconsistent."""


@dataclass(frozen=True)
class _ProductionGateApproval:
    material_digest: str
    checks: tuple[str, ...]
    approved_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _knowledge_material(record: dict[str, Any]) -> str:
    material = deepcopy(record)
    material.pop("knowledge_sync", None)
    return _canonical_json(material)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(f"evidence reference is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceValidationError(f"evidence reference must contain an object: {path}")
    return payload


def _source_signature(path: Path) -> dict[str, Any]:
    # Keep this import local so the evidence layer cannot become a Planner input.
    from video_pipeline.perception import source_signature

    return source_signature(path)


def _project_identity(project_dir: Path) -> tuple[str, str]:
    state_path = project_dir / "project_state.json"
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            value = json.loads(state_path.read_text(encoding="utf-8-sig"))
            state = value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            state = {}
    project_name = str(state.get("project") or project_dir.name).strip() or project_dir.name
    stable_seed = f"{project_name}\n{state.get('created_at') or project_name}"
    project_id = str(state.get("project_id") or "").strip()
    if not project_id:
        project_id = "project-" + _sha256_bytes(stable_seed.encode("utf-8"))[:16]
    return project_id, project_name


def _inside_project(project_dir: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise EvidenceValidationError(f"evidence reference path must be project-relative: {relative!r}")
    resolved = (project_dir / relative).resolve()
    try:
        resolved.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise EvidenceValidationError(f"evidence reference escapes project: {relative!r}") from exc
    return resolved


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp.{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _reference(
    project_dir: Path,
    bundle_dir: Path,
    label: str,
    source: Path,
    *,
    media: bool = False,
) -> dict[str, Any]:
    if not source.is_file():
        raise EvidenceValidationError(f"required evidence source is missing: {source}")
    suffix = source.suffix if media else ".json"
    destination = bundle_dir / "refs" / f"{label}{suffix}"
    _copy_file_atomic(source, destination)
    relative = destination.relative_to(project_dir).as_posix()
    project_id, _project_name = _project_identity(project_dir)
    result: dict[str, Any] = {
        "kind": "media" if media else "json",
        "path": relative,
        "uri": f"project://{project_id}/{relative}",
        "sha256": _sha256_file(destination),
    }
    if media:
        result["signature"] = _source_signature(destination)
    return result


def _reference_from_payload(
    project_dir: Path,
    bundle_dir: Path,
    label: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    destination = bundle_dir / "refs" / f"{label}.json"
    _atomic_write_json(destination, payload)
    relative = destination.relative_to(project_dir).as_posix()
    project_id, _project_name = _project_identity(project_dir)
    return {
        "kind": "json",
        "path": relative,
        "uri": f"project://{project_id}/{relative}",
        "sha256": _sha256_file(destination),
    }


def _issue_records(review: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, issue in enumerate(review.get("issues") or [], start=1):
        if not isinstance(issue, dict):
            continue
        issue_id = str(issue.get("id") or f"review-issue-{index:03d}")
        records.append(
            {
                "issue_id": issue_id,
                "category": str(issue.get("category") or ""),
                "severity": str(issue.get("severity") or ""),
                "segment_id": str(issue.get("segment_id") or issue.get("segment") or ""),
                "time_range": {
                    "start": issue.get("start"),
                    "end": issue.get("end"),
                },
                "description": str(issue.get("description") or ""),
                "suggestion": str(issue.get("suggestion") or ""),
            }
        )
    return records


def _durable_review_sources(
    project_dir: Path,
    review: dict[str, Any],
) -> tuple[Path, Path]:
    task_id = str(review.get("task_id") or "").strip()
    if not task_id:
        raise EvidenceValidationError("Review evidence has no durable task_id")
    matches = [
        path
        for path in (project_dir / "review" / "tasks").glob(f"*/{task_id}.json")
        if path.is_file()
    ]
    if len(matches) != 1:
        raise EvidenceValidationError(
            f"Review task {task_id} must have exactly one durable state file; found {len(matches)}"
        )
    result_path = project_dir / "review" / "results" / f"{task_id}.json"
    if not result_path.is_file():
        raise EvidenceValidationError(f"Review task {task_id} has no durable Provider result")
    return matches[0], result_path


def _changed_values(change: dict[str, Any]) -> tuple[Any, Any]:
    if "before" in change or "after" in change:
        return change.get("before"), change.get("after")
    detail = change.get("detail")
    if isinstance(detail, dict):
        return detail.get("before"), detail.get("after")
    return None, None


def _structured_action(
    change: dict[str, Any],
    plan_action: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    action_id = str(change.get("action_id") or "")
    if not action_id or action_id == "system":
        return None
    action_type = str(change.get("type") or plan_action.get("type") or "")
    before, after = _changed_values(change)
    try:
        issue_index = max(0, int(action_id.rsplit("-", 1)[-1]) - 1)
    except ValueError:
        issue_index = 0
    issue = issues[issue_index] if issue_index < len(issues) else None
    issue_refs = [str(issue["issue_id"])] if issue else []
    segment_id = str(change.get("segment_id") or plan_action.get("segment_id") or "")
    field = ""
    metric = ""
    operator = "replace"
    value: Any = after
    if action_type == "adjust_trim":
        field = "segment.duration"
        metric = "shot_duration_s"
        operator = "<="
        if isinstance(after, dict):
            value = after.get("duration")
    elif action_type == "replace_clip":
        field = "segment.source"
        if isinstance(after, dict):
            value = after.get("source")
    elif action_type == "fix_subtitle":
        kind = str(plan_action.get("kind") or "")
        field = "subtitle.text" if kind == "text" else "subtitle.time_range"
        metric = "subtitle_timing_s" if kind == "timing" else ""
        detail = change.get("detail") if isinstance(change.get("detail"), dict) else {}
        if kind == "text" and before is None and after is None:
            before = detail.get("text_from")
            after = detail.get("text_to")
            value = after
    time_range = (
        deepcopy(issue.get("time_range"))
        if issue
        else {"start": None, "end": None}
    )
    return {
        "action_id": action_id,
        "type": action_type,
        "issue_refs": issue_refs,
        "target": {"segment_id": segment_id, "time_range": time_range},
        "scope": {"kind": "segment" if segment_id else "time_range"},
        "field": field,
        "metric": metric,
        "operator": operator,
        "before": before,
        "after": after,
        "value": value,
        "reason": str(change.get("reason") or plan_action.get("reason") or ""),
        "parameters": {
            key: deepcopy(value)
            for key, value in plan_action.items()
            if key not in {"id", "type", "segment_id", "reason"}
        },
    }


def _normalise_actions(
    repair_plan: dict[str, Any],
    repair_diff: dict[str, Any],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    planned = {
        str(item.get("id") or ""): item
        for item in repair_plan.get("actions") or []
        if isinstance(item, dict)
    }
    actions: list[dict[str, Any]] = []
    for change in repair_diff.get("changes") or []:
        if not isinstance(change, dict):
            continue
        action_id = str(change.get("action_id") or "")
        action = _structured_action(change, planned.get(action_id, {}), issues)
        if action is not None:
            actions.append(action)
    return actions


def _tier_event(source: str | None, target: str, actor: str, reason: str) -> dict[str, Any]:
    return {
        "from": source,
        "to": target,
        "at": _now_iso(),
        "actor": actor,
        "reason": reason,
    }


def _transition_tier(
    record: dict[str, Any],
    target: str,
    *,
    actor: str,
    reason: str,
    approval: _ProductionGateApproval | None = None,
) -> dict[str, Any]:
    current = str(record.get("evidence_tier") or "")
    if current in NON_PROMOTABLE_TIERS:
        raise EvidenceValidationError(f"evidence tier {current!r} can never be upgraded")
    if target not in EVIDENCE_TIERS:
        raise EvidenceValidationError(f"unknown evidence tier: {target}")
    if current == target:
        return record
    allowed = {
        "": {TIER_OBSERVED},
        TIER_OBSERVED: {TIER_HUMAN_VERIFIED, TIER_PRODUCTION_VERIFIED},
        TIER_HUMAN_VERIFIED: {TIER_PRODUCTION_VERIFIED},
    }
    if target not in allowed.get(current, set()):
        raise EvidenceValidationError(f"illegal evidence tier transition: {current!r} -> {target!r}")
    if target == TIER_PRODUCTION_VERIFIED and approval is None:
        raise EvidenceValidationError("production_verified requires a Production Evidence Gate approval")
    record["evidence_tier"] = target
    event = _tier_event(current or None, target, actor, reason)
    if approval is not None:
        event["gate_material_digest"] = approval.material_digest
    record.setdefault("tier_history", []).append(event)
    record["updated_at"] = event["at"]
    return record


def capture_observed_repair(
    project_dir: Path,
    *,
    review_before: dict[str, Any],
    qa_before: dict[str, Any] | None,
    perception_before: dict[str, Any],
    plan_before: dict[str, Any],
    repair_plan: dict[str, Any],
    repair_diff: dict[str, Any],
    script_before: str | None = None,
    timeline_before: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an idempotent project-local evidence bundle before rerendering."""
    project_dir = Path(project_dir).resolve()
    final_path = project_dir / "output" / "final.mp4"
    if not final_path.is_file():
        raise EvidenceValidationError("automatic Repair evidence requires the real pre-repair final.mp4")
    project_id, project_name = _project_identity(project_dir)
    before_sha = _sha256_file(final_path)
    identity_material = {
        "project_id": project_id,
        "review_task_id": review_before.get("task_id"),
        "review_signature": (review_before.get("target") or {}).get("signature"),
        "before_sha256": before_sha,
        "repair_actions": repair_plan.get("actions") or [],
    }
    evidence_id = "evidence-" + _sha256_bytes(
        _canonical_json(identity_material).encode("utf-8")
    )[:20]
    bundle_dir = project_dir / EVIDENCE_DIR / evidence_id
    record_path = bundle_dir / "evidence.json"
    if record_path.is_file():
        existing = _read_json(record_path)
        if existing.get("evidence_id") != evidence_id:
            raise EvidenceValidationError(f"evidence identity collision: {record_path}")
        return {"ok": True, "reused": True, "record": existing, "path": str(record_path)}

    issues = _issue_records(review_before)
    actions = _normalise_actions(repair_plan, repair_diff, issues)
    if not issues or not actions:
        raise EvidenceValidationError("automatic Repair evidence needs structured issues and effective actions")
    review_task_before, review_result_before = _durable_review_sources(
        project_dir, review_before
    )
    refs = {
        "perception_before": _reference_from_payload(
            project_dir, bundle_dir, "perception_before", perception_before
        ),
        "plan_before": _reference_from_payload(project_dir, bundle_dir, "plan_before", plan_before),
        "review_before": _reference_from_payload(
            project_dir, bundle_dir, "review_before", review_before
        ),
        "review_task_before": _reference(
            project_dir, bundle_dir, "review_task_before", review_task_before
        ),
        "review_result_before": _reference(
            project_dir, bundle_dir, "review_result_before", review_result_before
        ),
        "repair_plan": _reference_from_payload(project_dir, bundle_dir, "repair_plan", repair_plan),
        "repair_diff": _reference_from_payload(project_dir, bundle_dir, "repair_diff", repair_diff),
        "video_before": _reference(
            project_dir, bundle_dir, "video_before", final_path, media=True
        ),
    }
    if qa_before is not None:
        refs["qa_before"] = _reference_from_payload(
            project_dir, bundle_dir, "qa_before", qa_before
        )
    action_types = {str(item.get("type") or "") for item in actions}
    if "fix_subtitle" in action_types:
        if script_before is None:
            raise EvidenceValidationError("subtitle Repair evidence requires pre-repair script text")
        refs["script_before"] = _reference_from_payload(
            project_dir,
            bundle_dir,
            "script_before",
            {"text": script_before},
        )
        if any(
            str((item.get("parameters") or {}).get("kind") or "") == "timing"
            for item in actions
        ):
            if timeline_before is None:
                raise EvidenceValidationError(
                    "subtitle timing evidence requires pre-repair speech_timeline.json"
                )
            refs["timeline_before"] = _reference_from_payload(
                project_dir,
                bundle_dir,
                "timeline_before",
                timeline_before,
            )
    run_id = "repair-run-" + _sha256_bytes(
        _canonical_json(identity_material).encode("utf-8")
    )[:16]
    now = _now_iso()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "evidence_kind": "automatic_repair",
        "project_id": project_id,
        "project": project_name,
        "source_identity": {
            "project_id": project_id,
            "project": project_name,
            "run_id": run_id,
            "review_task_id": str(review_before.get("task_id") or ""),
        },
        "evidence_tier": "",
        "tier_history": [],
        "created_at": now,
        "updated_at": now,
        "video": {
            "before": {
                "reference": "video_before",
                "signature": refs["video_before"]["signature"],
                "sha256": refs["video_before"]["sha256"],
            },
            "after": None,
        },
        "issues": issues,
        "actions": actions,
        "qa_result": None,
        "post_review_result": None,
        "verification": {"status": "awaiting_post_review", "gate": None, "errors": []},
        "provenance": {"references": refs, "chain_digest": None},
        "knowledge_sync": {"status": "pending", "message": None, "at": now},
    }
    _transition_tier(
        record,
        TIER_OBSERVED,
        actor="video-os-director",
        reason="real deterministic Repair applied; awaiting rerender QA and Review",
    )
    errors = validate_evidence_record(record, allow_incomplete_chain=True)
    if errors:
        raise EvidenceValidationError("; ".join(errors))
    _atomic_write_json(record_path, record)
    return {"ok": True, "reused": False, "record": record, "path": str(record_path)}


def _load_reference(project_dir: Path, reference: dict[str, Any]) -> tuple[Path, Any]:
    path = _inside_project(project_dir, str(reference.get("path") or ""))
    if not path.is_file():
        raise EvidenceValidationError(f"evidence reference is missing: {reference.get('path')}")
    digest = _sha256_file(path)
    if digest != reference.get("sha256"):
        raise EvidenceValidationError(f"evidence reference digest mismatch: {reference.get('path')}")
    if reference.get("kind") == "json":
        return path, _read_json(path)
    if reference.get("kind") == "media":
        signature = _source_signature(path)
        if signature != reference.get("signature"):
            raise EvidenceValidationError(f"evidence media signature mismatch: {reference.get('path')}")
        return path, None
    raise EvidenceValidationError(f"evidence reference kind is invalid: {reference.get('kind')!r}")


def _gate_material(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record.get("schema_version"),
        "evidence_id": record.get("evidence_id"),
        "evidence_kind": record.get("evidence_kind"),
        "project_id": record.get("project_id"),
        "source_identity": record.get("source_identity"),
        "video": record.get("video"),
        "issues": record.get("issues"),
        "actions": record.get("actions"),
        "qa_result": record.get("qa_result"),
        "post_review_result": record.get("post_review_result"),
        "planner_memory": record.get("planner_memory"),
        "references": (record.get("provenance") or {}).get("references"),
    }


def _plan_segment(plan: dict[str, Any], segment_id: str) -> dict[str, Any] | None:
    for segment in plan.get("segments") or []:
        if isinstance(segment, dict) and str(segment.get("id") or "") == segment_id:
            return segment
    return None


def _same_value(left: Any, right: Any) -> bool:
    if left == right:
        return True
    try:
        return abs(float(left) - float(right)) <= 1e-6
    except (TypeError, ValueError):
        return False


def _assert_segment_values(
    phase: str,
    segment: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    for field, value in expected.items():
        if field not in segment or not _same_value(segment.get(field), value):
            raise EvidenceValidationError(
                f"Repair {phase} value for {field!r} is not present in the referenced Plan"
            )


def _production_gate(
    project_dir: Path,
    record: dict[str, Any],
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> _ProductionGateApproval:
    errors = validate_evidence_record(record, allow_incomplete_chain=False)
    if errors:
        raise EvidenceValidationError("; ".join(errors))
    current_tier = str(record.get("evidence_tier") or "")
    if current_tier in NON_PROMOTABLE_TIERS:
        raise EvidenceValidationError(f"evidence tier {current_tier!r} can never be upgraded")
    if current_tier not in {TIER_OBSERVED, TIER_HUMAN_VERIFIED}:
        raise EvidenceValidationError(f"Production Evidence Gate cannot promote tier {current_tier!r}")
    current_project_id, _project_name = _project_identity(project_dir)
    if current_project_id != record.get("project_id"):
        raise EvidenceValidationError("evidence project_id does not match the current project")

    references = (record.get("provenance") or {}).get("references") or {}
    if set(REQUIRED_GATE_REFERENCES) - set(references):
        missing = sorted(set(REQUIRED_GATE_REFERENCES) - set(references))
        raise EvidenceValidationError("production evidence references are incomplete: " + ", ".join(missing))
    loaded: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    for label in sorted(REQUIRED_GATE_REFERENCES):
        ref = references.get(label)
        if not isinstance(ref, dict):
            raise EvidenceValidationError(f"production evidence reference {label} is invalid")
        paths[label], loaded[label] = _load_reference(project_dir, ref)

    planner_memory = record.get("planner_memory")
    if isinstance(planner_memory, dict) and planner_memory.get("memory_applied") is True:
        memory_labels = (
            "base_plan_after",
            "memory_context_after",
            "memory_application_after",
        )
        for label in memory_labels:
            ref = references.get(label)
            if not isinstance(ref, dict):
                raise EvidenceValidationError(
                    f"applied Planner Memory is missing provenance reference: {label}"
                )
            paths[label], loaded[label] = _load_reference(project_dir, ref)
        final_memory = loaded["plan_after"].get("memory") or {}
        expected_memory = {
            "base_plan_signature": planner_memory.get("base_plan_signature"),
            "memory_context_signature": planner_memory.get("memory_context_signature"),
            "memory_application_signature": planner_memory.get(
                "memory_application_signature"
            ),
            "applied_rules": planner_memory.get("applied_rules"),
        }
        for field, value in expected_memory.items():
            if final_memory.get(field) != value:
                raise EvidenceValidationError(
                    f"Planner Memory provenance {field} does not match Final Plan"
                )
        if not planner_memory.get("applied_rules"):
            raise EvidenceValidationError("Planner Memory provenance claims use without applied rules")
        if (
            (loaded["memory_context_after"].get("context_signature") or {}).get("sha256")
            != planner_memory.get("memory_context_signature")
        ):
            raise EvidenceValidationError("Planner Memory Context signature mismatch")
        if (
            (loaded["memory_application_after"].get("application_signature") or {}).get(
                "sha256"
            )
            != planner_memory.get("memory_application_signature")
        ):
            raise EvidenceValidationError("Planner Memory Application signature mismatch")

    current_after_sources = {
        "perception_after": project_dir / "perception" / "perception.json",
        "plan_after": project_dir / "output" / "edit_plan.json",
        "qa_after": project_dir / "output" / "qa_report.json",
        "review_after": project_dir / "review" / "review.json",
        "repair_plan": project_dir / "repair" / "repair_plan.json",
        "repair_diff": project_dir / "repair" / "repair_diff.json",
    }
    if isinstance(planner_memory, dict) and planner_memory.get("memory_applied") is True:
        current_after_sources.update(
            {
                "base_plan_after": project_dir / "output" / "edit_plan.base.json",
                "memory_context_after": project_dir / "output" / "memory_context.json",
                "memory_application_after": project_dir
                / "output"
                / "memory_application.json",
            }
        )
    for label, current_path in current_after_sources.items():
        if not current_path.is_file() or _sha256_file(current_path) != references[label].get("sha256"):
            raise EvidenceValidationError(
                f"current execution artifact does not match evidence reference: {label}"
            )

    before = (record.get("video") or {}).get("before") or {}
    after = (record.get("video") or {}).get("after") or {}
    before_ref = references["video_before"]
    after_ref = references["video_after"]
    if before.get("signature") != before_ref.get("signature") or before.get("sha256") != before_ref.get("sha256"):
        raise EvidenceValidationError("pre-repair video identity does not match its preserved artifact")
    if after.get("signature") != after_ref.get("signature") or after.get("sha256") != after_ref.get("sha256"):
        raise EvidenceValidationError("post-repair video identity does not match its preserved artifact")
    if before.get("sha256") == after.get("sha256"):
        raise EvidenceValidationError("Repair before/after videos have identical content")

    review_before = loaded["review_before"]
    review_after = loaded["review_after"]
    review_task_before = loaded["review_task_before"]
    review_task_after = loaded["review_task_after"]
    review_result_before = loaded["review_result_before"]
    review_result_after = loaded["review_result_after"]
    qa_after = loaded["qa_after"]
    if review_before.get("status") != "done" or review_before.get("verdict") != "fix":
        raise EvidenceValidationError("pre-repair Review must be a completed fix verdict")
    if (review_before.get("target") or {}).get("signature") != before.get("signature"):
        raise EvidenceValidationError("pre-repair Review targets another video signature")
    if qa_after.get("ok") is not True:
        raise EvidenceValidationError("post-repair QA did not pass")
    if review_after.get("status") != "done" or review_after.get("verdict") != "pass":
        raise EvidenceValidationError("post-repair Review did not pass")
    if (review_after.get("target") or {}).get("signature") != after.get("signature"):
        raise EvidenceValidationError("post-repair Review targets another video signature")
    for phase, review, task, provider_result in (
        ("pre-repair", review_before, review_task_before, review_result_before),
        ("post-repair", review_after, review_task_after, review_result_after),
    ):
        if task.get("status") != "done" or task.get("task_id") != review.get("task_id"):
            raise EvidenceValidationError(f"{phase} Review durable task identity/status mismatch")
        if task.get("target_signature") != (review.get("target") or {}).get("signature"):
            raise EvidenceValidationError(f"{phase} Review task targets another video signature")
        expected_result = (
            project_dir / "review" / "results" / f"{review.get('task_id')}.json"
        ).resolve()
        if Path(str(task.get("result_path") or "")).resolve() != expected_result:
            raise EvidenceValidationError(f"{phase} Review task result_path is not durable")
        if provider_result != review:
            raise EvidenceValidationError(f"{phase} Review Provider result does not match review.json")
    current_review_task, current_review_result = _durable_review_sources(
        project_dir, review_after
    )
    for label, current_path in (
        ("review_task_after", current_review_task),
        ("review_result_after", current_review_result),
    ):
        if _sha256_file(current_path) != references[label].get("sha256"):
            raise EvidenceValidationError(
                f"current execution artifact does not match evidence reference: {label}"
            )

    plan_by_phase: dict[str, dict[str, Any]] = {}
    for suffix in ("before", "after"):
        perception = loaded[f"perception_{suffix}"]
        plan = loaded[f"plan_{suffix}"]
        plan_by_phase[suffix] = plan
        expected_digest = (perception.get("input_signature") or {}).get("digest_sha256")
        actual_digest = (plan.get("perception") or {}).get("input_signature_digest")
        if not expected_digest or expected_digest != actual_digest:
            raise EvidenceValidationError(
                f"{suffix} Plan is not bound to its referenced Perception signature"
            )

    repair_plan = loaded["repair_plan"]
    repair_diff = loaded["repair_diff"]
    planned = {
        str(item.get("id") or ""): str(item.get("type") or "")
        for item in repair_plan.get("actions") or []
        if isinstance(item, dict)
    }
    changed = {
        str(item.get("action_id") or ""): item
        for item in repair_diff.get("changes") or []
        if isinstance(item, dict) and item.get("action_id") != "system"
    }
    issue_ids = {str(item.get("issue_id") or "") for item in record.get("issues") or []}
    for action in record.get("actions") or []:
        action_id = str(action.get("action_id") or "")
        if planned.get(action_id) != str(action.get("type") or "") or action_id not in changed:
            raise EvidenceValidationError(f"Repair action {action_id!r} is not bound to plan and diff")
        if not action.get("issue_refs") or not set(map(str, action["issue_refs"])).issubset(issue_ids):
            raise EvidenceValidationError(f"Repair action {action_id!r} has no valid Review issue reference")
        if action.get("before") == action.get("after"):
            raise EvidenceValidationError(f"Repair action {action_id!r} has identical before/after values")
        action_type = str(action.get("type") or "")
        if action_type in {"adjust_trim", "replace_clip"}:
            segment_id = str((action.get("target") or {}).get("segment_id") or "")
            before_segment = _plan_segment(plan_by_phase["before"], segment_id)
            after_segment = _plan_segment(plan_by_phase["after"], segment_id)
            if before_segment is None or after_segment is None:
                raise EvidenceValidationError(
                    f"Repair action {action_id!r} segment is missing from before/after Plan"
                )
            if not isinstance(action.get("before"), dict) or not isinstance(action.get("after"), dict):
                raise EvidenceValidationError(
                    f"Repair action {action_id!r} requires structured Plan before/after values"
                )
            _assert_segment_values("before", before_segment, action["before"])
            _assert_segment_values("after", after_segment, action["after"])
        elif action_type == "fix_subtitle":
            parameters = action.get("parameters") if isinstance(action.get("parameters"), dict) else {}
            kind = str(parameters.get("kind") or "")
            for label in ("script_before", "script_after"):
                if not isinstance(references.get(label), dict):
                    raise EvidenceValidationError(
                        f"Repair action {action_id!r} is missing {label} provenance"
                    )
                _path, loaded[label] = _load_reference(project_dir, references[label])
            script_before = str(loaded["script_before"].get("text") or "")
            script_after = str(loaded["script_after"].get("text") or "")
            current_script = project_dir / "script" / "script.txt"
            if not current_script.is_file() or current_script.read_text(encoding="utf-8-sig") != script_after:
                raise EvidenceValidationError("current script.txt does not match subtitle Repair evidence")
            if kind == "text":
                if str(action.get("before") or "") not in script_before:
                    raise EvidenceValidationError("subtitle before text is absent from pre-repair script")
                if str(action.get("after") or "") not in script_after or script_before == script_after:
                    raise EvidenceValidationError("subtitle after text is absent from changed post-repair script")
            elif kind == "timing":
                for label in ("timeline_before", "timeline_after"):
                    if not isinstance(references.get(label), dict):
                        raise EvidenceValidationError(
                            f"Repair action {action_id!r} is missing {label} provenance"
                        )
                    _path, loaded[label] = _load_reference(project_dir, references[label])
                try:
                    cue_index = int(parameters["cue_index"])
                    before_cue = loaded["timeline_before"]["cues"][cue_index]
                    after_cue = loaded["timeline_after"]["cues"][cue_index]
                except (KeyError, TypeError, ValueError, IndexError) as exc:
                    raise EvidenceValidationError("subtitle timing cue provenance is invalid") from exc
                if not isinstance(action.get("before"), dict) or not isinstance(action.get("after"), dict):
                    raise EvidenceValidationError("subtitle timing before/after must be structured")
                _assert_segment_values("subtitle timing before", before_cue, action["before"])
                _assert_segment_values("subtitle timing after", after_cue, action["after"])
                current_timeline = project_dir / "speech_timeline.json"
                if not current_timeline.is_file() or _sha256_file(current_timeline) != references["timeline_after"].get("sha256"):
                    raise EvidenceValidationError("current speech_timeline.json does not match Repair evidence")
            else:
                raise EvidenceValidationError(f"unsupported subtitle Repair kind: {kind!r}")

    final_path = project_dir / "output" / "final.mp4"
    if not final_path.is_file() or _sha256_file(final_path) != after.get("sha256"):
        raise EvidenceValidationError("current final.mp4 does not match post-repair evidence")
    if _source_signature(final_path) != after.get("signature"):
        raise EvidenceValidationError("current final.mp4 signature does not match post-repair evidence")

    # Reuse the existing independent authenticity checks (ffprobe + full decode).
    from video_pipeline.config import load_config
    from . import project_manager as pm

    config = load_config(project_dir)
    for stage in ("QA", "REVIEW"):
        valid, stage_errors = pm.artifact_valid(
            project_dir,
            stage,
            config,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        if not valid:
            raise EvidenceValidationError(
                f"current {stage} authenticity validation failed: " + "; ".join(stage_errors)
            )
    if isinstance(planner_memory, dict) and planner_memory.get("memory_applied") is True:
        from .planner_memory import validate_planner_memory_artifacts

        memory_errors = validate_planner_memory_artifacts(
            project_dir,
            config,
            verify_memory_source=False,
        )
        if memory_errors:
            raise EvidenceValidationError(
                "current Planner Memory provenance validation failed: "
                + "; ".join(memory_errors)
            )

    material_digest = _sha256_bytes(_canonical_json(_gate_material(record)).encode("utf-8"))
    checks = [
        "all_reference_hashes_match",
        "before_review_signature_matches",
        "repair_plan_diff_actions_match",
        "perception_plan_bindings_match",
        "before_after_media_differ",
        "qa_media_authenticity_passes",
        "post_review_signature_matches",
    ]
    if isinstance(planner_memory, dict) and planner_memory.get("memory_applied") is True:
        checks.append("planner_memory_provenance_matches")
    return _ProductionGateApproval(material_digest, tuple(checks), _now_iso())


def _attach_post_review(
    project_dir: Path,
    record: dict[str, Any],
) -> dict[str, Any]:
    evidence_id = str(record["evidence_id"])
    bundle_dir = project_dir / EVIDENCE_DIR / evidence_id
    refs = record.setdefault("provenance", {}).setdefault("references", {})
    sources = {
        "perception_after": project_dir / "perception" / "perception.json",
        "plan_after": project_dir / "output" / "edit_plan.json",
        "qa_after": project_dir / "output" / "qa_report.json",
        "review_after": project_dir / "review" / "review.json",
    }
    for label, source in sources.items():
        refs[label] = _reference(project_dir, bundle_dir, label, source)
    plan_after = _read_json(sources["plan_after"])
    memory = plan_after.get("memory") or {}
    if memory.get("memory_applied") is True:
        memory_sources = {
            "base_plan_after": project_dir / "output" / "edit_plan.base.json",
            "memory_context_after": project_dir / "output" / "memory_context.json",
            "memory_application_after": project_dir / "output" / "memory_application.json",
        }
        for label, source in memory_sources.items():
            refs[label] = _reference(project_dir, bundle_dir, label, source)
        record["planner_memory"] = {
            "memory_applied": True,
            "mode": memory.get("mode"),
            "base_plan_signature": memory.get("base_plan_signature"),
            "memory_context_signature": memory.get("memory_context_signature"),
            "memory_application_signature": memory.get("memory_application_signature"),
            "applied_rules": deepcopy(memory.get("applied_rules") or []),
            "final_plan_reference": "plan_after",
            "base_plan_reference": "base_plan_after",
            "context_reference": "memory_context_after",
            "application_reference": "memory_application_after",
        }
    else:
        record["planner_memory"] = {
            "memory_applied": False,
            "mode": memory.get("mode"),
            "applied_rules": [],
            "final_plan_reference": "plan_after",
        }
    if any(str(item.get("type") or "") == "fix_subtitle" for item in record.get("actions") or []):
        script_path = project_dir / "script" / "script.txt"
        if not script_path.is_file():
            raise EvidenceValidationError("post-repair script.txt is missing")
        refs["script_after"] = _reference_from_payload(
            project_dir,
            bundle_dir,
            "script_after",
            {"text": script_path.read_text(encoding="utf-8-sig")},
        )
        if any(
            str((item.get("parameters") or {}).get("kind") or "") == "timing"
            for item in record.get("actions") or []
        ):
            timeline_path = project_dir / "speech_timeline.json"
            refs["timeline_after"] = _reference(
                project_dir, bundle_dir, "timeline_after", timeline_path
            )
    final_path = project_dir / "output" / "final.mp4"
    refs["video_after"] = _reference(
        project_dir, bundle_dir, "video_after", final_path, media=True
    )
    qa = _read_json(sources["qa_after"])
    review = _read_json(sources["review_after"])
    review_task_after, review_result_after = _durable_review_sources(project_dir, review)
    refs["review_task_after"] = _reference(
        project_dir, bundle_dir, "review_task_after", review_task_after
    )
    refs["review_result_after"] = _reference(
        project_dir, bundle_dir, "review_result_after", review_result_after
    )
    record["video"]["after"] = {
        "reference": "video_after",
        "signature": refs["video_after"]["signature"],
        "sha256": refs["video_after"]["sha256"],
    }
    record["qa_result"] = {
        "ok": qa.get("ok") is True,
        "reference": "qa_after",
    }
    record["post_review_result"] = {
        "status": review.get("status"),
        "verdict": review.get("verdict"),
        "task_id": review.get("task_id"),
        "target_signature": (review.get("target") or {}).get("signature"),
        "reference": "review_after",
    }
    record["updated_at"] = _now_iso()
    return record


def finalize_repair_evidence(
    project_dir: Path,
    evidence_id: str,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    """Attach the current QA/Review and invoke the only production-tier Gate."""
    project_dir = Path(project_dir).resolve()
    record_path = project_dir / EVIDENCE_DIR / evidence_id / "evidence.json"
    record = _read_json(record_path)
    if record.get("evidence_tier") == TIER_PRODUCTION_VERIFIED:
        return {"ok": True, "reused": True, "record": record, "path": str(record_path)}
    try:
        record = _attach_post_review(project_dir, record)
        approval = _production_gate(
            project_dir,
            record,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
    except EvidenceValidationError as exc:
        record["verification"] = {
            "status": "rejected",
            "gate": None,
            "errors": [str(exc)],
            "checked_at": _now_iso(),
        }
        _atomic_write_json(record_path, record)
        return {"ok": False, "reused": False, "record": record, "path": str(record_path), "error": str(exc)}
    except (OSError, FileNotFoundError) as exc:
        record["verification"] = {
            "status": "rejected",
            "gate": None,
            "errors": [str(exc)],
            "checked_at": _now_iso(),
        }
        _atomic_write_json(record_path, record)
        return {"ok": False, "reused": False, "record": record, "path": str(record_path), "error": str(exc)}
    record["verification"] = {
        "status": "passed",
        "gate": {
            "name": "production_evidence_gate_v1",
            "material_digest": approval.material_digest,
            "checks": list(approval.checks),
            "approved_at": approval.approved_at,
        },
        "errors": [],
        "checked_at": approval.approved_at,
    }
    record["provenance"]["chain_digest"] = approval.material_digest
    _transition_tier(
        record,
        TIER_PRODUCTION_VERIFIED,
        actor="production-evidence-gate-v1",
        reason="signature-bound Repair chain passed QA, media authenticity, and post-Repair Review",
        approval=approval,
    )
    _atomic_write_json(record_path, record)
    return {"ok": True, "reused": False, "record": record, "path": str(record_path)}


def process_after_review(
    project_dir: Path,
    *,
    knowledge_root: Path | str | None = None,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict[str, Any]:
    """Finalize the newest observed Repair evidence; never raises into production."""
    project_dir = Path(project_dir).resolve()
    records = sorted(
        (project_dir / EVIDENCE_DIR).glob("*/evidence.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    pending = [path for path in records if _read_json(path).get("evidence_tier") != TIER_PRODUCTION_VERIFIED]
    if not pending:
        return {"ok": True, "status": "no_evidence", "warning": None}
    result = finalize_repair_evidence(
        project_dir,
        pending[0].parent.name,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    if result.get("ok"):
        sync = sync_verified_evidence(project_dir, knowledge_root=knowledge_root)
        return {
            "ok": sync.get("ok", False),
            "status": sync.get("status"),
            "evidence_id": result["record"]["evidence_id"],
            "warning": sync.get("warning"),
            "synced": sync.get("synced", 0),
        }
    # A fix verdict here is expected to lead to another Repair, not FINAL.
    verdict = (result.get("record", {}).get("post_review_result") or {}).get("verdict")
    warning = None if verdict == "fix" else str(result.get("error") or "evidence gate rejected")
    return {
        "ok": verdict == "fix",
        "status": "observed" if verdict == "fix" else "gate_rejected",
        "evidence_id": result.get("record", {}).get("evidence_id"),
        "warning": warning,
    }


def validate_evidence_record(
    record: dict[str, Any],
    *,
    allow_incomplete_chain: bool = False,
) -> list[str]:
    errors: list[str] = []
    if int(record.get("schema_version", 0)) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("evidence_id", "evidence_kind", "project_id", "project"):
        if not str(record.get(field) or "").strip():
            errors.append(f"{field} is required")
    tier = str(record.get("evidence_tier") or "")
    if tier not in EVIDENCE_TIERS and tier not in NON_PROMOTABLE_TIERS:
        errors.append(f"invalid evidence_tier: {tier!r}")
    identity = record.get("source_identity")
    if not isinstance(identity, dict) or not str(identity.get("project_id") or "") or not str(identity.get("run_id") or ""):
        errors.append("source_identity requires project_id and run_id")
    elif str(identity.get("project_id")) != str(record.get("project_id") or ""):
        errors.append("source_identity.project_id must match project_id")
    actions = record.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("actions must be a non-empty array")
    else:
        seen: set[str] = set()
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                errors.append(f"actions[{index}] must be an object")
                continue
            action_id = str(action.get("action_id") or "")
            if not action_id or action_id in seen:
                errors.append(f"actions[{index}] requires a unique action_id")
            seen.add(action_id)
            if not str(action.get("field") or action.get("metric") or ""):
                errors.append(f"actions[{index}] requires metric or field")
            if not str(action.get("operator") or ""):
                errors.append(f"actions[{index}] requires operator")
            if "before" not in action or "after" not in action or "value" not in action:
                errors.append(f"actions[{index}] requires before, after, and value")
            elif action.get("value") is None:
                errors.append(f"actions[{index}].value cannot be null")
            if action.get("before") == action.get("after"):
                errors.append(f"actions[{index}] before and after must differ")
            if not isinstance(action.get("scope"), dict) or not str((action.get("scope") or {}).get("kind") or ""):
                errors.append(f"actions[{index}] requires structured scope")
            target = action.get("target")
            if not isinstance(target, dict):
                errors.append(f"actions[{index}] requires structured segment/time target")
            else:
                segment_id = str(target.get("segment_id") or "")
                time_range = target.get("time_range")
                has_time = False
                if isinstance(time_range, dict):
                    try:
                        start = float(time_range.get("start"))
                        end = float(time_range.get("end"))
                        has_time = start >= 0 and end >= start
                    except (TypeError, ValueError):
                        has_time = False
                if not segment_id and not has_time:
                    errors.append(f"actions[{index}] target needs segment_id or valid time_range")
            if not str(action.get("reason") or ""):
                errors.append(f"actions[{index}] requires reason")
    references = (record.get("provenance") or {}).get("references")
    if not isinstance(references, dict):
        errors.append("provenance.references must be an object")
    if not allow_incomplete_chain:
        video = record.get("video")
        if not isinstance(video, dict) or not isinstance(video.get("before"), dict) or not isinstance(video.get("after"), dict):
            errors.append("production evidence requires before and after video identities")
        if not isinstance(record.get("qa_result"), dict):
            errors.append("production evidence requires qa_result")
        if not isinstance(record.get("post_review_result"), dict):
            errors.append("production evidence requires post_review_result")
        planner_memory = record.get("planner_memory")
        if isinstance(planner_memory, dict) and planner_memory.get("memory_applied") is True:
            if planner_memory.get("mode") != "advisory":
                errors.append("applied Planner Memory provenance must use advisory mode")
            if not isinstance(planner_memory.get("applied_rules"), list) or not planner_memory.get(
                "applied_rules"
            ):
                errors.append("applied Planner Memory provenance requires applied_rules")
    return errors


def validate_production_seal(record: dict[str, Any]) -> None:
    if record.get("evidence_tier") != TIER_PRODUCTION_VERIFIED:
        return
    gate = (record.get("verification") or {}).get("gate")
    if not isinstance(gate, dict) or gate.get("name") != "production_evidence_gate_v1":
        raise EvidenceValidationError("production_verified evidence has no unified Gate record")
    expected = _sha256_bytes(_canonical_json(_gate_material(record)).encode("utf-8"))
    if gate.get("material_digest") != expected:
        raise EvidenceValidationError("production evidence Gate material digest is invalid")
    history = record.get("tier_history") or []
    if not any(
        isinstance(item, dict)
        and item.get("to") == TIER_PRODUCTION_VERIFIED
        and item.get("gate_material_digest") == expected
        for item in history
    ):
        raise EvidenceValidationError("production evidence tier history has no matching Gate transition")


def write_evidence_record(
    knowledge_root: Path | str,
    record: dict[str, Any],
    *,
    _production_gate_token: object | None = None,
) -> dict[str, Any]:
    """Atomically and idempotently persist a validated record to repair_log/."""
    root = require_knowledge_root(knowledge_root)
    errors = validate_evidence_record(
        record,
        allow_incomplete_chain=record.get("evidence_tier") != TIER_PRODUCTION_VERIFIED,
    )
    if errors:
        raise EvidenceValidationError("; ".join(errors))
    if (
        record.get("evidence_tier") == TIER_PRODUCTION_VERIFIED
        and _production_gate_token is not _PRODUCTION_WRITE_TOKEN
    ):
        raise EvidenceValidationError(
            "production_verified Knowledge writes are restricted to the unified Production Evidence Gate"
        )
    validate_production_seal(record)
    evidence_id = str(record["evidence_id"])
    destination = root / "repair_log" / f"{evidence_id}.json"
    reused = False
    if destination.is_file():
        existing = _read_json(destination)
        if _knowledge_material(existing) == _knowledge_material(record):
            reused = True
        else:
            immutable_fields = ("evidence_id", "evidence_kind", "project_id", "source_identity")
            if any(existing.get(field) != record.get(field) for field in immutable_fields):
                raise EvidenceValidationError(f"evidence identity collision: {destination}")
            old_history = existing.get("tier_history") or []
            new_history = record.get("tier_history") or []
            if new_history[: len(old_history)] != old_history:
                raise EvidenceValidationError("evidence update does not preserve append-only tier history")
            _atomic_write_json(destination, record)
    else:
        _atomic_write_json(destination, record)
    manifest = refresh_counts(root)
    return {
        "ok": True,
        "path": str(destination),
        "evidence_id": evidence_id,
        "reused": reused,
        "manifest": str(root / "manifest.json"),
        "repair_log_count": manifest.get("counts", {}).get("repair_log", 0),
    }


def sync_verified_evidence(
    project_dir: Path,
    *,
    knowledge_root: Path | str | None = None,
) -> dict[str, Any]:
    """Sync local production records without making production depend on Knowledge."""
    project_dir = Path(project_dir).resolve()
    records = sorted((project_dir / EVIDENCE_DIR).glob("*/evidence.json"))
    verified = [path for path in records if _read_json(path).get("evidence_tier") == TIER_PRODUCTION_VERIFIED]
    if not verified:
        return {"ok": True, "status": "no_verified_evidence", "synced": 0, "warning": None}
    try:
        root = require_knowledge_root(knowledge_root)
    except Exception as exc:  # Knowledge status is auxiliary to video production.
        warning = f"production evidence retained locally but Knowledge is unavailable: {exc}"
        for path in verified:
            record = _read_json(path)
            record["knowledge_sync"] = {"status": "unavailable", "message": warning, "at": _now_iso()}
            _atomic_write_json(path, record)
        return {"ok": False, "status": "unavailable", "synced": 0, "warning": warning}
    synced = 0
    reused = 0
    try:
        for path in verified:
            record = _read_json(path)
            # Synchronization bookkeeping is local runtime state, not Gate material.
            knowledge_record = deepcopy(record)
            knowledge_record["knowledge_sync"] = {
                "status": "synced",
                "message": None,
                "at": _now_iso(),
            }
            result = write_evidence_record(
                root,
                knowledge_record,
                _production_gate_token=_PRODUCTION_WRITE_TOKEN,
            )
            reused += int(bool(result.get("reused")))
            synced += 1
            record["knowledge_sync"] = {
                "status": "synced",
                "message": None,
                "at": _now_iso(),
                "path": result["path"],
            }
            _atomic_write_json(path, record)
    except Exception as exc:
        warning = f"video completed, but production evidence Knowledge sync failed: {exc}"
        return {"ok": False, "status": "failed", "synced": synced, "warning": warning}
    return {
        "ok": True,
        "status": "synced",
        "synced": synced,
        "reused": reused,
        "warning": None,
        "knowledge_root": str(root),
    }


def create_manual_evidence(
    payload: dict[str, Any],
    *,
    reviewer: str,
    verification_reason: str,
) -> dict[str, Any]:
    """Build human-verified structured evidence; it is not production-verified."""
    reviewer = str(reviewer).strip()
    verification_reason = str(verification_reason).strip()
    if not reviewer or not verification_reason:
        raise EvidenceValidationError("manual evidence requires reviewer and verification_reason")
    project_id = str(payload.get("project_id") or "").strip()
    project = str(payload.get("project") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    actions = deepcopy(payload.get("actions"))
    if not project_id or not project or not run_id:
        raise EvidenceValidationError("manual evidence requires project_id, project, and run_id")
    identity_material = {
        "project_id": project_id,
        "run_id": run_id,
        "actions": actions,
        "provenance": payload.get("provenance") or {},
    }
    evidence_id = str(payload.get("evidence_id") or "").strip() or (
        "evidence-" + _sha256_bytes(_canonical_json(identity_material).encode("utf-8"))[:20]
    )
    now = _now_iso()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "evidence_kind": "manual_edit",
        "project_id": project_id,
        "project": project,
        "source_identity": {"project_id": project_id, "project": project, "run_id": run_id},
        "evidence_tier": "",
        "tier_history": [],
        "created_at": str(payload.get("timestamp") or now),
        "updated_at": now,
        "video": deepcopy(payload.get("video")),
        "issues": deepcopy(payload.get("issues") or []),
        "actions": actions,
        "qa_result": deepcopy(payload.get("qa_result")),
        "post_review_result": deepcopy(payload.get("post_review_result")),
        "verification": {"status": "human_verified", "gate": None, "errors": []},
        "provenance": deepcopy(payload.get("provenance") or {"references": {}}),
        "knowledge_sync": {"status": "pending", "message": None, "at": now},
    }
    record.setdefault("provenance", {}).setdefault("references", {})
    _transition_tier(record, TIER_OBSERVED, actor="manual-evidence-recorder", reason="structured manual edit observed")
    _transition_tier(
        record,
        TIER_HUMAN_VERIFIED,
        actor=reviewer,
        reason=verification_reason,
    )
    errors = validate_evidence_record(record, allow_incomplete_chain=True)
    if errors:
        raise EvidenceValidationError("; ".join(errors))
    return record


def record_manual_evidence(
    knowledge_root: Path | str,
    payload: dict[str, Any],
    *,
    reviewer: str,
    verification_reason: str,
) -> dict[str, Any]:
    record = create_manual_evidence(
        payload,
        reviewer=reviewer,
        verification_reason=verification_reason,
    )
    result = write_evidence_record(knowledge_root, record)
    result["evidence_tier"] = record["evidence_tier"]
    return result
