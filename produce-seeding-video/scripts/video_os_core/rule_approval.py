"""Human review, approval, and activation gates for Video OS Memory Rules.

Governance loop:
    rule_candidate -> human review (approve/reject/defer/deprecate) -> editing_rule

The system presents evidence, validates candidates, records human decisions,
and converts approved candidates into inactive editing rules. A separate human
activation is required before an exact Rule revision may become Planner advice.
The system never approves or activates rules itself.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import knowledge
from .knowledge import _atomic_write_json, refresh_counts
from .rule_candidates import (
    build_candidates,
    candidate_content_hash,
    candidate_lineage_id_for_payload,
    candidate_material_key,
    validate_rule_candidate,
)
from .production_evidence import (
    EvidenceValidationError,
    TIER_PRODUCTION_VERIFIED,
    validate_evidence_record,
    validate_production_seal,
)


SCHEMA_VERSION = 2
CANDIDATE_DECISION_ACTIONS = {
    "approve",
    "reject",
    "defer",
    "activate",
    "deactivate",
    "deprecate",
    "revoke",
    "reopen",
}
RULE_STATUSES = {"active", "inactive", "deprecated", "revoked", "superseded"}
RULE_APPLICATION_MODES = {"advisory"}
FORMAL_EVIDENCE_KIND = "production_evidence"
RULE_CONTENT_HASH_ALGORITHM = "video-os-editing-rule-v2"
REVIEW_HASH_ALGORITHM = "video-os-candidate-review-v2"
LIFECYCLE_HASH_ALGORITHM = "video-os-rule-lifecycle-v1"


class ApprovalError(RuntimeError):
    """Raised when a candidate cannot be approved/reviewed safely."""


class ReviewRecordExistsError(FileExistsError):
    """Raised when a review record would overwrite an existing immutable record."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_norm_json(value).encode("utf-8")).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError(f"{label} JSON invalid: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ApprovalError(f"{label} must contain an object: {path}")
    return payload


def rule_id_for_candidate(lineage_id: str, review_id: str | None = None) -> str:
    """Return a stable rule identity for one candidate lineage.

    ``review_id`` remains accepted for source compatibility but is deliberately
    excluded from the identity. New candidate revisions become rule revisions,
    never unrelated rule IDs.
    """
    return "rule-" + hashlib.sha1(lineage_id.encode("utf-8")).hexdigest()[:16]


def rule_file_name(rule_id: str, revision: int) -> str:
    return f"{rule_id}-v{int(revision)}.json"


def _review_material(review: dict[str, Any]) -> dict[str, Any]:
    material = deepcopy(review)
    material.pop("review_hash", None)
    return material


def _seal_review(review: dict[str, Any]) -> dict[str, Any]:
    sealed = deepcopy(review)
    sealed["review_hash"] = {
        "algorithm": REVIEW_HASH_ALGORITHM,
        "sha256": _sha256(_review_material(sealed)),
    }
    return sealed


def _evidence_action_id(record: dict[str, Any], source_ref: str) -> str:
    evidence_id = str(record.get("evidence_id") or "")
    prefix = f"{evidence_id}-"
    if not source_ref.startswith(prefix):
        raise ApprovalError(
            f"candidate evidence ref {source_ref!r} does not bind evidence {evidence_id!r}"
        )
    action_id = source_ref[len(prefix) :]
    actions = {
        str(item.get("action_id") or "")
        for item in record.get("actions") or []
        if isinstance(item, dict)
    }
    if not action_id or action_id not in actions:
        raise ApprovalError(
            f"candidate evidence ref {source_ref!r} does not name a current action"
        )
    return action_id


def build_evidence_snapshot(
    knowledge_root: Path,
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve candidate evidence into sealed production-evidence identities."""
    snapshot: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidate.get("evidence") or []:
        if not isinstance(item, dict):
            raise ApprovalError("candidate evidence entry must be an object")
        kind = str(item.get("kind") or "")
        if kind != FORMAL_EVIDENCE_KIND:
            raise ApprovalError(
                f"formal rule evidence must be {FORMAL_EVIDENCE_KIND}, got {kind or 'missing'}"
            )
        source_file = str(item.get("source_file") or "").strip()
        if not source_file or Path(source_file).name != source_file:
            raise ApprovalError(f"production evidence source_file is invalid: {source_file!r}")
        path = Path(knowledge_root).resolve() / "repair_log" / source_file
        if not path.is_file():
            raise ApprovalError(f"production evidence source missing: {source_file}")
        record = _read_json(path, "production evidence")
        errors = validate_evidence_record(record, allow_incomplete_chain=False)
        if errors:
            raise ApprovalError(
                f"production evidence schema invalid: {source_file}: " + "; ".join(errors)
            )
        if record.get("evidence_tier") != TIER_PRODUCTION_VERIFIED:
            raise ApprovalError(f"evidence is not production_verified: {source_file}")
        try:
            validate_production_seal(record)
        except EvidenceValidationError as exc:
            raise ApprovalError(f"production evidence seal invalid: {source_file}: {exc}") from exc
        source_ref = str(item.get("ref") or "")
        action_id = _evidence_action_id(record, source_ref)
        gate = (record.get("verification") or {}).get("gate") or {}
        identity = (str(record["evidence_id"]), action_id)
        if identity in seen:
            continue
        seen.add(identity)
        source_identity = record.get("source_identity") or {}
        snapshot.append(
            {
                "kind": FORMAL_EVIDENCE_KIND,
                "evidence_id": record.get("evidence_id"),
                "action_id": action_id,
                "source_ref": source_ref,
                "source_file": source_file,
                "project_id": record.get("project_id"),
                "project": record.get("project"),
                "run_id": source_identity.get("run_id"),
                "evidence_tier": record.get("evidence_tier"),
                "gate_material_digest": gate.get("material_digest"),
            }
        )
    if not snapshot:
        raise ApprovalError("formal rule requires production_verified evidence")
    return snapshot


def _candidate_binding(
    knowledge_root: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "lineage_id": candidate_lineage_id_for_payload(candidate),
        "revision": int(candidate.get("revision") or 1),
        "content_hash": candidate_content_hash(candidate),
        "evidence_snapshot": build_evidence_snapshot(knowledge_root, candidate),
    }


def _lifecycle_event(
    *,
    event: str,
    previous_status: str | None,
    status: str,
    reviewer: str,
    reason: str,
    review_id: str,
    previous_hash: str | None,
    application_mode: str | None = None,
) -> dict[str, Any]:
    payload = {
        "event": event,
        "previous_status": previous_status,
        "status": status,
        "reviewer": reviewer,
        "reason": reason,
        "review_id": review_id,
        "at": _now_iso(),
        "previous_hash": previous_hash,
    }
    if application_mode is not None:
        payload["application_mode"] = application_mode
    payload["event_hash"] = {
        "algorithm": LIFECYCLE_HASH_ALGORITHM,
        "sha256": _sha256(payload),
    }
    return payload


def _rule_material(rule: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "schema_version",
        "rule_id",
        "revision",
        "version",
        "lineage_id",
        "source_candidate_id",
        "source_candidate",
        "review_id",
        "review_hash",
        "evidence_ids",
        "evidence_snapshot",
        "rule_class",
        "category",
        "rule_type",
        "type",
        "scope",
        "expression",
        "metric",
        "field",
        "operator",
        "value",
        "description",
        "confidence_at_approval",
        "approval",
        "provenance",
        "supersedes",
        "conflicts_with",
    )
    return {field: deepcopy(rule.get(field)) for field in fields}


def _seal_rule(rule: dict[str, Any]) -> dict[str, Any]:
    sealed = deepcopy(rule)
    sealed["content_hash"] = {
        "algorithm": RULE_CONTENT_HASH_ALGORITHM,
        "sha256": _sha256(_rule_material(sealed)),
    }
    return sealed


def candidate_path(knowledge_root: Path, candidate_id: str) -> Path:
    return Path(knowledge_root).resolve() / "rule_candidates" / f"{candidate_id}.json"


def load_candidate(knowledge_root: Path, candidate_id: str) -> dict[str, Any]:
    path = candidate_path(knowledge_root, candidate_id)
    if not path.is_file():
        raise ApprovalError(f"candidate not found: {candidate_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalError(f"candidate JSON invalid: {candidate_id}: {exc}") from exc
    errors = validate_rule_candidate(payload)
    if errors:
        raise ApprovalError(
            f"candidate schema invalid: {candidate_id}: " + "; ".join(errors)
        )
    return payload


def _evidence_file(knowledge_root: Path, kind: str, source_file: str) -> Path:
    if kind == "feedback":
        return Path(knowledge_root).resolve() / "edits" / source_file
    if kind in ("repair_log", FORMAL_EVIDENCE_KIND):
        return Path(knowledge_root).resolve() / "repair_log" / source_file
    return Path(knowledge_root).resolve() / "reviews" / source_file


def resolve_evidence_sources(
    knowledge_root: Path,
    candidate: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return (missing_source_files, unresolved_refs)."""
    missing_files: list[str] = []
    unresolved_refs: list[str] = []
    for item in candidate.get("evidence", []) or []:
        if not isinstance(item, dict):
            continue
        source_file = str(item.get("source_file") or "")
        kind = str(item.get("kind") or "")
        if source_file:
            path = _evidence_file(knowledge_root, kind, source_file)
            if not path.is_file():
                missing_files.append(source_file)
        ref = str(item.get("ref") or "")
        if ref and not str(item.get("snapshot_ref") or "").strip():
            unresolved_refs.append(ref)
    return missing_files, unresolved_refs


def recompute_candidate(knowledge_root: Path, candidate_id: str) -> dict[str, Any] | None:
    """Recompute the exact persisted revision from its current evidence."""
    persisted = load_candidate(knowledge_root, candidate_id)
    lineage_id = candidate_lineage_id_for_payload(persisted)
    candidates, _ = build_candidates(knowledge_root)
    for candidate in candidates:
        if candidate_lineage_id_for_payload(candidate) != lineage_id:
            continue
        if candidate_material_key(candidate) != candidate_material_key(persisted):
            return None
        recomputed = dict(candidate)
        for field in (
            "candidate_id",
            "rule_id",
            "lineage_id",
            "revision",
            "supersedes_candidate_id",
        ):
            if field in persisted:
                recomputed[field] = persisted[field]
        return recomputed
    return None


def find_rule_conflicts(
    knowledge_root: Path,
    candidate: dict[str, Any],
    target_scope: dict[str, Any],
) -> list[dict[str, Any]]:
    """First version: same expression with different threshold/constraint conflicts."""
    conflicts: list[dict[str, Any]] = []
    expression = candidate.get("expression") or {}
    metric = str(expression.get("metric") or "")
    constraint = str(expression.get("constraint") or "")
    candidate_scope = {
        "video_type": str(target_scope.get("video_type") or ""),
        "client": str(target_scope.get("client") or ""),
        "style_profile": str(target_scope.get("style_profile") or ""),
    }
    rules_dir = Path(knowledge_root).resolve() / "editing_rules"
    if not rules_dir.is_dir():
        return conflicts
    for path in sorted(rules_dir.glob("*.json")):
        try:
            rule = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rule, dict):
            continue
        if rule.get("status") == "deprecated":
            continue
        rule_expression = rule.get("expression") or {}
        rule_scope = rule.get("scope") or {}
        overlapping = True
        for key, value in candidate_scope.items():
            rule_value = str(rule_scope.get(key) or "")
            if value and rule_value and value != rule_value:
                overlapping = False
                break
        if not overlapping:
            continue
        if metric and str(rule_expression.get("metric") or "") == metric:
            new_value = str(expression.get("value") or "")
            old_value = str(rule_expression.get("value") or "")
            new_operator = str(expression.get("operator") or "")
            old_operator = str(rule_expression.get("operator") or "")
            if new_operator == old_operator and new_value and new_value == old_value:
                continue  # identical rule
            conflicts.append(
                {
                    "existing_rule_id": rule.get("rule_id"),
                    "type": "same_metric_different_threshold",
                    "existing_expression": rule_expression,
                    "new_expression": expression,
                    "existing_status": rule.get("status"),
                    "existing_scope": rule_scope,
                }
            )
        elif constraint and str(rule_expression.get("constraint") or "") == constraint:
            conflicts.append(
                {
                    "existing_rule_id": rule.get("rule_id"),
                    "type": "same_constraint",
                    "existing_expression": rule_expression,
                    "new_expression": expression,
                    "existing_status": rule.get("status"),
                    "existing_scope": rule_scope,
                }
            )
    return conflicts


def _apply_scope_adjustment(
    candidate_scope: dict[str, Any],
    scope_adjustment: dict[str, Any] | None,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    base = {
        "video_type": candidate_scope.get("video_type"),
        "client": candidate_scope.get("client"),
        "style_profile": candidate_scope.get("style_profile"),
    }
    if not scope_adjustment:
        return base
    # Human may narrow scope; never expand beyond candidate evidence.
    for key in ("video_type", "client", "style_profile"):
        value = scope_adjustment.get(key)
        if value is None:
            continue
        candidate_value = candidate_scope.get(key)
        if candidate_value and value != candidate_value:
            raise ApprovalError(
                f"scope cannot be expanded without evidence: {key} "
                f"{candidate_value!r} -> {value!r}"
            )
        base[key] = value
    return base


def write_review_record(
    knowledge_root: Path,
    review: dict[str, Any],
) -> dict[str, Any]:
    """Write an immutable, append-only review record."""
    errors = _validate_review(review)
    if errors:
        raise ApprovalError("Invalid review record: " + "; ".join(errors))
    reviews_dir = Path(knowledge_root).resolve() / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    path = reviews_dir / f"review-{review['review_id']}.json"
    if path.is_file():
        raise ReviewRecordExistsError(f"review record already exists: {path}")
    _atomic_write_json(path, review)
    refresh_counts(knowledge_root)
    return {"written": True, "path": str(path)}


def _validate_review(review: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(review.get("schema_version", 0)) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("review_id", "decision", "reason", "reviewed_at"):
        if not str(review.get(field) or "").strip():
            errors.append(f"{field} is required")
    if review.get("decision") not in CANDIDATE_DECISION_ACTIONS:
        errors.append(f"decision must be one of {sorted(CANDIDATE_DECISION_ACTIONS)}")
    reviewer = review.get("reviewer") or {}
    if not isinstance(reviewer, dict) or reviewer.get("type") != "human":
        errors.append("reviewer.type must be human")
    if not str(reviewer.get("name") or "").strip():
        errors.append("reviewer.name is required")
    if review.get("review_type") == "rule_lifecycle":
        binding = review.get("rule_binding")
        if not isinstance(binding, dict):
            errors.append("rule_binding is required for lifecycle review")
        else:
            for field in ("rule_id", "revision", "content_hash"):
                if binding.get(field) in (None, ""):
                    errors.append(f"rule_binding.{field} is required")
    else:
        for field in (
            "candidate_id",
            "candidate_revision",
            "candidate_content_hash",
            "evidence_snapshot",
        ):
            if review.get(field) in (None, ""):
                errors.append(f"{field} is required")
        if not isinstance(review.get("evidence_snapshot"), list) or not review.get(
            "evidence_snapshot"
        ):
            errors.append("evidence_snapshot must be a non-empty list")
    review_hash = review.get("review_hash")
    if not isinstance(review_hash, dict):
        errors.append("review_hash is required")
    elif review_hash.get("algorithm") != REVIEW_HASH_ALGORITHM:
        errors.append("review_hash algorithm is invalid")
    elif review_hash.get("sha256") != _sha256(_review_material(review)):
        errors.append("review_hash does not match review content")
    return errors


def review_id_for(candidate_id: str, revision: int | None = None) -> str:
    revision_part = f"-r{int(revision)}" if revision is not None else ""
    return (
        f"review-{candidate_id}{revision_part}-"
        f"{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
    )


def _candidate_review(
    knowledge_root: Path,
    candidate: dict[str, Any],
    *,
    decision: str,
    reviewer: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binding = _candidate_binding(knowledge_root, candidate)
    review = {
        "schema_version": SCHEMA_VERSION,
        "review_type": "candidate",
        "review_id": review_id_for(
            str(binding["candidate_id"]), int(binding["revision"])
        ),
        "candidate_id": binding["candidate_id"],
        "candidate_revision": binding["revision"],
        "candidate_content_hash": binding["content_hash"],
        "evidence_snapshot": binding["evidence_snapshot"],
        "decision": decision,
        "reviewer": {"type": "human", "name": reviewer},
        "reason": reason,
        "reviewed_at": _now_iso(),
    }
    if extra:
        review.update(deepcopy(extra))
    return _seal_review(review)


def _review_path(knowledge_root: Path, review_id: str) -> Path:
    return Path(knowledge_root).resolve() / "reviews" / f"review-{review_id}.json"


def validate_review_integrity(
    knowledge_root: Path,
    review: dict[str, Any],
    *,
    candidate: dict[str, Any] | None = None,
) -> list[str]:
    """Validate a sealed review against the current candidate/evidence snapshot."""
    errors = _validate_review(review)
    if errors or review.get("review_type") == "rule_lifecycle":
        return errors
    candidate_id = str(review.get("candidate_id") or "")
    if candidate is None:
        path = candidate_path(knowledge_root, candidate_id)
        if not path.is_file():
            return errors + [f"candidate not found: {candidate_id}"]
        try:
            candidate = _read_json(path, "candidate")
        except ApprovalError as exc:
            return errors + [str(exc)]
    candidate_errors = validate_rule_candidate(candidate)
    if candidate_errors:
        errors.append("candidate schema invalid: " + "; ".join(candidate_errors))
        return errors
    if candidate.get("candidate_id") != candidate_id:
        errors.append("review candidate_id does not match candidate content")
    if int(candidate.get("revision") or 1) != int(review.get("candidate_revision") or 0):
        errors.append("review candidate revision is stale")
    if candidate_content_hash(candidate) != review.get("candidate_content_hash"):
        errors.append("review candidate content hash is stale")
    try:
        current_snapshot = build_evidence_snapshot(knowledge_root, candidate)
    except ApprovalError as exc:
        errors.append(str(exc))
    else:
        if current_snapshot != review.get("evidence_snapshot"):
            errors.append("review evidence snapshot is stale")
    return errors


def _validate_lifecycle(rule: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    lifecycle = rule.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return ["lifecycle is required"]
    history = lifecycle.get("history")
    if not isinstance(history, list) or not history:
        return ["lifecycle.history must be a non-empty array"]
    previous_hash: str | None = None
    previous_status: str | None = None
    for index, event in enumerate(history):
        if not isinstance(event, dict):
            errors.append(f"lifecycle.history[{index}] must be an object")
            continue
        event_hash = event.get("event_hash")
        material = deepcopy(event)
        material.pop("event_hash", None)
        if not isinstance(event_hash, dict):
            errors.append(f"lifecycle.history[{index}].event_hash is required")
        elif event_hash.get("algorithm") != LIFECYCLE_HASH_ALGORITHM:
            errors.append(f"lifecycle.history[{index}].event_hash algorithm is invalid")
        elif event_hash.get("sha256") != _sha256(material):
            errors.append(f"lifecycle.history[{index}] content hash is invalid")
        if event.get("previous_hash") != previous_hash:
            errors.append(f"lifecycle.history[{index}] previous_hash is invalid")
        if event.get("previous_status") != previous_status:
            errors.append(f"lifecycle.history[{index}] previous_status is invalid")
        previous_hash = (
            str(event_hash.get("sha256")) if isinstance(event_hash, dict) else None
        )
        previous_status = str(event.get("status") or "")
    if lifecycle.get("status") != previous_status or rule.get("status") != previous_status:
        errors.append("rule status does not match lifecycle history")
    if lifecycle.get("revision") != len(history):
        errors.append("lifecycle revision does not match history length")
    if rule.get("status") not in RULE_STATUSES:
        errors.append(f"invalid rule status: {rule.get('status')}")
    expected_active = rule.get("status") == "active"
    if bool(rule.get("active")) != expected_active:
        errors.append("active flag does not match rule status")
    return errors


def _validate_lifecycle_reviews(
    knowledge_root: Path,
    rule: dict[str, Any],
) -> list[str]:
    """Bind every lifecycle mutation to an immutable human review record."""
    errors: list[str] = []
    history = ((rule.get("lifecycle") or {}).get("history") or [])
    for index, event in enumerate(history):
        if not isinstance(event, dict):
            continue
        review_id = str(event.get("review_id") or "")
        if index == 0:
            if event.get("event") != "approve":
                errors.append("initial lifecycle event must be approve")
            if review_id != str(rule.get("review_id") or ""):
                errors.append("initial lifecycle event review_id mismatch")
            continue
        review_file = _review_path(knowledge_root, review_id)
        if not review_file.is_file():
            errors.append(f"lifecycle review not found: {review_id}")
            continue
        try:
            review = _read_json(review_file, "lifecycle review")
        except ApprovalError as exc:
            errors.append(str(exc))
            continue
        review_errors = _validate_review(review)
        errors.extend(f"lifecycle review: {item}" for item in review_errors)
        binding = review.get("rule_binding") or {}
        expected_binding = {
            "rule_id": rule.get("rule_id"),
            "revision": rule.get("revision"),
            "content_hash": (rule.get("content_hash") or {}).get("sha256"),
            "previous_status": event.get("previous_status"),
            "target_status": event.get("status"),
        }
        for field, value in expected_binding.items():
            if binding.get(field) != value:
                errors.append(f"lifecycle review rule_binding.{field} mismatch")
        if review.get("review_type") != "rule_lifecycle":
            errors.append("lifecycle review_type must be rule_lifecycle")
        if review.get("decision") != event.get("event"):
            errors.append("lifecycle review decision mismatch")
        if str((review.get("reviewer") or {}).get("name") or "") != str(
            event.get("reviewer") or ""
        ):
            errors.append("lifecycle review reviewer mismatch")
        if review.get("reason") != event.get("reason"):
            errors.append("lifecycle review reason mismatch")
        if event.get("event") == "activate":
            if binding.get("application_mode") != event.get("application_mode"):
                errors.append("activation application_mode mismatch")
            if binding.get("application_mode") not in RULE_APPLICATION_MODES:
                errors.append("activation application_mode must be advisory")
    return errors


def _validate_current_activation(rule: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if rule.get("status") != "active":
        return errors
    activation = rule.get("activation")
    if not isinstance(activation, dict):
        return ["active rule requires a sealed activation record"]
    required = (
        "reviewer",
        "reason",
        "rule_id",
        "rule_revision",
        "rule_content_hash",
        "review_id",
        "activated_at",
        "application_mode",
    )
    for field in required:
        if activation.get(field) in (None, ""):
            errors.append(f"activation.{field} is required")
    expected = {
        "rule_id": rule.get("rule_id"),
        "rule_revision": rule.get("revision"),
        "rule_content_hash": (rule.get("content_hash") or {}).get("sha256"),
        "application_mode": "advisory",
    }
    for field, value in expected.items():
        if activation.get(field) != value:
            errors.append(f"activation.{field} mismatch")
    history = ((rule.get("lifecycle") or {}).get("history") or [])
    last_event = history[-1] if history and isinstance(history[-1], dict) else {}
    if last_event.get("event") != "activate":
        errors.append("active rule lifecycle does not end with activate")
    if activation.get("review_id") != last_event.get("review_id"):
        errors.append("activation review_id does not match lifecycle")
    if activation.get("activated_at") != last_event.get("at"):
        errors.append("activation timestamp does not match lifecycle")
    if activation.get("reviewer") != last_event.get("reviewer"):
        errors.append("activation reviewer does not match lifecycle")
    if activation.get("reason") != last_event.get("reason"):
        errors.append("activation reason does not match lifecycle")
    if activation.get("application_mode") != last_event.get("application_mode"):
        errors.append("activation application_mode does not match lifecycle")
    return errors


def validate_rule_integrity(
    knowledge_root: Path,
    rule: dict[str, Any],
) -> list[str]:
    """Fail closed unless rule, review, candidate and evidence still agree."""
    errors: list[str] = []
    if int(rule.get("schema_version", 0)) != SCHEMA_VERSION:
        return [f"schema_version must be {SCHEMA_VERSION} for a formal rule"]
    required = (
        "rule_id",
        "revision",
        "version",
        "lineage_id",
        "source_candidate",
        "review_id",
        "review_hash",
        "evidence_ids",
        "evidence_snapshot",
        "scope",
        "expression",
        "metric",
        "field",
        "operator",
        "value",
        "confidence_at_approval",
        "provenance",
        "content_hash",
        "lifecycle",
    )
    for field in required:
        if field not in rule:
            errors.append(f"missing rule field: {field}")
    content_hash = rule.get("content_hash")
    if not isinstance(content_hash, dict):
        errors.append("content_hash is required")
    elif content_hash.get("algorithm") != RULE_CONTENT_HASH_ALGORITHM:
        errors.append("content_hash algorithm is invalid")
    elif content_hash.get("sha256") != _sha256(_rule_material(rule)):
        errors.append("rule content hash is invalid")
    errors.extend(_validate_lifecycle(rule))
    errors.extend(_validate_current_activation(rule))

    source = rule.get("source_candidate")
    if not isinstance(source, dict):
        errors.append("source_candidate must be an object")
        return errors
    candidate_id = str(source.get("candidate_id") or "")
    candidate_file = candidate_path(knowledge_root, candidate_id)
    if not candidate_file.is_file():
        errors.append(f"candidate not found: {candidate_id}")
        return errors
    try:
        candidate = _read_json(candidate_file, "candidate")
    except ApprovalError as exc:
        errors.append(str(exc))
        return errors
    candidate_errors = validate_rule_candidate(candidate)
    if candidate_errors:
        errors.append("candidate schema invalid: " + "; ".join(candidate_errors))
    if candidate.get("status") != "approved":
        errors.append("source candidate is not approved")
    if rule.get("source_candidate_id") != candidate_id:
        errors.append("source_candidate_id mismatch")
    if int(source.get("revision") or 0) != int(candidate.get("revision") or 1):
        errors.append("source candidate revision mismatch")
    current_candidate_hash = candidate_content_hash(candidate)
    if source.get("content_hash") != current_candidate_hash:
        errors.append("source candidate content hash mismatch")
    if source.get("lineage_id") != candidate_lineage_id_for_payload(candidate):
        errors.append("source candidate lineage mismatch")
    if int(rule.get("revision") or 0) != int(source.get("revision") or 0):
        errors.append("rule revision does not match candidate revision")
    if rule.get("rule_id") != rule_id_for_candidate(
        candidate_lineage_id_for_payload(candidate)
    ):
        errors.append("rule_id is not stable for candidate lineage")
    if rule.get("version") != f"v{int(rule.get('revision') or 0)}":
        errors.append("rule version does not match rule revision")
    for field in ("rule_class", "category", "rule_type", "type", "expression"):
        if rule.get(field) != candidate.get(field):
            errors.append(f"rule {field} does not match approved candidate")
    candidate_expression = candidate.get("expression") or {}
    expected_metric = candidate_expression.get("metric")
    expected_field = expected_metric or "constraint"
    expected_operator = candidate_expression.get("operator") or "enforce"
    expected_value = (
        candidate_expression.get("value")
        if expected_metric
        else candidate_expression.get("constraint")
    )
    if rule.get("metric") != expected_metric:
        errors.append("rule metric does not match candidate expression")
    if rule.get("field") != expected_field:
        errors.append("rule field does not match candidate expression")
    if rule.get("operator") != expected_operator:
        errors.append("rule operator does not match candidate expression")
    if rule.get("value") != expected_value:
        errors.append("rule value does not match candidate expression")
    if rule.get("confidence_at_approval") != candidate.get("confidence"):
        errors.append("rule confidence does not match approved candidate")

    review_id = str(rule.get("review_id") or "")
    review_file = _review_path(knowledge_root, review_id)
    if not review_file.is_file():
        errors.append(f"approval review not found: {review_id}")
        return errors
    try:
        review = _read_json(review_file, "review")
    except ApprovalError as exc:
        errors.append(str(exc))
        return errors
    review_errors = validate_review_integrity(
        knowledge_root, review, candidate=candidate
    )
    errors.extend(f"review: {item}" for item in review_errors)
    if review.get("decision") != "approve":
        errors.append("approval review decision is not approve")
    if review.get("review_id") != review_id:
        errors.append("approval review ID mismatch")
    review_hash = (review.get("review_hash") or {}).get("sha256")
    if rule.get("review_hash") != review_hash:
        errors.append("rule review hash mismatch")
    if source.get("content_hash") != review.get("candidate_content_hash"):
        errors.append("rule candidate hash does not match review")
    if source.get("revision") != review.get("candidate_revision"):
        errors.append("rule candidate revision does not match review")
    try:
        expected_scope = _apply_scope_adjustment(
            candidate.get("scope") or {},
            review.get("scope_adjustment"),
            str((review.get("reviewer") or {}).get("name") or ""),
            str(review.get("reason") or ""),
        )
    except ApprovalError as exc:
        errors.append(f"review scope adjustment invalid: {exc}")
    else:
        if rule.get("scope") != expected_scope:
            errors.append("rule scope does not match reviewed scope")
    if rule.get("evidence_snapshot") != review.get("evidence_snapshot"):
        errors.append("rule evidence snapshot does not match review")
    evidence_ids = [
        item.get("evidence_id")
        for item in rule.get("evidence_snapshot") or []
        if isinstance(item, dict)
    ]
    if rule.get("evidence_ids") != evidence_ids:
        errors.append("rule evidence_ids do not match evidence snapshot")

    provenance = rule.get("provenance")
    required_provenance = (
        "candidate_id",
        "candidate_revision",
        "candidate_content_hash",
        "review_id",
        "review_hash",
        "evidence_ids",
    )
    if not isinstance(provenance, dict):
        errors.append("provenance must be an object")
    else:
        for field in required_provenance:
            if provenance.get(field) in (None, "", []):
                errors.append(f"provenance.{field} is required")
        expected_provenance = {
            "candidate_id": candidate_id,
            "candidate_revision": source.get("revision"),
            "candidate_content_hash": source.get("content_hash"),
            "review_id": review_id,
            "review_hash": review_hash,
            "evidence_ids": evidence_ids,
        }
        for field, value in expected_provenance.items():
            if provenance.get(field) != value:
                errors.append(f"provenance.{field} mismatch")
    errors.extend(_validate_lifecycle_reviews(knowledge_root, rule))
    return errors


# ---------------------------------------------------------------- CLI actions


def review_candidate(
    knowledge_root: Path,
    candidate_id: str,
) -> dict[str, Any]:
    candidate = load_candidate(knowledge_root, candidate_id)
    missing_files, unresolved_refs = resolve_evidence_sources(
        knowledge_root, candidate
    )
    conflicts = find_rule_conflicts(
        knowledge_root,
        candidate,
        candidate.get("scope") or {},
    )
    recomputed = recompute_candidate(knowledge_root, candidate_id)
    try:
        binding = _candidate_binding(knowledge_root, candidate)
        governance_errors: list[str] = []
    except ApprovalError as exc:
        binding = None
        governance_errors = [str(exc)]
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "candidate_revision": int(candidate.get("revision") or 1),
        "candidate_content_hash": candidate_content_hash(candidate),
        "status": candidate.get("status"),
        "rule_type": candidate.get("rule_type"),
        "category": candidate.get("category"),
        "expression": candidate.get("expression"),
        "scope": candidate.get("scope"),
        "confidence": candidate.get("confidence"),
        "confidence_factors": candidate.get("confidence_factors"),
        "evidence_count": candidate.get("evidence_count"),
        "weighted_evidence": candidate.get("weighted_evidence"),
        "project_count": candidate.get("project_count"),
        "version_count": candidate.get("version_count"),
        "human_feedback_count": candidate.get("human_feedback_count"),
        "repair_evidence_count": candidate.get("repair_evidence_count"),
        "evidence": candidate.get("evidence", []),
        "contradicting_evidence": candidate.get("contradicting_evidence", []),
        "source_valid": not missing_files,
        "missing_source_files": missing_files,
        "unresolved_refs": unresolved_refs,
        "recompute_matches": recomputed is not None,
        "confidence_recomputable": (
            recomputed is not None
            and abs(float(recomputed.get("confidence", -1)) - float(candidate.get("confidence", -2)))
            < 1e-6
        ),
        "possible_rule_conflicts": conflicts,
        "formal_governance_ready": binding is not None,
        "formal_governance_errors": governance_errors,
        "evidence_snapshot": (
            binding.get("evidence_snapshot") if binding is not None else []
        ),
        "available_decisions": ["approve", "reject", "defer"],
    }


def approve_rule(
    knowledge_root: Path,
    candidate_id: str,
    *,
    reviewer: str,
    reason: str,
    video_type: str | None = None,
    client: str | None = None,
    style_profile: str | None = None,
    conflict_resolution: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    candidate = load_candidate(knowledge_root, candidate_id)
    rules_dir = Path(knowledge_root).resolve() / "editing_rules"
    # Idempotency is accepted only for an existing rule that still passes the
    # complete candidate/review/evidence integrity chain. A forged candidate
    # status must never be treated as proof of approval.
    if candidate.get("status") == "approved":
        if rules_dir.is_dir():
            for path in sorted(rules_dir.glob("*.json")):
                try:
                    existing = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(existing, dict)
                    and existing.get("source_candidate_id") == candidate_id
                ):
                    integrity_errors = validate_rule_integrity(
                        knowledge_root, existing
                    )
                    if integrity_errors:
                        return {
                            "ok": False,
                            "candidate_id": candidate_id,
                            "decision": "approve",
                            "denied": True,
                            "reasons": [
                                "approved candidate has no valid rule: "
                                + "; ".join(integrity_errors)
                            ],
                        }
                    return {
                        "ok": True,
                        "candidate_id": candidate_id,
                        "decision": "approve",
                        "idempotent": True,
                        "rule_id": existing.get("rule_id"),
                        "rule_file": str(path),
                        "message": "rule already exists for this candidate approval",
                    }
        return {
            "ok": False,
            "candidate_id": candidate_id,
            "decision": "approve",
            "denied": True,
            "reasons": [
                "candidate status says approved but no sealed approval rule exists"
            ],
        }
    checks: list[str] = []
    if candidate.get("status") == "stale":
        checks.append("candidate is stale; approval denied")
    if candidate.get("status") == "conflicted" and not conflict_resolution:
        checks.append("conflicted candidate needs conflict_resolution to approve")
    if candidate.get("status") in ("rejected", "deferred"):
        checks.append(
            f"candidate status is {candidate.get('status')}; use reopen-candidate first"
        )
    if not reason.strip():
        checks.append("approval reason is required")
    if not reviewer.strip():
        checks.append("reviewer is required")

    scope_adjustment: dict[str, Any] | None = None
    if video_type or client or style_profile:
        scope_adjustment = {
            "video_type": video_type,
            "client": client,
            "style_profile": style_profile,
        }
    try:
        target_scope = _apply_scope_adjustment(
            candidate.get("scope") or {},
            scope_adjustment,
            reviewer,
            reason,
        )
    except ApprovalError as exc:
        checks.append(str(exc))
        target_scope = candidate.get("scope") or {}

    try:
        binding = _candidate_binding(knowledge_root, candidate)
    except ApprovalError as exc:
        checks.append(str(exc))
        binding = None

    weighted = float(candidate.get("weighted_evidence", 0))
    if weighted < 2.0:
        checks.append(f"weighted evidence {weighted} < 2.0")
    if int(candidate.get("evidence_count", 0)) < 2:
        checks.append("evidence_count < 2")
    if int(candidate.get("project_count", 0)) < 2:
        checks.append("project_count < 2")

    recomputed = recompute_candidate(knowledge_root, candidate_id)
    if recomputed is None:
        checks.append("confidence cannot be recomputed (candidate no longer derivable)")
    else:
        original_confidence = float(candidate.get("confidence", -1))
        recomputed_confidence = float(recomputed.get("confidence", -2))
        if abs(original_confidence - recomputed_confidence) > 1e-6:
            checks.append(
                f"confidence not reproducible: stored {original_confidence} "
                f"vs recomputed {recomputed_confidence}"
            )

    if (candidate.get("contradicting_evidence") or []) and not conflict_resolution:
        checks.append("contradicting evidence present; conflict_resolution required")

    rule_conflicts = find_rule_conflicts(knowledge_root, candidate, target_scope)
    if rule_conflicts and not conflict_resolution:
        checks.append(
            "unresolved rule conflicts: "
            + ", ".join(
                str(item.get("existing_rule_id")) for item in rule_conflicts
            )
        )

    if checks:
        return {
            "ok": False,
            "candidate_id": candidate_id,
            "decision": "approve",
            "denied": True,
            "reasons": checks,
            "rule_conflicts": rule_conflicts,
            "dry_run": dry_run,
        }

    assert binding is not None
    review = _candidate_review(
        knowledge_root,
        candidate,
        decision="approve",
        reviewer=reviewer,
        reason=reason,
        extra={
            "scope_adjustment": scope_adjustment,
            "conflict_resolution": conflict_resolution,
        },
    )
    review_id = str(review["review_id"])
    lineage_id = str(binding["lineage_id"])
    revision = int(binding["revision"])
    rule_id = rule_id_for_candidate(lineage_id)
    expression = deepcopy(candidate.get("expression") or {})
    metric = expression.get("metric")
    field = metric or "constraint"
    operator = expression.get("operator") or "enforce"
    value = expression.get("value") if metric else expression.get("constraint")
    evidence_snapshot = deepcopy(binding["evidence_snapshot"])
    evidence_ids = [item["evidence_id"] for item in evidence_snapshot]
    previous_revisions: list[int] = []
    if rules_dir.is_dir():
        for path in sorted(rules_dir.glob("*.json")):
            try:
                existing = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(existing, dict) or existing.get("rule_id") != rule_id:
                continue
            try:
                previous_revisions.append(int(existing.get("revision") or 0))
            except (TypeError, ValueError):
                continue
    supersedes = None
    if previous_revisions:
        prior = max(previous_revisions)
        if prior >= revision:
            checks.append(
                f"rule revision collision: existing v{prior}, candidate requests v{revision}"
            )
        else:
            supersedes = {"rule_id": rule_id, "revision": prior}
    if checks:
        return {
            "ok": False,
            "candidate_id": candidate_id,
            "decision": "approve",
            "denied": True,
            "reasons": checks,
            "rule_conflicts": rule_conflicts,
            "dry_run": dry_run,
        }
    review_hash = str(review["review_hash"]["sha256"])
    lifecycle_event = _lifecycle_event(
        event="approve",
        previous_status=None,
        status="inactive",
        reviewer=reviewer,
        reason=reason,
        review_id=review_id,
        previous_hash=None,
    )
    editing_rule = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": rule_id,
        "revision": revision,
        "version": f"v{revision}",
        "lineage_id": lineage_id,
        "source_candidate_id": candidate_id,
        "source_candidate": {
            "candidate_id": candidate_id,
            "lineage_id": lineage_id,
            "revision": revision,
            "content_hash": binding["content_hash"],
        },
        "review_id": review_id,
        "review_hash": review_hash,
        "evidence_ids": evidence_ids,
        "rule_class": candidate.get("rule_class"),
        "category": candidate.get("category"),
        "rule_type": candidate.get("rule_type"),
        "type": candidate.get("type"),
        "scope": target_scope,
        "expression": expression,
        "metric": metric,
        "field": field,
        "operator": operator,
        "value": value,
        "description": candidate.get("description"),
        "status": "inactive",
        "active": False,
        "confidence_at_approval": candidate.get("confidence"),
        "evidence_snapshot": evidence_snapshot,
        "approval": {
            "review_id": review_id,
            "reviewer": reviewer,
            "reason": reason,
            "scope_adjustment": scope_adjustment,
            "conflict_resolution": conflict_resolution,
        },
        "provenance": {
            "candidate_id": candidate_id,
            "candidate_revision": revision,
            "candidate_content_hash": binding["content_hash"],
            "review_id": review_id,
            "review_hash": review_hash,
            "evidence_ids": evidence_ids,
        },
        "supersedes": supersedes,
        "lifecycle": {
            "status": "inactive",
            "revision": 1,
            "history": [lifecycle_event],
        },
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "evidence_status": "valid",
        "conflicts_with": [
            str(item.get("existing_rule_id")) for item in rule_conflicts
        ],
    }
    editing_rule = _seal_rule(editing_rule)
    if dry_run:
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "decision": "approve",
            "dry_run": True,
            "review": review,
            "editing_rule": editing_rule,
            "message": "dry-run: nothing written",
        }

    rules_dir.mkdir(parents=True, exist_ok=True)
    existing_rule = rules_dir / rule_file_name(rule_id, revision)
    if existing_rule.is_file():
        existing_payload = _read_json(existing_rule, "editing rule")
        if existing_payload == editing_rule:
            return {
                "ok": True,
                "candidate_id": candidate_id,
                "decision": "approve",
                "idempotent": True,
                "rule_id": rule_id,
                "rule_revision": revision,
                "rule_file": str(existing_rule),
                "message": "rule already exists for this candidate approval",
            }
        raise ApprovalError(f"rule revision already exists with different content: {existing_rule}")

    write_review_record(knowledge_root, review)
    _atomic_write_json(existing_rule, editing_rule)
    candidate_path_file = candidate_path(knowledge_root, candidate_id)
    candidate_copy = dict(candidate)
    candidate_copy["status"] = "approved"
    candidate_copy["updated_at"] = _now_iso()
    _atomic_write_json(candidate_path_file, candidate_copy)
    refresh_counts(knowledge_root)
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "decision": "approve",
        "idempotent": False,
        "rule_id": rule_id,
        "rule_revision": revision,
        "rule_file": str(existing_rule),
        "review_id": review_id,
        "review_file": str(_review_path(knowledge_root, review_id)),
    }


def reject_candidate(
    knowledge_root: Path,
    candidate_id: str,
    *,
    reviewer: str,
    reason: str,
    rejection_category: str | None = None,
    allow_future_recandidacy: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not reason.strip():
        raise ApprovalError("rejection reason is required")
    if not reviewer.strip():
        raise ApprovalError("reviewer is required")
    candidate = load_candidate(knowledge_root, candidate_id)
    if candidate.get("status") in ("approved", "rejected"):
        raise ApprovalError(
            f"candidate status is {candidate.get('status')}; create a new revision for another review"
        )
    review = _candidate_review(
        knowledge_root,
        candidate,
        decision="reject",
        reviewer=reviewer,
        reason=reason,
        extra={
            "rejection_category": rejection_category,
            "allow_future_recandidacy": allow_future_recandidacy,
        },
    )
    if dry_run:
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "decision": "reject",
            "dry_run": True,
            "review": review,
            "message": "dry-run: candidate file will not be modified",
        }
    write_review_record(knowledge_root, review)
    candidate_copy = dict(candidate)
    candidate_copy["status"] = "rejected"
    candidate_copy["updated_at"] = _now_iso()
    _atomic_write_json(candidate_path(knowledge_root, candidate_id), candidate_copy)
    refresh_counts(knowledge_root)
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "decision": "reject",
        "review_id": review["review_id"],
        "candidate_status": "rejected",
        "candidate_kept": True,
    }


def defer_candidate(
    knowledge_root: Path,
    candidate_id: str,
    *,
    reviewer: str,
    reason: str,
    minimum_new_projects: int = 0,
    minimum_weighted_evidence: float = 0.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not reason.strip():
        raise ApprovalError("defer reason is required")
    if not reviewer.strip():
        raise ApprovalError("reviewer is required")
    candidate = load_candidate(knowledge_root, candidate_id)
    if candidate.get("status") in ("approved", "rejected", "deferred"):
        raise ApprovalError(
            f"candidate status is {candidate.get('status')}; create or reopen a revision before deferring"
        )
    resume_when = {
        "minimum_new_projects": max(0, int(minimum_new_projects)),
        "minimum_weighted_evidence": max(0.0, float(minimum_weighted_evidence)),
    }
    review = _candidate_review(
        knowledge_root,
        candidate,
        decision="defer",
        reviewer=reviewer,
        reason=reason,
        extra={"resume_when": resume_when},
    )
    if dry_run:
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "decision": "defer",
            "dry_run": True,
            "review": review,
            "message": "dry-run: nothing written",
        }
    write_review_record(knowledge_root, review)
    candidate_copy = dict(candidate)
    candidate_copy["status"] = "deferred"
    candidate_copy["updated_at"] = _now_iso()
    _atomic_write_json(candidate_path(knowledge_root, candidate_id), candidate_copy)
    refresh_counts(knowledge_root)
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "decision": "defer",
        "review_id": review["review_id"],
        "resume_when": resume_when,
        "candidate_status": "deferred",
    }


def reopen_candidate(
    knowledge_root: Path,
    candidate_id: str,
    *,
    reviewer: str,
    reason: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reopen a rejected/deferred candidate for re-review.

    This is the only safe path from rejected/deferred back to reviewing.
    A new review record is always generated; the original rejected/deferred
    history is preserved in the candidate's existing review records.
    """
    if not reason.strip():
        raise ApprovalError("reopen reason is required")
    if not reviewer.strip():
        raise ApprovalError("reviewer is required")
    candidate = load_candidate(knowledge_root, candidate_id)
    if candidate.get("status") not in ("rejected", "deferred"):
        raise ApprovalError(
            f"candidate status is {candidate.get('status')}; "
            "reopen only applies to rejected or deferred candidates"
        )
    review = _candidate_review(
        knowledge_root,
        candidate,
        decision="reopen",
        reviewer=reviewer,
        reason=reason,
    )
    if dry_run:
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "decision": "reopen",
            "dry_run": True,
            "review": review,
            "message": "dry-run: candidate status will be set to reviewing",
        }
    write_review_record(knowledge_root, review)
    candidate_copy = dict(candidate)
    candidate_copy["status"] = "reviewing"
    candidate_copy["updated_at"] = _now_iso()
    _atomic_write_json(candidate_path(knowledge_root, candidate_id), candidate_copy)
    refresh_counts(knowledge_root)
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "decision": "reopen",
        "previous_status": candidate.get("status"),
        "new_status": "reviewing",
        "review_id": review["review_id"],
    }


def _rule_records(
    knowledge_root: Path,
    rule_id: str | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    rules_dir = Path(knowledge_root).resolve() / "editing_rules"
    records: list[tuple[Path, dict[str, Any]]] = []
    if not rules_dir.is_dir():
        return records
    for path in sorted(rules_dir.glob("*.json")):
        try:
            rule = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rule, dict):
            continue
        if rule_id and rule.get("rule_id") != rule_id and path.stem != rule_id:
            continue
        records.append((path, rule))

    def revision_key(item: tuple[Path, dict[str, Any]]) -> tuple[int, str]:
        try:
            revision = int(item[1].get("revision") or 0)
        except (TypeError, ValueError):
            revision = -1
        return revision, item[0].name

    records.sort(
        key=revision_key
    )
    return records


def _latest_rule_record(
    knowledge_root: Path,
    rule_id: str,
) -> tuple[Path, dict[str, Any]]:
    records = _rule_records(knowledge_root, rule_id)
    if not records:
        raise ApprovalError(f"rule not found: {rule_id}")
    return records[-1]


def list_rules(knowledge_root: Path) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    for path, rule in _rule_records(knowledge_root):
        integrity_errors = validate_rule_integrity(knowledge_root, rule)
        rules.append(
            {
                "rule_id": rule.get("rule_id"),
                "revision": rule.get("revision"),
                "version": rule.get("version"),
                "source_candidate_id": rule.get("source_candidate_id"),
                "rule_type": rule.get("rule_type"),
                "category": rule.get("category"),
                "expression": rule.get("expression"),
                "scope": rule.get("scope"),
                "status": rule.get("status"),
                "active": rule.get("active"),
                "confidence_at_approval": rule.get("confidence_at_approval"),
                "evidence_status": rule.get("evidence_status"),
                "integrity_valid": not integrity_errors,
                "integrity_errors": integrity_errors,
                "file": path.name,
            }
        )
    return {"ok": True, "rule_count": len(rules), "rules": rules}


def explain_rule(knowledge_root: Path, rule_id: str) -> dict[str, Any]:
    path, rule = _latest_rule_record(knowledge_root, rule_id)
    canonical_rule_id = str(rule.get("rule_id") or rule_id)
    review_id = rule.get("review_id") or (rule.get("approval") or {}).get("review_id")
    review: dict[str, Any] | None = None
    if review_id:
        review_path = _review_path(knowledge_root, str(review_id))
        if review_path.is_file():
            try:
                review = json.loads(review_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                review = None
    candidate: dict[str, Any] | None = None
    candidate_id = rule.get("source_candidate_id")
    if candidate_id:
        candidate_file = candidate_path(knowledge_root, candidate_id)
        if candidate_file.is_file():
            try:
                candidate = json.loads(candidate_file.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                candidate = None
    missing_sources: list[str] = []
    for item in rule.get("evidence_snapshot", []) or []:
        source_file = str(item.get("source_file") or "")
        kind = str(item.get("kind") or "")
        if source_file:
            source_path = _evidence_file(knowledge_root, kind, source_file)
            if not source_path.is_file():
                missing_sources.append(source_file)
    integrity_errors = validate_rule_integrity(knowledge_root, rule)
    return {
        "ok": True,
        "rule_id": rule_id,
        "rule_revision": rule.get("revision"),
        "rule_file": str(path),
        "rule": rule,
        "review": review,
        "candidate": candidate,
        "missing_sources": missing_sources,
        "evidence_status": "invalid" if integrity_errors else "valid",
        "integrity_valid": not integrity_errors,
        "integrity_errors": integrity_errors,
    }


def _transition_rule_lifecycle(
    knowledge_root: Path,
    rule_id: str,
    *,
    decision: str,
    target_status: str,
    reviewer: str,
    reason: str,
    application_mode: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not reason.strip():
        raise ApprovalError(f"{decision} reason is required")
    if not reviewer.strip():
        raise ApprovalError("reviewer is required")
    path, rule = _latest_rule_record(knowledge_root, rule_id)
    canonical_rule_id = str(rule.get("rule_id") or rule_id)
    integrity_errors = validate_rule_integrity(knowledge_root, rule)
    if integrity_errors:
        raise ApprovalError(
            "rule integrity invalid; lifecycle transition denied: "
            + "; ".join(integrity_errors)
        )
    if rule.get("status") == target_status:
        if decision == "activate" and application_mode not in RULE_APPLICATION_MODES:
            raise ApprovalError("activation application_mode must be advisory")
        return {
            "ok": True,
            "rule_id": canonical_rule_id,
            "rule_revision": rule.get("revision"),
            "decision": decision,
            "status": target_status,
            "idempotent": True,
            "rule_kept": True,
        }
    if rule.get("status") == "revoked":
        raise ApprovalError("revoked rule cannot transition to another lifecycle state")
    current_status = str(rule.get("status") or "")
    if decision == "activate":
        if current_status != "inactive":
            raise ApprovalError(
                f"only inactive rules can be activated; current status is {current_status}"
            )
        if application_mode not in RULE_APPLICATION_MODES:
            raise ApprovalError("activation application_mode must be advisory")
    elif decision == "deactivate":
        if current_status != "active":
            raise ApprovalError(
                f"only active rules can be deactivated; current status is {current_status}"
            )
        if application_mode is not None:
            raise ApprovalError("deactivation does not accept application_mode")
    elif application_mode is not None:
        raise ApprovalError(f"{decision} does not accept application_mode")
    review_id = review_id_for(
        f"{canonical_rule_id}-v{int(rule.get('revision') or 0)}"
    )
    rule_binding = {
        "rule_id": canonical_rule_id,
        "revision": rule.get("revision"),
        "content_hash": (rule.get("content_hash") or {}).get("sha256"),
        "previous_status": rule.get("status"),
        "target_status": target_status,
    }
    if decision == "activate":
        rule_binding["application_mode"] = application_mode
    review = _seal_review({
        "schema_version": SCHEMA_VERSION,
        "review_type": "rule_lifecycle",
        "review_id": review_id,
        "decision": decision,
        "rule_binding": rule_binding,
        "reviewer": {"type": "human", "name": reviewer},
        "reason": reason,
        "reviewed_at": _now_iso(),
    })
    history = deepcopy((rule.get("lifecycle") or {}).get("history") or [])
    previous_hash = (history[-1].get("event_hash") or {}).get("sha256")
    lifecycle_event = _lifecycle_event(
        event=decision,
        previous_status=str(rule.get("status") or ""),
        status=target_status,
        reviewer=reviewer,
        reason=reason,
        review_id=review_id,
        previous_hash=str(previous_hash or "") or None,
        application_mode=application_mode if decision == "activate" else None,
    )
    history.append(lifecycle_event)
    updated = deepcopy(rule)
    updated["status"] = target_status
    updated["active"] = target_status == "active"
    updated["lifecycle"] = {
        "status": target_status,
        "revision": len(history),
        "history": history,
    }
    if decision == "activate":
        updated["activation"] = {
            "reviewer": reviewer,
            "reason": reason,
            "rule_id": canonical_rule_id,
            "rule_revision": rule.get("revision"),
            "rule_content_hash": (rule.get("content_hash") or {}).get("sha256"),
            "review_id": review_id,
            "activated_at": lifecycle_event["at"],
            "application_mode": application_mode,
        }
    else:
        updated.pop("activation", None)
        timestamp_field = {
            "deactivate": "deactivated_at",
            "deprecate": "deprecated_at",
            "revoke": "revoked_at",
        }[decision]
        reason_field = {
            "deactivate": "deactivation_reason",
            "deprecate": "deprecation_reason",
            "revoke": "revoke_reason",
        }[decision]
        updated[timestamp_field] = lifecycle_event["at"]
        updated[reason_field] = reason
    updated["updated_at"] = _now_iso()
    if dry_run:
        return {
            "ok": True,
            "rule_id": canonical_rule_id,
            "rule_revision": rule.get("revision"),
            "decision": decision,
            "dry_run": True,
            "review": review,
            "editing_rule": updated,
            "message": f"dry-run: rule will be marked {target_status} (not deleted)",
        }
    write_review_record(knowledge_root, review)
    _atomic_write_json(path, updated)
    refresh_counts(knowledge_root)
    return {
        "ok": True,
        "rule_id": canonical_rule_id,
        "rule_revision": rule.get("revision"),
        "decision": decision,
        "status": target_status,
        "review_id": review["review_id"],
        "rule_kept": True,
    }


def activate_rule(
    knowledge_root: Path,
    rule_id: str,
    *,
    reviewer: str,
    reason: str,
    application_mode: str = "advisory",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Human-activate one exact approved rule revision for advisory use."""
    return _transition_rule_lifecycle(
        knowledge_root,
        rule_id,
        decision="activate",
        target_status="active",
        reviewer=reviewer,
        reason=reason,
        application_mode=application_mode,
        dry_run=dry_run,
    )


def deactivate_rule(
    knowledge_root: Path,
    rule_id: str,
    *,
    reviewer: str,
    reason: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Human-deactivate a rule while preserving its complete audit history."""
    return _transition_rule_lifecycle(
        knowledge_root,
        rule_id,
        decision="deactivate",
        target_status="inactive",
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
    )


def deprecate_rule(
    knowledge_root: Path,
    rule_id: str,
    *,
    reviewer: str,
    reason: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    return _transition_rule_lifecycle(
        knowledge_root,
        rule_id,
        decision="deprecate",
        target_status="deprecated",
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
    )


def revoke_rule(
    knowledge_root: Path,
    rule_id: str,
    *,
    reviewer: str,
    reason: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    return _transition_rule_lifecycle(
        knowledge_root,
        rule_id,
        decision="revoke",
        target_status="revoked",
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
    )
