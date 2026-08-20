"""Read-only Memory Rule API for Video OS.

Deterministic read-only access to editing rules. Never modifies rule files,
manifest, project_state, or edit_plan. Planner Memory may consume only valid,
human-activated Rule revisions; inactive rules remain preview-only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .knowledge_root import require_knowledge_root
from .rule_approval import RULE_STATUSES, validate_rule_integrity


ALLOWED_RULE_STATUSES = set(RULE_STATUSES)
DEFAULT_READ_STATUSES = ("inactive", "active")
EXCLUDED_BY_DEFAULT = {"deprecated", "revoked", "superseded"}
SCOPE_FIELDS = ("video_type", "client", "style_profile", "platform", "project")
OPERATOR_WHITELIST = {"<=", "<", ">=", ">", "==", "!="}


def validate_editing_rule(payload: dict[str, Any]) -> list[str]:
    """Return structural violations for a formal editing rule."""
    errors: list[str] = []
    required = (
        "schema_version",
        "rule_id",
        "revision",
        "version",
        "lineage_id",
        "source_candidate_id",
        "source_candidate",
        "review_id",
        "review_hash",
        "rule_class",
        "category",
        "rule_type",
        "scope",
        "expression",
        "description",
        "status",
        "active",
        "confidence_at_approval",
        "evidence_snapshot",
        "evidence_ids",
        "approval",
        "provenance",
        "content_hash",
        "lifecycle",
        "created_at",
        "updated_at",
    )
    for field in required:
        if field not in payload:
            errors.append(f"missing field: {field}")
    if int(payload.get("schema_version", 0)) != 2:
        errors.append("schema_version must be 2 for a formal rule")
    status = payload.get("status")
    if status not in ALLOWED_RULE_STATUSES:
        errors.append(f"invalid status: {status}")
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        errors.append("scope must be an object")
    expression = payload.get("expression")
    if not isinstance(expression, dict) or not expression:
        errors.append("expression must be a non-empty object")
    evidence = payload.get("evidence_snapshot")
    if not isinstance(evidence, list):
        errors.append("evidence_snapshot must be a list")
    approval = payload.get("approval")
    if not isinstance(approval, dict) or not str(approval.get("review_id") or ""):
        errors.append("approval.review_id is required")
    try:
        float(payload.get("confidence_at_approval", -1))
    except (TypeError, ValueError):
        errors.append("confidence_at_approval must be numeric")
    return errors


def load_rules(
    knowledge_dir: Path,
    statuses: tuple[str, ...] = DEFAULT_READ_STATUSES,
    rule_class: str = "editing",
    include_historical: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read editing rules. Returns (valid_rules, invalid_files).

    - Default excludes deprecated/superseded unless include_historical.
    - Never modifies any file.
    """
    rules_dir = require_knowledge_root(knowledge_dir) / "editing_rules"
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    if not rules_dir.is_dir():
        return valid, invalid
    status_set = set(statuses)
    if include_historical:
        status_set.update(EXCLUDED_BY_DEFAULT)
    records: dict[str, list[tuple[int, Path, dict[str, Any], list[str]]]] = {}
    for path in sorted(rules_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            invalid.append({"file": path.name, "errors": [f"invalid JSON: {exc}"]})
            continue
        if not isinstance(payload, dict):
            invalid.append({"file": path.name, "errors": ["not an object"]})
            continue
        errors = validate_editing_rule(payload)
        if not errors:
            errors.extend(validate_rule_integrity(knowledge_dir, payload))
        if not errors and payload.get("rule_class") != rule_class:
            continue
        rule_id = str(payload.get("rule_id") or path.stem)
        try:
            revision = int(payload.get("revision") or 0)
        except (TypeError, ValueError):
            revision = -1
            errors.append("revision must be an integer")
        records.setdefault(rule_id, []).append((revision, path, payload, errors))

    for rule_id in sorted(records):
        ordered = sorted(records[rule_id], key=lambda item: (item[0], item[1].name))
        selected = ordered if include_historical else [ordered[-1]]
        for _revision, path, payload, errors in selected:
            if errors:
                invalid.append({"file": path.name, "rule_id": rule_id, "errors": errors})
                continue
            status = payload.get("status")
            if status_set and status not in status_set:
                continue
            valid.append(payload)
    return valid, invalid


def load_project_context(path: Path) -> dict[str, Any]:
    """Load and normalize a project_context file. Missing fields stay null."""
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("project context must be a JSON object")
    normalized = {
        "schema_version": int(payload.get("schema_version", 0)),
        "project": payload.get("project"),
        "version": payload.get("version"),
        "video_type": payload.get("video_type"),
        "client": payload.get("client"),
        "style_profile": payload.get("style_profile"),
        "platform": payload.get("platform"),
        "duration_target_s": payload.get("duration_target_s"),
        "available_metrics": payload.get("available_metrics") or {},
    }
    metrics = normalized["available_metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("available_metrics must be an object")
    return normalized
