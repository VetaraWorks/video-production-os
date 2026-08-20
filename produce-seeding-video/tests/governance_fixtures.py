from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


def build_production_evidence(
    *,
    project_id: str,
    project: str,
    run_id: str,
    evidence_id: str,
    value: float = 8.0,
    metric: str = "product_first_appearance_s",
    field: str | None = None,
) -> dict[str, Any]:
    action_id = "action-1"
    record: dict[str, Any] = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "evidence_kind": "automatic_repair",
        "project_id": project_id,
        "project": project,
        "source_identity": {
            "project_id": project_id,
            "project": project,
            "run_id": run_id,
        },
        "evidence_tier": "production_verified",
        "tier_history": [],
        "created_at": "2026-08-09T00:00:00+00:00",
        "updated_at": "2026-08-09T00:00:00+00:00",
        "video": {
            "before": {"sha256": f"before-{evidence_id}"},
            "after": {"sha256": f"after-{evidence_id}"},
        },
        "issues": [],
        "actions": [
            {
                "action_id": action_id,
                "type": "adjust_timing",
                "field": field or metric,
                "metric": metric,
                "operator": "<=",
                "before": 22.0,
                "after": value,
                "value": value,
                "scope": {"kind": "whole_video"},
                "target": {"time_range": {"start": 0.0, "end": 1.0}},
                "reason": "production repair verified by QA and Review",
                "issue_refs": [],
            }
        ],
        "qa_result": {"ok": True},
        "post_review_result": {"status": "done", "verdict": "pass"},
        "verification": {"status": "production_verified", "errors": []},
        "provenance": {"references": {}},
        "knowledge_sync": {"status": "synced"},
    }
    digest = hashlib.sha256(_canonical_json(_gate_material(record)).encode("utf-8")).hexdigest()
    record["verification"]["gate"] = {
        "name": "production_evidence_gate_v1",
        "material_digest": digest,
        "checks": ["fixture-production-chain"],
        "verified_at": "2026-08-09T00:00:00+00:00",
    }
    record["tier_history"] = [
        {
            "from": "human_verified",
            "to": "production_verified",
            "actor": "fixture-production-gate",
            "reason": "verified production fixture",
            "at": "2026-08-09T00:00:00+00:00",
            "gate_material_digest": digest,
        }
    ]
    return record


def write_production_evidence(
    knowledge_root: Path,
    *,
    project_id: str,
    project: str,
    run_id: str,
    evidence_id: str,
    value: float = 8.0,
    metric: str = "product_first_appearance_s",
    field: str | None = None,
) -> Path:
    record = build_production_evidence(
        project_id=project_id,
        project=project,
        run_id=run_id,
        evidence_id=evidence_id,
        value=value,
        metric=metric,
        field=field,
    )
    path = Path(knowledge_root) / "repair_log" / f"{evidence_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def install_formal_rule(
    knowledge_root: Path,
    *,
    rule_key: str,
    expression: dict[str, Any],
    scope: dict[str, Any] | None = None,
    status: str = "inactive",
    evidence_status: str = "valid",
    confidence: float = 0.82,
) -> dict[str, Any]:
    """Install a fully bound v2 rule fixture without weakening production loaders."""
    from video_os_core.knowledge import _atomic_write_json, refresh_counts
    from video_os_core.rule_approval import (
        _candidate_binding,
        _candidate_review,
        _lifecycle_event,
        _seal_rule,
        activate_rule,
        deprecate_rule,
        revoke_rule,
        rule_file_name,
        rule_id_for_candidate,
        write_review_record,
    )

    root = Path(knowledge_root)
    safe = "".join(char if char.isalnum() else "-" for char in rule_key).strip("-")
    metric = str(expression.get("metric") or "fixture_constraint")
    numeric_value = expression.get("value")
    try:
        evidence_value = float(numeric_value)
    except (TypeError, ValueError):
        evidence_value = 1.0
    evidence_files = []
    for suffix in ("a", "b"):
        evidence_id = f"evidence-{safe}-{suffix}"
        evidence_files.append(
            write_production_evidence(
                root,
                project_id=f"project-{safe}-{suffix}",
                project=f"project-{safe}-{suffix}",
                run_id=f"run-{safe}-{suffix}",
                evidence_id=evidence_id,
                value=evidence_value,
                metric=metric,
            )
        )
    candidate_id = f"cand-{safe}"
    lineage_id = f"lineage-{hashlib.sha1(safe.encode('utf-8')).hexdigest()[:12]}"
    rule_type = "shot_selection" if "constraint" in expression else "timing"
    candidate = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "rule_id": candidate_id,
        "lineage_id": lineage_id,
        "revision": 1,
        "supersedes_candidate_id": None,
        "status": "candidate",
        "rule_class": "editing",
        "category": "shot_selection" if rule_type == "shot_selection" else "timing",
        "rule_type": rule_type,
        "scope": scope
        or {"video_type": None, "client": None, "style_profile": None},
        "expression": expression,
        "type": "qualitative" if rule_type == "shot_selection" else "quantitative",
        "metric": expression if rule_type != "shot_selection" else None,
        "description": f"formal fixture {safe}",
        "applicability": {"template": [], "segment_ids": [], "client_ids": []},
        "confidence": confidence,
        "confidence_factors": {
            "evidence": 1.0,
            "consistency": 1.0,
            "diversity": 1.0,
            "human_ratio": 0.0,
            "conflict_penalty": 0.0,
        },
        "weighted_evidence": 2.0,
        "evidence_count": 2,
        "project_count": 2,
        "version_count": 2,
        "human_feedback_count": 0,
        "repair_evidence_count": 2,
        "support": 2,
        "sources": [path.name for path in evidence_files],
        "evidence": [
            {
                "kind": "production_evidence",
                "ref": f"{path.stem}-action-1",
                "snapshot_ref": "",
                "project": f"project-{safe}-{suffix}",
                "source_id": f"project-{safe}-{suffix}",
                "version": f"run-{safe}-{suffix}",
                "source_file": path.name,
            }
            for path, suffix in zip(evidence_files, ("a", "b"))
        ],
        "contradicting_evidence": [],
        "created_at": "2026-08-09T00:00:00+00:00",
        "updated_at": "2026-08-09T00:00:00+00:00",
        "conflicts_with": [],
    }
    candidate_dir = root / "rule_candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    binding = _candidate_binding(root, candidate)
    review = _candidate_review(
        root,
        candidate,
        decision="approve",
        reviewer="fixture-human",
        reason="fixture production evidence approved by human",
        extra={"scope_adjustment": None, "conflict_resolution": None},
    )
    candidate["status"] = "approved"
    _atomic_write_json(candidate_dir / f"{candidate_id}.json", candidate)
    write_review_record(root, review)
    rule_id = rule_id_for_candidate(lineage_id)
    revision = 1
    review_id = str(review["review_id"])
    evidence_snapshot = binding["evidence_snapshot"]
    evidence_ids = [item["evidence_id"] for item in evidence_snapshot]
    operator = expression.get("operator") or "enforce"
    value = expression.get("value") if expression.get("metric") else expression.get("constraint")
    event = _lifecycle_event(
        event="approve",
        previous_status=None,
        status="inactive",
        reviewer="fixture-human",
        reason="fixture production evidence approved by human",
        review_id=review_id,
        previous_hash=None,
    )
    rule = {
        "schema_version": 2,
        "rule_id": rule_id,
        "revision": revision,
        "version": "v1",
        "lineage_id": lineage_id,
        "source_candidate_id": candidate_id,
        "source_candidate": {
            "candidate_id": candidate_id,
            "lineage_id": lineage_id,
            "revision": revision,
            "content_hash": binding["content_hash"],
        },
        "review_id": review_id,
        "review_hash": review["review_hash"]["sha256"],
        "evidence_ids": evidence_ids,
        "rule_class": "editing",
        "category": candidate["category"],
        "rule_type": rule_type,
        "type": candidate["type"],
        "scope": candidate["scope"],
        "expression": expression,
        "metric": expression.get("metric"),
        "field": expression.get("metric") or "constraint",
        "operator": operator,
        "value": value,
        "description": candidate["description"],
        "status": "inactive",
        "active": False,
        "confidence_at_approval": confidence,
        "evidence_snapshot": evidence_snapshot,
        "approval": {
            "review_id": review_id,
            "reviewer": "fixture-human",
            "reason": "fixture production evidence approved by human",
            "scope_adjustment": None,
            "conflict_resolution": None,
        },
        "provenance": {
            "candidate_id": candidate_id,
            "candidate_revision": revision,
            "candidate_content_hash": binding["content_hash"],
            "review_id": review_id,
            "review_hash": review["review_hash"]["sha256"],
            "evidence_ids": evidence_ids,
        },
        "supersedes": None,
        "lifecycle": {"status": "inactive", "revision": 1, "history": [event]},
        "created_at": "2026-08-09T00:00:00+00:00",
        "updated_at": "2026-08-09T00:00:00+00:00",
        "evidence_status": evidence_status,
        "conflicts_with": [],
    }
    rule = _seal_rule(rule)
    rules_dir = root / "editing_rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    rule_path = rules_dir / rule_file_name(rule_id, revision)
    _atomic_write_json(rule_path, rule)
    refresh_counts(root)
    if status == "deprecated":
        deprecate_rule(root, rule_id, reviewer="fixture-human", reason="fixture deprecated")
    elif status == "revoked":
        revoke_rule(root, rule_id, reviewer="fixture-human", reason="fixture revoked")
    elif status == "active":
        activate_rule(
            root,
            rule_id,
            reviewer="fixture-human",
            reason="fixture advisory activation by a human",
            application_mode="advisory",
        )
    elif status != "inactive":
        raise ValueError(f"unsupported formal fixture status: {status}")
    return json.loads(rule_path.read_text(encoding="utf-8"))
