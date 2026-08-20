"""Rule candidate extraction for Video OS (Phase 4.3).

Deterministic aggregation:
    knowledge/edits/ (feedback v2, rule_class=editing, structured metrics)
  + knowledge/repair_log/ (executed repairs, weight 0.5)
    -> knowledge/rule_candidates/*.json  (status=candidate only)

This layer only discovers possible patterns. It never writes editing_rules/,
never approves or applies candidates, never changes edit_plan, and never calls
models. Zero candidates is a valid result when evidence is insufficient.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .knowledge import _atomic_write_json, load_manifest, refresh_counts
from .knowledge_root import require_knowledge_root
from .production_evidence import (
    EvidenceValidationError,
    TIER_PRODUCTION_VERIFIED,
    validate_production_seal,
)


SCHEMA_VERSION = 1
MIN_WEIGHTED_EVIDENCE = 2.0
MIN_RECORDS = 2
MIN_INDEPENDENT_PROJECTS = 2
MIN_CONSISTENCY = 0.6
EVIDENCE_WEIGHTS = {
    "feedback": 1.0,
    "repair_log": 0.5,
    "production_evidence": 1.0,
}
FEEDBACK_WEIGHT = EVIDENCE_WEIGHTS["feedback"]
REPAIR_WEIGHT = EVIDENCE_WEIGHTS["repair_log"]
PRODUCTION_EVIDENCE_WEIGHT = EVIDENCE_WEIGHTS["production_evidence"]
DUPLICATE_KEYWORDS = ("重复", "duplicate", "reuse", "重复使用", "再次使用", "same")
RULE_TYPES = {"timing", "rhythm", "shot_selection"}
REVIEWED_CANDIDATE_STATUSES = frozenset(
    {"reviewing", "approved", "rejected", "deferred"}
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def candidate_material_key(payload: dict[str, Any]) -> str:
    """Return the evidence-derived candidate content, excluding governance state."""
    copy = dict(payload)
    for field in (
        "candidate_id",
        "rule_id",
        "lineage_id",
        "revision",
        "supersedes_candidate_id",
        "status",
        "created_at",
        "updated_at",
    ):
        copy.pop(field, None)
    return _norm_json(copy)


def candidate_content_hash(payload: dict[str, Any]) -> str:
    """Bind a review to evidence-derived candidate content, not mutable status."""
    return hashlib.sha256(candidate_material_key(payload).encode("utf-8")).hexdigest()


def candidate_id_for(
    rule_class: str,
    category: str,
    rule_type: str,
    expression: dict[str, Any],
    scope: dict[str, Any],
) -> str:
    key = _norm_json(
        {
            "rule_class": rule_class,
            "category": category,
            "rule_type": rule_type,
            "expression": expression,
            "scope": scope,
        }
    )
    return "cand-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def candidate_lineage_id_for(
    rule_class: str,
    category: str,
    rule_type: str,
    expression: dict[str, Any],
    scope: dict[str, Any],
) -> str:
    """Identify a logical rule family independently of changing evidence values."""
    if rule_type == "shot_selection":
        subject = {"constraint": expression.get("constraint")}
    else:
        subject = {"metric": expression.get("metric")}
    key = _norm_json(
        {
            "rule_class": rule_class,
            "category": category,
            "rule_type": rule_type,
            "subject": subject,
            "scope": scope,
        }
    )
    return "lineage-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def candidate_lineage_id_for_payload(payload: dict[str, Any]) -> str:
    existing = str(payload.get("lineage_id") or "").strip()
    if existing:
        return existing
    return candidate_lineage_id_for(
        str(payload.get("rule_class") or ""),
        str(payload.get("category") or ""),
        str(payload.get("rule_type") or ""),
        payload.get("expression") if isinstance(payload.get("expression"), dict) else {},
        payload.get("scope") if isinstance(payload.get("scope"), dict) else {},
    )


def _candidate_revision(payload: dict[str, Any]) -> int:
    try:
        return max(1, int(payload.get("revision") or 1))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------- evidence collection


def _source_refs(record: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("source_docs", "snapshot_refs"):
        values = record.get(key) or []
        if isinstance(values, list):
            refs.extend(str(item) for item in values if str(item).strip())
    return refs


def _direction_from_metric(change: dict[str, Any]) -> tuple[str, str, float, float] | None:
    """Return (metric, direction, before_value, after_value) for structured metrics."""
    before = change.get("before") or {}
    after = change.get("after") or {}
    before_metric = before.get("metric") if isinstance(before, dict) else None
    after_metric = after.get("metric") if isinstance(after, dict) else None
    if not isinstance(before_metric, dict) or not isinstance(after_metric, dict):
        return None
    metric = str(before_metric.get("name") or "")
    if not metric or metric != str(after_metric.get("name") or ""):
        return None
    try:
        before_value = float(before_metric["value"])
        after_value = float(after_metric["value"])
    except (TypeError, ValueError, KeyError):
        return None
    if abs(after_value - before_value) < 1e-9:
        return None
    direction = "decrease" if after_value < before_value else "increase"
    return metric, direction, before_value, after_value


def _rule_type_for(category: str, metric: str) -> str:
    if category == "shot_selection":
        return "shot_selection"
    if category == "rhythm":
        return "rhythm"
    lowered = metric.casefold()
    if any(keyword in lowered for keyword in ("shot_duration", "shot_interval", "interval", "duration_s")):
        return "rhythm"
    return "timing"


def _feedback_evidence(
    record: dict[str, Any],
    source_file: str,
    change: dict[str, Any],
) -> dict[str, Any] | None:
    if change.get("rule_class") != "editing":
        return None
    if change.get("status") not in ("pending", "referenced", "archived"):
        return None
    project = str(record.get("project") or "").strip()
    to_version = str(record.get("to_version") or "").strip()
    from_version = str(record.get("from_version") or "").strip()
    feedback_id = str(record.get("feedback_id") or "").strip()
    if not (project and to_version and feedback_id):
        return None
    source_refs = _source_refs(record)
    category = str(change.get("category") or "other")
    structured = change.get("rule_candidate_structured")
    if isinstance(structured, dict) and str(structured.get("constraint") or "").strip():
        return {
            "kind": "feedback",
            "rule_type": "shot_selection",
            "constraint": str(structured["constraint"]).strip(),
            "category": category,
            "direction": str(structured["constraint"]).strip(),
            "weight": FEEDBACK_WEIGHT,
            "ref": feedback_id,
            "project": project,
            "version": to_version,
            "from_version": from_version,
            "source_file": source_file,
            "snapshot_ref": _first_ref(record),
            "source_refs": source_refs,
        }
    metric_info = _direction_from_metric(change)
    operator_value: tuple[str, float] | None = None
    if isinstance(structured, dict) and str(structured.get("metric") or "").strip():
        try:
            operator_value = (
                str(structured["operator"]),
                float(structured["value"]),
            )
        except (TypeError, ValueError, KeyError):
            operator_value = None
    if metric_info is None and operator_value is None:
        return None  # fuzzy natural language: never guess numeric rules
    if metric_info is not None:
        metric, direction, before_value, after_value = metric_info
        if operator_value is None:
            operator, value = ("<=", after_value) if direction == "decrease" else (">=", after_value)
        else:
            operator, value = operator_value
        rule_type = _rule_type_for(category, metric)
    else:
        metric = str(structured["metric"])
        operator, value = operator_value  # type: ignore[misc]
        direction = "decrease" if operator == "<=" else "increase"
        rule_type = _rule_type_for(category, metric)
    return {
        "kind": "feedback",
        "rule_type": rule_type,
        "metric": metric,
        "operator": operator,
        "value": round(value, 3),
        "direction": direction,
        "category": category,
        "weight": FEEDBACK_WEIGHT,
        "ref": feedback_id,
        "project": project,
        "version": to_version,
        "from_version": from_version,
        "source_file": source_file,
        "snapshot_ref": _first_ref(record),
        "source_refs": source_refs,
    }


def _first_ref(record: dict[str, Any]) -> str:
    refs = _source_refs(record)
    return refs[0] if refs else ""


def _repair_evidence(
    entry: dict[str, Any],
    source_file: str,
) -> Iterable[dict[str, Any]]:
    project = str(entry.get("project") or "").strip()
    version = str(entry.get("version") or "").strip()
    if not (project and version):
        return
    actions = entry.get("actions") or []
    if not isinstance(actions, list):
        return
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "")
        if action_type == "replace_clip":
            reason = str(action.get("reason") or "")
            if any(keyword in reason for keyword in DUPLICATE_KEYWORDS):
                yield {
                    "kind": "repair_log",
                    "rule_type": "shot_selection",
                    "constraint": "avoid_duplicate_visual_fingerprint",
                    "category": "shot_selection",
                    "direction": "avoid_duplicate_visual_fingerprint",
                    "weight": REPAIR_WEIGHT,
                    "ref": f"{project}-{version}-action-{index + 1}",
                    "project": project,
                    "version": version,
                    "from_version": str(entry.get("from_version") or "unknown"),
                    "source_file": source_file,
                    "snapshot_ref": str(entry.get("source") or ""),
                    "source_refs": [str(item) for item in (entry.get("source_reports") or []) if str(item).strip()],
                }
        elif action_type == "adjust_trim":
            before = action.get("before") or {}
            after = action.get("after") or {}
            try:
                before_duration = float(before.get("duration"))
                after_duration = float(after.get("duration"))
            except (TypeError, ValueError):
                continue
            if after_duration < before_duration - 1e-9:
                yield {
                    "kind": "repair_log",
                    "rule_type": "rhythm",
                    "metric": "shot_duration_s",
                    "operator": "<=",
                    "value": round(after_duration, 3),
                    "direction": "decrease",
                    "category": "rhythm",
                    "weight": REPAIR_WEIGHT,
                    "ref": f"{project}-{version}-action-{index + 1}",
                    "project": project,
                    "version": version,
                    "from_version": str(entry.get("from_version") or "unknown"),
                    "source_file": source_file,
                    "snapshot_ref": str(entry.get("source") or ""),
                    "source_refs": [str(item) for item in (entry.get("source_reports") or []) if str(item).strip()],
                }


def _production_repair_evidence(
    entry: dict[str, Any],
    source_file: str,
) -> Iterable[dict[str, Any]]:
    """Convert only sealed structured production evidence; never infer prose."""
    project = str(entry.get("project") or "").strip()
    project_id = str(entry.get("project_id") or "").strip()
    identity = entry.get("source_identity") if isinstance(entry.get("source_identity"), dict) else {}
    run_id = str(identity.get("run_id") or "").strip()
    evidence_id = str(entry.get("evidence_id") or "").strip()
    if not (project and project_id and run_id and evidence_id):
        return
    issues = {
        str(item.get("issue_id") or ""): str(item.get("category") or "")
        for item in entry.get("issues") or []
        if isinstance(item, dict)
    }
    references = (entry.get("provenance") or {}).get("references") or {}
    source_refs = [
        str(item.get("path") or "")
        for item in references.values()
        if isinstance(item, dict) and str(item.get("path") or "")
    ]
    snapshot_ref = str((references.get("video_after") or {}).get("path") or "")
    for action in entry.get("actions") or []:
        if not isinstance(action, dict):
            continue
        action_id = str(action.get("action_id") or "")
        action_type = str(action.get("type") or "")
        categories = {
            issues.get(str(ref), "") for ref in (action.get("issue_refs") or [])
        }
        if action_type == "replace_clip" and "duplicate_shot" in categories:
            yield {
                "kind": "production_evidence",
                "rule_type": "shot_selection",
                "constraint": "avoid_duplicate_visual_fingerprint",
                "category": "shot_selection",
                "direction": "avoid_duplicate_visual_fingerprint",
                "weight": PRODUCTION_EVIDENCE_WEIGHT,
                "ref": f"{evidence_id}-{action_id}",
                "project": project,
                "source_id": project_id,
                "version": run_id,
                "from_version": str((entry.get("video") or {}).get("before", {}).get("sha256") or "unknown")[:16],
                "source_file": source_file,
                "snapshot_ref": snapshot_ref,
                "source_refs": source_refs,
            }
            continue
        metric = str(action.get("metric") or action.get("field") or "").strip()
        operator = str(action.get("operator") or "").strip()
        if not metric or operator not in {"<=", ">="}:
            continue
        try:
            value = float(action.get("value"))
        except (TypeError, ValueError):
            continue
        before = action.get("before")
        after = action.get("after")
        before_value = before.get("duration") if isinstance(before, dict) else before
        after_value = after.get("duration") if isinstance(after, dict) else after
        try:
            direction = "decrease" if float(after_value) < float(before_value) else "increase"
        except (TypeError, ValueError):
            direction = "decrease" if operator == "<=" else "increase"
        category = "rhythm" if metric in {"shot_duration_s", "segment.duration"} else "timing"
        yield {
            "kind": "production_evidence",
            "rule_type": _rule_type_for(category, metric),
            "metric": metric,
            "operator": operator,
            "value": round(value, 3),
            "direction": direction,
            "category": category,
            "weight": PRODUCTION_EVIDENCE_WEIGHT,
            "ref": f"{evidence_id}-{action_id}",
            "project": project,
            "source_id": project_id,
            "version": run_id,
            "from_version": str((entry.get("video") or {}).get("before", {}).get("sha256") or "unknown")[:16],
            "source_file": source_file,
            "snapshot_ref": snapshot_ref,
            "source_refs": source_refs,
        }
def collect_evidence(knowledge_root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    knowledge_root = require_knowledge_root(knowledge_root)
    evidence: list[dict[str, Any]] = []
    stats = {
        "feedback_scanned": 0,
        "feedback_structured": 0,
        "feedback_excluded_unverified": 0,
        "feedback_excluded_style_or_audit": 0,
        "feedback_excluded_fuzzy": 0,
        "feedback_excluded_other": 0,
        "repair_log_scanned": 0,
        "repair_log_excluded_unverified": 0,
        "repair_evidence_collected": 0,
        "production_evidence_scanned": 0,
        "production_evidence_excluded_invalid": 0,
        "production_evidence_collected": 0,
    }
    edits_dir = knowledge_root / "edits"
    for path in sorted(edits_dir.glob("*.json")):
        if path.name.endswith(".draft.json"):
            stats["feedback_excluded_other"] += 1
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            stats["feedback_excluded_other"] += 1
            continue
        if not isinstance(record, dict) or int(record.get("schema_version", 0)) != 2:
            stats["feedback_excluded_other"] += 1
            continue
        stats["feedback_scanned"] += 1
        if record.get("evidence_tier") != TIER_PRODUCTION_VERIFIED:
            stats["feedback_excluded_unverified"] += 1
            continue
        changes = record.get("changes") or []
        structured_hit = False
        for change in changes:
            if not isinstance(change, dict):
                continue
            rule_class = str(change.get("rule_class") or "")
            if rule_class not in ("editing",):
                if rule_class in ("style", "audit"):
                    stats["feedback_excluded_style_or_audit"] += 1
                continue
            item = _feedback_evidence(record, path.name, change)
            if item is None:
                stats["feedback_excluded_fuzzy"] += 1
                continue
            structured_hit = True
            evidence.append(item)
        if structured_hit:
            stats["feedback_structured"] += 1
    repair_dir = knowledge_root / "repair_log"
    for path in sorted(repair_dir.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("evidence_kind") in {"automatic_repair", "manual_edit"}:
            stats["production_evidence_scanned"] += 1
            if entry.get("evidence_tier") != TIER_PRODUCTION_VERIFIED:
                stats["repair_log_excluded_unverified"] += 1
                continue
            try:
                validate_production_seal(entry)
            except EvidenceValidationError:
                stats["production_evidence_excluded_invalid"] += 1
                continue
            for item in _production_repair_evidence(entry, path.name):
                evidence.append(item)
                stats["production_evidence_collected"] += 1
            continue
        stats["repair_log_scanned"] += 1
        if entry.get("evidence_tier") != TIER_PRODUCTION_VERIFIED:
            stats["repair_log_excluded_unverified"] += 1
            continue
        for item in _repair_evidence(entry, path.name):
            evidence.append(item)
            stats["repair_evidence_collected"] += 1
    return evidence, stats


# ---------------------------------------------------------------- aggregation


def _group_key(item: dict[str, Any]) -> tuple[str, str]:
    if item["rule_type"] == "shot_selection":
        return ("shot_selection", item["constraint"])
    return (item["rule_type"], item["metric"])


def _dedup_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in evidence:
        key = (
            str(item.get("source_id") or item["project"]),
            str(item["version"]),
            str(item["ref"]),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _split_support_conflict(
    rule_type: str,
    evidence: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if rule_type == "shot_selection":
        return evidence, []
    directions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        directions[item["direction"]].append(item)
    if len(directions) == 1:
        return evidence, []
    main_direction = max(
        directions,
        key=lambda key: sum(float(item["weight"]) for item in directions[key]),
    )
    support = directions[main_direction]
    conflicts = [
        item for direction, items in directions.items() if direction != main_direction for item in items
    ]
    return support, conflicts


def _expression_value(evidence: list[dict[str, Any]], operator: str) -> float:
    values = [float(item["value"]) for item in evidence]
    return max(values) if operator == "<=" else min(values)


def _confidence_factors(
    support: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    *,
    source_completeness: float,
) -> tuple[float, dict[str, float]]:
    weighted = sum(float(item["weight"]) for item in support)
    conflict_weighted = sum(float(item["weight"]) for item in conflicts)
    projects = {item["project"] for item in support}
    versions = {(item["project"], item["version"]) for item in support}
    human_count = sum(1 for item in support if item["kind"] == "feedback")
    repair_count = sum(1 for item in support if item["kind"] == "repair_log")
    evidence_count = len(support)

    evidence_factor = min(weighted / 5.0, 1.0)
    consistency = (
        weighted / (weighted + conflict_weighted)
        if conflict_weighted > 0
        else 1.0
    )
    diversity = 0.5 * min(len(projects) / 3.0, 1.0) + 0.5 * min(len(versions) / 4.0, 1.0)
    human_ratio = (
        human_count / evidence_count if evidence_count else 0.0
    )
    conflict_penalty = (
        conflict_weighted / (weighted + conflict_weighted)
        if weighted + conflict_weighted > 0
        else 0.0
    )
    confidence = (
        0.30 * evidence_factor
        + 0.25 * consistency
        + 0.15 * diversity
        + 0.10 * human_ratio
        + 0.20 * (1.0 - conflict_penalty)
    ) * source_completeness
    factors = {
        "evidence": round(evidence_factor, 3),
        "consistency": round(consistency, 3),
        "diversity": round(diversity, 3),
        "human_ratio": round(human_ratio, 3),
        "conflict_penalty": round(conflict_penalty, 3),
        "source_completeness": round(source_completeness, 3),
    }
    return round(max(0.0, min(1.0, confidence)), 3), factors


def _source_completeness(evidence: list[dict[str, Any]]) -> float:
    if not evidence:
        return 0.0
    complete = sum(
        1
        for item in evidence
        if item.get("ref")
        and item.get("project")
        and item.get("version")
        and item.get("source_file")
    )
    return complete / len(evidence)


def build_candidates(
    knowledge_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence, collect_stats = collect_evidence(knowledge_root)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        groups[_group_key(item)].append(item)

    candidates: list[dict[str, Any]] = []
    reasons: list[str] = []
    for key, raw_group in sorted(groups.items()):
        rule_type, metric_or_constraint = key
        group = _dedup_evidence(raw_group)
        support, conflicts = _split_support_conflict(rule_type, group)
        weighted = sum(float(item["weight"]) for item in support)
        conflict_weighted = sum(float(item["weight"]) for item in conflicts)
        if weighted < MIN_WEIGHTED_EVIDENCE:
            reasons.append(
                f"{rule_type}/{metric_or_constraint}: weighted evidence {weighted:.1f} < {MIN_WEIGHTED_EVIDENCE}"
            )
            continue
        unique_refs = {item["ref"] for item in support}
        if len(unique_refs) < MIN_RECORDS:
            reasons.append(
                f"{rule_type}/{metric_or_constraint}: only {len(unique_refs)} unique record(s) < {MIN_RECORDS}"
            )
            continue
        independent_projects = {
            str(item.get("source_id") or item["project"]) for item in support
        }
        if len(independent_projects) < MIN_INDEPENDENT_PROJECTS:
            reasons.append(
                f"{rule_type}/{metric_or_constraint}: only {len(independent_projects)} independent project(s) < {MIN_INDEPENDENT_PROJECTS}"
            )
            continue
        consistency = (
            weighted / (weighted + conflict_weighted)
            if conflict_weighted > 0
            else 1.0
        )
        if consistency < MIN_CONSISTENCY:
            reasons.append(
                f"{rule_type}/{metric_or_constraint}: consistency {consistency:.2f} < {MIN_CONSISTENCY}"
            )
            continue
        if conflict_weighted >= weighted:
            reasons.append(
                f"{rule_type}/{metric_or_constraint}: equal-or-stronger contradicting evidence; not emitted"
            )
            continue

        category = _category_for_group(rule_type, support)
        scope = {"video_type": None, "client": None, "style_profile": None}
        if rule_type == "shot_selection":
            constraint = metric_or_constraint
            expression = {"constraint": constraint}
            candidate_type = "qualitative"
            description = {
                "avoid_duplicate_visual_fingerprint": "避免重复使用相同视觉指纹的镜头",
            }.get(constraint, f"shot_selection 约束：{constraint}")
        else:
            operator = str(support[0].get("operator") or "<=")
            value = _expression_value(support, operator)
            expression = {"metric": metric_or_constraint, "operator": operator, "value": value}
            candidate_type = "quantitative"
            description = f"{category} 候选：{metric_or_constraint} {operator} {value}"

        projects = {str(item.get("source_id") or item["project"]) for item in support}
        versions = {(item["project"], item["version"]) for item in support}
        human_count = sum(1 for item in support if item["kind"] == "feedback")
        repair_count = sum(
            1
            for item in support
            if item["kind"] in {"repair_log", "production_evidence"}
        )
        source_completeness = _source_completeness(support)
        confidence, factors = _confidence_factors(
            support,
            conflicts,
            source_completeness=source_completeness,
        )
        now = _now_iso()
        candidate_id = candidate_id_for(
            "editing",
            category,
            rule_type,
            expression,
            scope,
        )
        lineage_id = candidate_lineage_id_for(
            "editing",
            category,
            rule_type,
            expression,
            scope,
        )
        evidence_items = [
            {
                "kind": item["kind"],
                "ref": item["ref"],
                "snapshot_ref": item.get("snapshot_ref") or "",
                "project": item["project"],
                "source_id": item.get("source_id") or item["project"],
                "version": item["version"],
                "source_file": item["source_file"],
            }
            for item in support
        ]
        contradicting = [
            {
                "kind": item["kind"],
                "ref": item["ref"],
                "snapshot_ref": item.get("snapshot_ref") or "",
                "project": item["project"],
                "source_id": item.get("source_id") or item["project"],
                "version": item["version"],
                "source_file": item["source_file"],
            }
            for item in conflicts
        ]
        candidates.append(
            {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "rule_id": candidate_id,
                "lineage_id": lineage_id,
                "revision": 1,
                "supersedes_candidate_id": None,
                "status": "candidate",
                "rule_class": "editing",
                "category": category,
                "rule_type": rule_type,
                "scope": scope,
                "expression": expression,
                "type": candidate_type,
                "metric": expression if rule_type != "shot_selection" else None,
                "description": description,
                "applicability": {"template": [], "segment_ids": [], "client_ids": []},
                "confidence": confidence,
                "confidence_factors": factors,
                "weighted_evidence": round(weighted, 3),
                "evidence_count": len(support),
                "project_count": len(projects),
                "version_count": len(versions),
                "human_feedback_count": human_count,
                "repair_evidence_count": repair_count,
                "support": len(support),
                "sources": sorted({item["source_file"] for item in support}),
                "evidence": evidence_items,
                "contradicting_evidence": contradicting,
                "created_at": now,
                "updated_at": now,
                "conflicts_with": [],
            }
        )
    return candidates, {
        **collect_stats,
        "candidate_count": len(candidates),
        "reasons": reasons,
        "evidence_total": len(evidence),
    }


def _category_for_group(rule_type: str, evidence: list[dict[str, Any]]) -> str:
    if rule_type == "shot_selection":
        return "shot_selection"
    categories = {item.get("category") for item in evidence if item.get("category")}
    if rule_type == "rhythm" and "rhythm" in categories:
        return "rhythm"
    return next(iter(categories), "rhythm" if rule_type == "rhythm" else "timing")


# ---------------------------------------------------------------- persistence


def validate_rule_candidate(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = (
        "candidate_id",
        "rule_id",
        "status",
        "rule_class",
        "category",
        "rule_type",
        "scope",
        "expression",
        "description",
        "confidence",
        "confidence_factors",
        "weighted_evidence",
        "evidence_count",
        "project_count",
        "version_count",
        "human_feedback_count",
        "repair_evidence_count",
        "evidence",
        "contradicting_evidence",
        "created_at",
        "updated_at",
    )
    for field in required:
        if field not in payload:
            errors.append(f"missing field: {field}")
    if payload.get("rule_type") not in RULE_TYPES:
        errors.append(f"invalid rule_type: {payload.get('rule_type')}")
    if payload.get("status") not in (
        "candidate",
        "reviewing",
        "approved",
        "rejected",
        "deferred",
        "conflicted",
        "stale",
    ):
        errors.append(f"unexpected status: {payload.get('status')}")
    if "lineage_id" in payload and not str(payload.get("lineage_id") or "").strip():
        errors.append("lineage_id must be a non-empty string")
    if "revision" in payload:
        try:
            if int(payload["revision"]) < 1:
                errors.append("revision must be >= 1")
        except (TypeError, ValueError):
            errors.append("revision must be an integer")
    expression = payload.get("expression")
    if not isinstance(expression, dict) or not expression:
        errors.append("expression must be a non-empty object")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence must be a list")
    return errors


def extract_rule_candidates(
    knowledge_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    knowledge_root = Path(knowledge_root).expanduser().resolve()
    candidates, stats = build_candidates(knowledge_root)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            **stats,
            "candidates_preview": [
                {
                    "candidate_id": item["candidate_id"],
                    "rule_type": item["rule_type"],
                    "category": item["category"],
                    "expression": item["expression"],
                    "confidence": item["confidence"],
                    "evidence_count": item["evidence_count"],
                    "weighted_evidence": item["weighted_evidence"],
                }
                for item in candidates
            ],
        }

    target_dir = knowledge_root / "rule_candidates"
    target_dir.mkdir(parents=True, exist_ok=True)
    existing_by_lineage: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for existing_path in sorted(target_dir.glob("*.json")):
        if existing_path.name.endswith(".draft.json"):
            continue
        try:
            existing_payload = json.loads(
                existing_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(existing_payload, dict):
            continue
        existing_by_lineage[
            candidate_lineage_id_for_payload(existing_payload)
        ].append((existing_path, existing_payload))

    current_ids: set[str] = set()
    written = 0
    updated = 0
    unchanged = 0
    revisions_created = 0
    for candidate in candidates:
        lineage_id = candidate_lineage_id_for_payload(candidate)
        lineage_entries = existing_by_lineage.get(lineage_id, [])
        lineage_entries.sort(
            key=lambda item: (
                _candidate_revision(item[1]),
                str(item[1].get("updated_at") or ""),
                item[0].name,
            )
        )
        previous_path, previous = lineage_entries[-1] if lineage_entries else (None, None)

        material_key = candidate_material_key(candidate)
        matching_entries = [
            item
            for item in lineage_entries
            if candidate_material_key(item[1]) == material_key
        ]
        if matching_entries:
            matching_path, matching = matching_entries[-1]
            current_ids.add(
                str(matching.get("candidate_id") or matching_path.stem)
            )
            unchanged += 1
            continue

        if previous is not None and previous.get("status") in REVIEWED_CANDIDATE_STATUSES:
            revision = _candidate_revision(previous) + 1
            candidate_id = f"{candidate['candidate_id']}-r{revision}"
            while (target_dir / f"{candidate_id}.json").exists():
                revision += 1
                candidate_id = f"{candidate['candidate_id']}-r{revision}"
            candidate["candidate_id"] = candidate_id
            candidate["rule_id"] = candidate_id
            candidate["revision"] = revision
            candidate["supersedes_candidate_id"] = str(
                previous.get("candidate_id") or previous_path.stem
            )
            path = target_dir / f"{candidate_id}.json"
            written += 1
            revisions_created += 1
        elif previous is not None:
            candidate_id = str(previous.get("candidate_id") or previous_path.stem)
            candidate["candidate_id"] = candidate_id
            candidate["rule_id"] = str(previous.get("rule_id") or candidate_id)
            candidate["revision"] = _candidate_revision(previous)
            candidate["supersedes_candidate_id"] = previous.get(
                "supersedes_candidate_id"
            )
            candidate["created_at"] = previous.get("created_at") or candidate["created_at"]
            path = previous_path
            updated += 1
        else:
            candidate_id = str(candidate["candidate_id"])
            path = target_dir / f"{candidate_id}.json"
            written += 1

        current_ids.add(candidate_id)
        candidate["updated_at"] = _now_iso()
        _atomic_write_json(path, candidate)
        existing_by_lineage.setdefault(lineage_id, []).append((path, candidate))

    stale_marked = 0
    for path in sorted(target_dir.glob("*.json")):
        if path.name.endswith(".draft.json"):
            continue
        candidate_id = path.stem
        if candidate_id in current_ids:
            continue
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if existing.get("status") in REVIEWED_CANDIDATE_STATUSES:
            continue
        if existing.get("status") == "stale":
            continue
        existing["status"] = "stale"
        existing["updated_at"] = _now_iso()
        _atomic_write_json(path, existing)
        stale_marked += 1

    refresh_counts(knowledge_root)
    return {
        "ok": True,
        "dry_run": False,
        **stats,
        "written": written,
        "updated": updated,
        "unchanged": unchanged,
        "revisions_created": revisions_created,
        "stale_marked": stale_marked,
        "candidate_count": len(candidates),
        "rule_candidates_dir": str(target_dir),
    }


def validate_rule_candidates(knowledge_root: Path) -> dict[str, Any]:
    knowledge_root = require_knowledge_root(knowledge_root)
    target_dir = knowledge_root / "rule_candidates"
    errors_by_file: dict[str, list[str]] = {}
    valid_count = 0
    if target_dir.is_dir():
        for path in sorted(target_dir.glob("*.json")):
            if path.name.endswith(".draft.json"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                errors_by_file[path.name] = [f"invalid JSON: {exc}"]
                continue
            errors = validate_rule_candidate(payload)
            if errors:
                errors_by_file[path.name] = errors
            else:
                valid_count += 1
    refresh_counts(knowledge_root)
    return {
        "ok": not errors_by_file,
        "valid_count": valid_count,
        "invalid": errors_by_file,
        "manifest": load_manifest(knowledge_root),
    }


def list_candidates(knowledge_root: Path) -> dict[str, Any]:
    knowledge_root = require_knowledge_root(knowledge_root)
    target_dir = knowledge_root / "rule_candidates"
    candidates: list[dict[str, Any]] = []
    if target_dir.is_dir():
        for path in sorted(target_dir.glob("*.json")):
            if path.name.endswith(".draft.json"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            candidates.append(
                {
                    "candidate_id": payload.get("candidate_id"),
                    "rule_type": payload.get("rule_type"),
                    "category": payload.get("category"),
                    "expression": payload.get("expression"),
                    "status": payload.get("status"),
                    "confidence": payload.get("confidence"),
                    "weighted_evidence": payload.get("weighted_evidence"),
                    "evidence_count": payload.get("evidence_count"),
                }
            )
    return {"ok": True, "candidate_count": len(candidates), "candidates": candidates}
