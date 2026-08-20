"""Memory suggestion layer for Video OS (Phase 5.1, read-only advisory).

Reads approved editing rules plus the current project context and emits a
deterministic memory_suggestions.json. Suggestions are advisory only:
- They never modify edit_plan, project_state, render output, or any rule.
- The Planner is intentionally not wired to consume suggestions in this phase.

Formal knowledge with zero rules must produce an empty suggestion list.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .knowledge import _atomic_write_json
from .memory_reader import load_rules
from .rule_matcher import match_rules


SUGGESTION_SCHEMA_VERSION = 2
SUGGESTION_GENERATION_VERSION = "video-os-l0-suggestion-v2"
PROJECT_INPUT_SIGNATURE_ALGORITHM = "video-os-memory-project-input-v1"
EDIT_PLAN_SIGNATURE_ALGORITHM = "video-os-memory-edit-plan-v1"
SUGGESTION_HASH_ALGORITHM = "video-os-memory-suggestion-v2"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_id(project_dir: Path) -> str:
    state_path = project_dir / "project_state.json"
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
            state = payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            state = {}
    existing = str(state.get("project_id") or "").strip()
    if existing:
        return existing
    project = str(state.get("project") or project_dir.name)
    created_at = str(state.get("created_at") or project)
    return "project-" + hashlib.sha256(f"{project}\n{created_at}".encode("utf-8")).hexdigest()[:16]


def project_input_signature(project_dir: Path) -> dict[str, Any]:
    """Hash current script/media/config inputs without relying on stale reports."""
    project_dir = Path(project_dir).resolve()
    files: list[dict[str, Any]] = []
    roots = ("script", "raw_video", "material")
    paths: set[Path] = set()
    for name in roots:
        root = project_dir / name
        if root.is_dir():
            paths.update(path for path in root.rglob("*") if path.is_file())
    for relative in (Path("config/config.json"), Path("config/project_context.json")):
        path = project_dir / relative
        if path.is_file():
            paths.add(path)
    for path in sorted(paths, key=lambda item: item.relative_to(project_dir).as_posix()):
        relative = path.relative_to(project_dir).as_posix()
        files.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    script_path = project_dir / "script" / "script.txt"
    if not script_path.is_file() or not script_path.read_bytes().strip():
        raise ValueError("memory suggestion requires non-empty script/script.txt")
    material = {"algorithm": PROJECT_INPUT_SIGNATURE_ALGORITHM, "files": files}
    return {**material, "digest_sha256": _sha256_json(material)}


def edit_plan_signature(project_dir: Path) -> dict[str, Any]:
    path = Path(project_dir).resolve() / "output" / "edit_plan.json"
    if not path.is_file():
        raise ValueError("memory suggestion requires output/edit_plan.json")
    material = {
        "algorithm": EDIT_PLAN_SIGNATURE_ALGORITHM,
        "path": "output/edit_plan.json",
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }
    return {**material, "digest_sha256": _sha256_json(material)}


def suggestion_content_hash(suggestion: dict[str, Any]) -> str:
    material = deepcopy(suggestion)
    material.pop("suggestion_hash", None)
    return _sha256_json(material)


def build_project_context(project_dir: Path) -> dict[str, Any]:
    """Deterministic L0 context from existing project artifacts.
    video_type/client/style_profile/platform come only from an explicit
    config/project_context.json declaration; never guessed from names."""
    project_dir = Path(project_dir).resolve()
    config: dict[str, Any] = {}
    config_path = project_dir / "config" / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            config = {}
    duration_target: float | None = None
    try:
        duration_target = float(config.get("duration_seconds"))
    except (TypeError, ValueError):
        duration_target = None

    meta: dict[str, Any] = {}
    meta_path = project_dir / "config" / "project_context.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            meta = {}

    metrics: dict[str, float] = {}
    plan: dict[str, Any] = {}
    plan_path = project_dir / "output" / "edit_plan.json"
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            plan = {}
    if isinstance(plan, dict) and plan.get("segments"):
        durations = [
            float(segment.get("duration", 0))
            for segment in plan["segments"]
            if isinstance(segment, dict) and float(segment.get("duration", 0)) > 0
        ]
        if durations:
            metrics["average_clip_duration_s"] = round(
                sum(durations) / len(durations), 3
            )
    total = plan.get("duration_seconds")
    if total is not None:
        try:
            metrics["total_duration_s"] = round(float(total), 3)
        except (TypeError, ValueError):
            pass
    # Explicit declarations from config/project_context.json take precedence
    # over plan-derived estimates; both are factual sources, never guesses.
    declared_metrics = meta.get("available_metrics")
    if isinstance(declared_metrics, dict):
        for key, value in declared_metrics.items():
            try:
                metrics[str(key)] = round(float(value), 3)
            except (TypeError, ValueError):
                continue
    return {
        "schema_version": 1,
        "project_id": _project_id(project_dir),
        "project": project_dir.name,
        "version": meta.get("version") or "unreleased",
        "video_type": meta.get("video_type"),
        "client": meta.get("client"),
        "style_profile": meta.get("style_profile"),
        "platform": meta.get("platform"),
        "duration_target_s": duration_target,
        "available_metrics": metrics,
        "project_input_signature": project_input_signature(project_dir),
        "edit_plan_signature": edit_plan_signature(project_dir),
        "_edit_plan": plan,
    }


def _suggestion_text(rule: dict[str, Any], match: dict[str, Any]) -> str:
    expression = match.get("expression") or {}
    metric = expression.get("metric")
    observed = match.get("observed_value")
    compliance = match.get("compliance")
    if metric:
        operator = expression.get("operator")
        value = expression.get("value")
        if compliance is True:
            return (
                f"当前已满足规则 {metric} {operator} {value}"
                f"（当前值 {observed}），无需调整。"
            )
        if compliance is False:
            return (
                f"建议调整 {metric} 以满足 {operator} {value}"
                f"（当前值 {observed}）。"
            )
        return f"规则 {metric} 缺少指标或值无效，仅提示，不执行。"
    constraint = expression.get("constraint")
    if constraint:
        return f"建议遵守约束：{constraint}（布尔约束，仅提示，不自动执行）。"
    return "规则表达式不受支持，仅提示，不执行。"


def _source_cases(rule: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for item in rule.get("evidence_snapshot", []) or []:
        if not isinstance(item, dict):
            continue
        cases.append(
            {
                "kind": item.get("kind"),
                "ref": item.get("source_ref"),
                "evidence_id": item.get("evidence_id"),
                "action_id": item.get("action_id"),
                "project_id": item.get("project_id"),
                "project": item.get("project"),
                "version": item.get("run_id"),
                "source_file": item.get("source_file"),
            }
        )
    return cases


def _match_positions(
    plan: dict[str, Any],
    expression: dict[str, Any],
    observed_value: Any,
) -> list[dict[str, Any]]:
    metric = str(expression.get("metric") or "")
    positions: list[dict[str, Any]] = []
    segments = plan.get("segments") if isinstance(plan, dict) else []
    if metric in {"shot_duration_s", "segment.duration", "average_clip_duration_s"}:
        for index, segment in enumerate(segments or []):
            if not isinstance(segment, dict) or segment.get("duration") is None:
                continue
            positions.append(
                {
                    "kind": "segment",
                    "index": index,
                    "segment_id": segment.get("id"),
                    "field": "duration",
                    "timeline_start": segment.get("timeline_start"),
                    "timeline_end": segment.get("timeline_end"),
                    "current_value": segment.get("duration"),
                }
            )
    else:
        positions.append(
            {
                "kind": "whole_plan",
                "field": metric or "constraint",
                "current_value": observed_value,
            }
        )
    return positions


def _evidence_summary(rule: dict[str, Any]) -> dict[str, Any]:
    cases = _source_cases(rule)
    return {
        "evidence_count": len(cases),
        "evidence_ids": list(rule.get("evidence_ids") or []),
        "independent_project_ids": sorted(
            {
                str(item.get("project_id"))
                for item in cases
                if str(item.get("project_id") or "")
            }
        ),
        "cases": cases,
    }


def _suggestion_entry(
    match: dict[str, Any],
    rule: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    approval = rule.get("approval") or {}
    expression = deepcopy(match.get("expression") or {})
    rule_hash = str((rule.get("content_hash") or {}).get("sha256") or "")
    binding = {
        "project_id": context.get("project_id"),
        "project_input_signature": (context.get("project_input_signature") or {}).get(
            "digest_sha256"
        ),
        "edit_plan_signature": (context.get("edit_plan_signature") or {}).get(
            "digest_sha256"
        ),
        "rule_id": rule.get("rule_id"),
        "rule_revision": rule.get("revision"),
        "rule_content_hash": rule_hash,
        "suggestion_generation_version": SUGGESTION_GENERATION_VERSION,
    }
    suggestion_id = "sugg-" + _sha256_json(binding)[:24]
    positions = _match_positions(
        context.get("_edit_plan") or {}, expression, match.get("observed_value")
    )
    entry = {
        "schema_version": SUGGESTION_SCHEMA_VERSION,
        "suggestion_id": suggestion_id,
        "binding": binding,
        "rule_id": match.get("rule_id"),
        "rule_revision": rule.get("revision"),
        "rule_content_hash": rule_hash,
        "project_id": context.get("project_id"),
        "project": context.get("project"),
        "version": context.get("version"),
        "status": match.get("status"),
        "execution_status": match.get("execution_status"),
        "matched_rule": {
            "rule_id": rule.get("rule_id"),
            "revision": rule.get("revision"),
            "version": rule.get("version"),
            "content_hash": rule_hash,
            "expression": expression,
        },
        "scope": deepcopy(rule.get("scope") or {}),
        "match_positions": positions,
        "expression": expression,
        "observed_value": match.get("observed_value"),
        "current_value": (
            [item.get("current_value") for item in positions]
            if len(positions) > 1
            else positions[0].get("current_value") if positions else None
        ),
        "suggested_value": rule.get("value"),
        "compliance": match.get("compliance"),
        "suggestion": _suggestion_text(rule, match),
        "reason": str(approval.get("reason") or rule.get("description") or ""),
        "source_cases": _source_cases(rule),
        "evidence_summary": _evidence_summary(rule),
        "source_rule_provenance": {
            **deepcopy(rule.get("provenance") or {}),
            "approval_reviewer": approval.get("reviewer"),
            "approval_reason": approval.get("reason"),
        },
        "confidence": rule.get("confidence_at_approval"),
        "scope_evaluation": match.get("scope_evaluation"),
        "explanation": match.get("explanation"),
    }
    entry["suggestion_hash"] = {
        "algorithm": SUGGESTION_HASH_ALGORITHM,
        "sha256": suggestion_content_hash(entry),
    }
    return entry


def generate_memory_suggestions(
    project_dir: Path,
    knowledge_root: Path,
) -> dict[str, Any]:
    """Generate a deterministic read-only suggestion report. Writes nothing."""
    project_dir = Path(project_dir).expanduser().resolve()
    knowledge_root = Path(knowledge_root).expanduser().resolve()
    context = build_project_context(project_dir)
    rules, invalid = load_rules(knowledge_root)
    report = match_rules(context, rules, invalid)
    rules_by_id = {rule["rule_id"]: rule for rule in rules}
    suggestions = [
        _suggestion_entry(
            match,
            rules_by_id[match["rule_id"]],
            context,
        )
        for match in report["matches"]
        if match["match_status"] == "matched"
        and match["rule_id"] in rules_by_id
    ]
    summary = {
        "rules_scanned": report["summary"]["rules_scanned"],
        "suggestions": len(suggestions),
        "matched": report["summary"]["matched"],
        "not_matched": report["summary"]["not_matched"],
        "unknown": report["summary"]["unknown"],
        "conflicted": report["summary"]["conflicted"],
        "invalid": report["summary"]["invalid"],
    }
    return {
        "schema_version": SUGGESTION_SCHEMA_VERSION,
        "suggestion_generation_version": SUGGESTION_GENERATION_VERSION,
        "project_id": context.get("project_id"),
        "project": context.get("project"),
        "version": context.get("version"),
        "mode": "suggestion",
        "dry_run": True,
        "context": {
            "video_type": context.get("video_type"),
            "client": context.get("client"),
            "style_profile": context.get("style_profile"),
            "platform": context.get("platform"),
            "duration_target_s": context.get("duration_target_s"),
            "available_metrics": context.get("available_metrics"),
            "project_input_signature": context.get("project_input_signature"),
            "edit_plan_signature": context.get("edit_plan_signature"),
        },
        "summary": summary,
        "suggestions": suggestions,
        "warnings": report["warnings"],
    }


def validate_suggestion_snapshot(
    project_dir: Path,
    knowledge_root: Path,
    suggestion: dict[str, Any],
) -> list[str]:
    """Return stale/tamper errors for a previously generated suggestion."""
    errors: list[str] = []
    suggestion_hash = suggestion.get("suggestion_hash")
    if not isinstance(suggestion_hash, dict):
        errors.append("suggestion_hash is required")
    elif suggestion_hash.get("algorithm") != SUGGESTION_HASH_ALGORITHM:
        errors.append("suggestion_hash algorithm is invalid")
    elif suggestion_hash.get("sha256") != suggestion_content_hash(suggestion):
        errors.append("suggestion content hash is invalid")
    try:
        current = generate_memory_suggestions(project_dir, knowledge_root)
    except Exception as exc:
        errors.append(f"current suggestion context unavailable: {exc}")
        return errors
    current_map = {
        str(item.get("suggestion_id")): item
        for item in current.get("suggestions") or []
        if isinstance(item, dict)
    }
    current_item = current_map.get(str(suggestion.get("suggestion_id") or ""))
    if current_item is None:
        errors.append("suggestion is stale for current project/plan/rule lifecycle")
        return errors
    if current_item.get("suggestion_hash") != suggestion.get("suggestion_hash"):
        errors.append("suggestion snapshot does not match the current suggestion")
    return errors


def write_suggestion_report(suggestion: dict[str, Any], path: Path) -> dict[str, Any]:
    """Write a deterministic suggestion report. Idempotent; no timestamps."""
    target = Path(path).expanduser().resolve()
    _atomic_write_json(target, suggestion)
    return {"ok": True, "path": str(target)}
