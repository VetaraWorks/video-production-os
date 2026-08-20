"""Rule match explanation and traceability for Video OS (Phase 4.5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def explain_match(
    knowledge_dir: Path,
    report_path: Path,
    rule_id: str,
) -> dict[str, Any]:
    """Explain one rule match with full traceability:
    match -> editing_rule -> review -> rule_candidate -> feedback/repair_log."""
    knowledge_dir = Path(knowledge_dir).expanduser().resolve()
    report = _load_json(Path(report_path).expanduser().resolve())
    if report is None:
        raise ValueError(f"report not found or invalid: {report_path}")
    match = next(
        (item for item in report.get("matches", []) if item.get("rule_id") == rule_id),
        None,
    )
    if match is None:
        raise ValueError(f"rule {rule_id} not found in report")

    from .rule_approval import _latest_rule_record

    try:
        _rule_file, rule = _latest_rule_record(knowledge_dir, rule_id)
    except Exception:
        rule = None

    review: dict[str, Any] | None = None
    review_id = (match.get("approval") or {}).get("review_id")
    if review_id:
        review_file = knowledge_dir / "reviews" / f"review-{review_id}.json"
        review = _load_json(review_file) if review_file.is_file() else None

    candidate: dict[str, Any] | None = None
    candidate_id = match.get("source_candidate_id")
    if candidate_id:
        candidate_file = knowledge_dir / "rule_candidates" / f"{candidate_id}.json"
        candidate = _load_json(candidate_file) if candidate_file.is_file() else None

    evidence_details: list[dict[str, Any]] = []
    for item in match.get("evidence_snapshot", []) or []:
        source_file = str(item.get("source_file") or "")
        kind = str(item.get("kind") or "")
        source_path: Path | None = None
        if kind == "feedback":
            source_path = knowledge_dir / "edits" / source_file
        elif kind in ("repair_log", "production_evidence"):
            source_path = knowledge_dir / "repair_log" / source_file
        detail = dict(item)
        detail["file_present"] = source_path is not None and source_path.is_file()
        evidence_details.append(detail)

    stale = bool(match.get("execution_status") in ("not_applicable",))
    return {
        "ok": True,
        "rule_id": rule_id,
        "match": match,
        "rule": rule,
        "review": review,
        "candidate": candidate,
        "evidence": evidence_details,
        "summary": match.get("explanation"),
        "execution_status": match.get("execution_status"),
        "why_not_executed": (
            "L0 read-only preview; rules never change edit_plan in Phase 4.5"
        ),
    }


def validate_memory_api(knowledge_dir: Path) -> dict[str, Any]:
    """Self-check for the memory read API against the formal knowledge base."""
    from .memory_reader import load_rules
    from .rule_matcher import match_rules

    rules, invalid = load_rules(knowledge_dir)
    context = {
        "schema_version": 1,
        "project": "formal-check",
        "version": "unknown",
        "video_type": None,
        "client": None,
        "style_profile": None,
        "platform": None,
        "duration_target_s": None,
        "available_metrics": {},
    }
    report = match_rules(context, rules, invalid)
    return {
        "ok": True,
        "api": "memory_read_l0",
        "rules_scanned": report["summary"]["rules_scanned"],
        "matches": len(report["matches"]),
        "formal_editing_rules": len(rules),
    }
