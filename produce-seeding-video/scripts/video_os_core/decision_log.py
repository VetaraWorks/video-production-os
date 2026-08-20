"""Memory decision feedback log for Video OS (Phase 5.2).

Records human decisions about memory suggestions:
    suggestion -> human decision (accepted/rejected/modified/deferred)
                  -> decision_log (append-only)

This layer intentionally has no production authority:
- It never modifies edit_plan, Planner, video_pipeline, or any rule.
- Decisions are future Memory training data only.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .knowledge import _atomic_write_json, refresh_counts
from .memory_suggestions import (
    generate_memory_suggestions,
    suggestion_content_hash,
    validate_suggestion_snapshot,
)


DECISION_SCHEMA_VERSION = 2
DECISION_TYPES = {
    "accept",
    "reject",
    "defer",
    "accepted",
    "rejected",
    "deferred",
    "modified",
}
DECISION_LOG_DIR = "decision_log"
GOVERNANCE_HISTORY_DIR = "governance_history"
DECISION_HASH_ALGORITHM = "video-os-memory-decision-v2"


class DecisionError(ValueError):
    """Raised when a decision record is invalid."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_decision(value: str) -> str:
    return {
        "accepted": "accept",
        "rejected": "reject",
        "deferred": "defer",
    }.get(value, value)


def _decision_material(record: dict[str, Any]) -> dict[str, Any]:
    material = deepcopy(record)
    material.pop("decision_hash", None)
    return material


def decision_log_dir(project_dir: Path) -> Path:
    return Path(project_dir).expanduser().resolve() / "memory_preview" / DECISION_LOG_DIR


def load_suggestion_map(project_dir: Path, knowledge_root: Path) -> dict[str, dict[str, Any]]:
    """Load current suggestions keyed by stable suggestion_id."""
    report = generate_memory_suggestions(project_dir, knowledge_root)
    return {str(item["suggestion_id"]): item for item in report.get("suggestions", [])}


def validate_decision(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(record.get("schema_version", 0)) != DECISION_SCHEMA_VERSION:
        errors.append(f"schema_version must be {DECISION_SCHEMA_VERSION}")
    for field in (
        "decision_id",
        "suggestion_id",
        "suggestion_hash",
        "rule_id",
        "rule_revision",
        "rule_content_hash",
        "project_id",
        "project_input_signature",
        "edit_plan_signature",
        "decision",
        "reason",
        "reviewer",
        "recorded_at",
    ):
        if not str(record.get(field) or "").strip():
            errors.append(f"{field} is required")
    if record.get("decision") not in DECISION_TYPES:
        errors.append(f"decision must be one of {sorted(DECISION_TYPES)}")
    reviewer = record.get("reviewer")
    if not isinstance(reviewer, dict) or reviewer.get("type") != "human":
        errors.append("reviewer.type must be human")
    if not str((reviewer or {}).get("name") or "").strip():
        errors.append("reviewer.name is required")
    if record.get("decision") == "modified" and record.get("modified_value") is None:
        errors.append("modified decision requires modified_value")
    original = record.get("original_suggestion")
    if not isinstance(original, dict) or not original.get("suggestion_id"):
        errors.append("original_suggestion.suggestion_id is required")
    elif suggestion_content_hash(original) != record.get("suggestion_hash"):
        errors.append("original_suggestion does not match suggestion_hash")
    decision_hash = record.get("decision_hash")
    if not isinstance(decision_hash, dict):
        errors.append("decision_hash is required")
    elif decision_hash.get("algorithm") != DECISION_HASH_ALGORITHM:
        errors.append("decision_hash algorithm is invalid")
    elif decision_hash.get("sha256") != _sha256(_decision_material(record)):
        errors.append("decision_hash does not match decision content")
    return errors


def _snapshot_suggestion(suggestion: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(suggestion, ensure_ascii=False))


def record_decision(
    project_dir: Path,
    knowledge_root: Path,
    *,
    suggestion_id: str,
    decision: str,
    reviewer: str,
    reason: str,
    modified_value: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Record one human decision about a suggestion. Append-only; the original
    suggestion snapshot is preserved and never modified."""
    if decision not in DECISION_TYPES:
        raise DecisionError(f"decision must be one of {sorted(DECISION_TYPES)}")
    if not reviewer.strip():
        raise DecisionError("reviewer is required")
    if not reason.strip():
        raise DecisionError("reason is required")
    if decision == "modified" and modified_value is None:
        raise DecisionError("modified decision requires --modified-value")

    suggestion_map = load_suggestion_map(project_dir, knowledge_root)
    suggestion = suggestion_map.get(suggestion_id)
    if suggestion is None:
        raise DecisionError(
            f"suggestion not found in current memory_suggestions: {suggestion_id}"
        )

    stale_errors = validate_suggestion_snapshot(
        project_dir, knowledge_root, suggestion
    )
    if stale_errors:
        raise DecisionError("suggestion is stale: " + "; ".join(stale_errors))

    binding = suggestion.get("binding") or {}
    suggestion_hash = str(
        (suggestion.get("suggestion_hash") or {}).get("sha256") or ""
    )
    if not suggestion_hash:
        raise DecisionError("current suggestion has no valid content hash")
    request_material = {
        "suggestion_id": suggestion_id,
        "suggestion_hash": suggestion_hash,
        "decision": decision,
        "reviewer": reviewer,
        "reason": reason,
        "modified_value": modified_value,
    }
    decision_id = "decision-" + _sha256(request_material)[:24]

    record = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decision_id": decision_id,
        "suggestion_id": suggestion_id,
        "suggestion_hash": suggestion_hash,
        "rule_id": suggestion.get("rule_id"),
        "rule_revision": suggestion.get("rule_revision"),
        "rule_content_hash": suggestion.get("rule_content_hash"),
        "project_id": suggestion.get("project_id"),
        "project": suggestion.get("project"),
        "version": suggestion.get("version"),
        "project_input_signature": binding.get("project_input_signature"),
        "edit_plan_signature": binding.get("edit_plan_signature"),
        "decision": decision,
        "decision_category": _canonical_decision(decision),
        "reason": reason,
        "modified_value": modified_value,
        "original_suggestion": _snapshot_suggestion(suggestion),
        "reviewer": {"type": "human", "name": reviewer},
        "recorded_at": _now_iso(),
    }
    record["decision_hash"] = {
        "algorithm": DECISION_HASH_ALGORITHM,
        "sha256": _sha256(_decision_material(record)),
    }
    errors = validate_decision(record)
    if errors:
        raise DecisionError("Invalid decision record: " + "; ".join(errors))

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "decision": record,
            "message": "dry-run: nothing written",
        }

    log_dir = decision_log_dir(project_dir)
    history_dir = Path(knowledge_root).expanduser().resolve() / GOVERNANCE_HISTORY_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{decision_id}.json"
    history_path = history_dir / f"{decision_id}.json"

    existing: dict[str, Any] | None = None
    for candidate_path in (path, history_path):
        if not candidate_path.is_file():
            continue
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DecisionError(f"existing decision record is unreadable: {candidate_path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("decision_id") != decision_id:
            raise DecisionError(f"decision identity collision: {candidate_path}")
        if existing is None:
            existing = payload
        elif existing != payload:
            raise DecisionError("project and Knowledge decision history disagree")
    if existing is not None:
        existing_request = {
            "suggestion_id": existing.get("suggestion_id"),
            "suggestion_hash": existing.get("suggestion_hash"),
            "decision": existing.get("decision"),
            "reviewer": (existing.get("reviewer") or {}).get("name"),
            "reason": existing.get("reason"),
            "modified_value": existing.get("modified_value"),
        }
        if existing_request != request_material:
            raise DecisionError("decision identity collision with different request")
        if not path.is_file():
            _atomic_write_json(path, existing)
        if not history_path.is_file():
            _atomic_write_json(history_path, existing)
            refresh_counts(knowledge_root)
        return {
            "ok": True,
            "dry_run": False,
            "idempotent": True,
            "path": str(path),
            "governance_history_path": str(history_path),
            "decision": existing,
        }

    # Knowledge history is written first. A retry completes a missing project
    # copy idempotently if the second atomic write is interrupted.
    _atomic_write_json(history_path, record)
    _atomic_write_json(path, record)
    refresh_counts(knowledge_root)
    return {
        "ok": True,
        "dry_run": False,
        "idempotent": False,
        "path": str(path),
        "governance_history_path": str(history_path),
        "decision": record,
    }


def list_decisions(project_dir: Path) -> dict[str, Any]:
    log_dir = decision_log_dir(project_dir)
    records: list[dict[str, Any]] = []
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                records.append(payload)
    records.sort(
        key=lambda item: (
            str(item.get("recorded_at") or ""),
            str(item.get("decision_id") or ""),
        )
    )
    return {
        "ok": True,
        "decision_count": len(records),
        "decisions": records,
    }


def list_governance_history(
    knowledge_root: Path,
    *,
    rule_id: str | None = None,
) -> dict[str, Any]:
    """Read-only per-rule effectiveness/usage statistics; never learns thresholds."""
    history_dir = Path(knowledge_root).expanduser().resolve() / GOVERNANCE_HISTORY_DIR
    records: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    if history_dir.is_dir():
        for path in sorted(history_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                invalid.append({"file": path.name, "errors": [str(exc)]})
                continue
            if not isinstance(payload, dict):
                invalid.append({"file": path.name, "errors": ["not an object"]})
                continue
            errors = validate_decision(payload)
            if errors:
                invalid.append({"file": path.name, "errors": errors})
                continue
            if rule_id and payload.get("rule_id") != rule_id:
                continue
            records.append(payload)

    by_rule: dict[str, dict[str, Any]] = {}
    for record in records:
        current_rule_id = str(record.get("rule_id") or "")
        entry = by_rule.setdefault(
            current_rule_id,
            {
                "rule_id": current_rule_id,
                "accept": 0,
                "reject": 0,
                "defer": 0,
                "modified": 0,
                "decision_count": 0,
                "projects": [],
            },
        )
        category = str(record.get("decision_category") or _canonical_decision(str(record.get("decision") or "")))
        if category in ("accept", "reject", "defer", "modified"):
            entry[category] += 1
        entry["decision_count"] += 1
        entry["projects"].append(
            {
                "project_id": record.get("project_id"),
                "project": record.get("project"),
                "decision_id": record.get("decision_id"),
                "decision": category,
                "suggestion_id": record.get("suggestion_id"),
                "recorded_at": record.get("recorded_at"),
            }
        )
    return {
        "ok": not invalid,
        "decision_count": len(records),
        "rule_count": len(by_rule),
        "rules": [by_rule[key] for key in sorted(by_rule)],
        "decisions": records,
        "invalid": invalid,
        "automatic_learning": False,
    }
