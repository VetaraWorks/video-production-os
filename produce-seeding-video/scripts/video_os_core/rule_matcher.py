"""Deterministic Rule scope and expression matching for Video OS.

The standalone report remains an L0 dry-run and never modifies a project. The
pure scope evaluator is also shared by the audited Planner Memory advisory layer.
"""

from __future__ import annotations

import json
from typing import Any

from .memory_reader import OPERATOR_WHITELIST, SCOPE_FIELDS


REPORT_SCHEMA_VERSION = 1


def _compare(operator: str, left: float, right: float) -> bool:
    if operator == "<=":
        return left <= right
    if operator == "<":
        return left < right
    if operator == ">=":
        return left >= right
    if operator == ">":
        return left > right
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    raise ValueError(f"unsupported operator: {operator}")


def evaluate_scope(
    rule: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, dict[str, Any], list[str]]:
    """Return (status, evaluation, missing_fields). Status in
    matched | not_matched | unknown."""
    rule_scope = rule.get("scope") or {}
    evaluation: dict[str, Any] = {}
    missing: list[str] = []
    for key in SCOPE_FIELDS:
        rule_value = rule_scope.get(key)
        if rule_value is None or str(rule_value).strip() == "":
            continue  # not constrained
        context_value = context.get(key)
        if context_value is None or str(context_value).strip() in ("", "unknown"):
            missing.append(key)
            evaluation[key] = {"rule": str(rule_value), "context": None, "result": "unknown"}
            continue
        if str(context_value) == str(rule_value):
            evaluation[key] = {
                "rule": str(rule_value),
                "context": str(context_value),
                "result": "matched",
            }
        else:
            evaluation[key] = {
                "rule": str(rule_value),
                "context": str(context_value),
                "result": "not_matched",
            }
    if missing:
        return "unknown", evaluation, missing
    if any(item.get("result") == "not_matched" for item in evaluation.values()):
        return "not_matched", evaluation, []
    return "matched", evaluation, []


def evaluate_expression(
    rule: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate a rule expression deterministically. Never uses eval."""
    expression = rule.get("expression") or {}
    metrics = context.get("available_metrics") or {}
    metric = expression.get("metric")
    if metric:
        operator = expression.get("operator")
        if operator not in OPERATOR_WHITELIST:
            return {
                "expression_status": "unsupported",
                "reason": f"operator not in whitelist: {operator}",
                "observed_value": None,
                "compliance": None,
            }
        observed = metrics.get(metric)
        if observed is None:
            return {
                "expression_status": "missing_metric",
                "reason": f"context has no metric: {metric}",
                "observed_value": None,
                "compliance": None,
            }
        try:
            observed_value = float(observed)
            threshold = float(expression["value"])
        except (TypeError, ValueError, KeyError):
            return {
                "expression_status": "invalid_value",
                "reason": "non-numeric metric or threshold",
                "observed_value": observed,
                "compliance": None,
            }
        compliance = _compare(str(operator), observed_value, threshold)
        return {
            "expression_status": "evaluated",
            "observed_value": observed_value,
            "compliance": compliance,
        }
    constraint = expression.get("constraint")
    if constraint:
        return {
            "expression_status": "constraint",
            "constraint": str(constraint),
            "observed_value": None,
            "compliance": None,
            "reason": "boolean constraint displayed only; never executed",
        }
    return {
        "expression_status": "unsupported",
        "reason": "expression has neither metric nor constraint",
        "observed_value": None,
        "compliance": None,
    }


def _execution_status(rule: dict[str, Any], match_status: str) -> str:
    if match_status != "matched":
        return "not_applicable"
    status = rule.get("status")
    if status == "inactive":
        return "would_match_but_inactive"
    if status == "active":
        return "would_apply_in_future"
    return "not_applicable"


def build_explanation(
    rule: dict[str, Any],
    context: dict[str, Any],
    scope_status: str,
    scope_eval: dict[str, Any],
    expression_result: dict[str, Any],
    execution_status: str,
) -> str:
    parts: list[str] = []
    rule_scope = rule.get("scope") or {}
    video_type = rule_scope.get("video_type")
    if scope_status == "matched" and video_type:
        parts.append(f"该项目属于{video_type}，scope 匹配。")
    elif scope_status == "not_matched":
        parts.append("scope 不匹配，规则不适用。")
    elif scope_status == "unknown":
        missing = [
            key for key, item in scope_eval.items() if item.get("result") == "unknown"
        ]
        parts.append(f"scope 缺失字段（{'、'.join(missing) or 'unknown'}），无法判定。")
    metric = (rule.get("expression") or {}).get("metric")
    if metric and expression_result.get("observed_value") is not None:
        observed = expression_result["observed_value"]
        operator = (rule.get("expression") or {}).get("operator")
        value = (rule.get("expression") or {}).get("value")
        compliance = expression_result.get("compliance")
        if compliance is True:
            parts.append(
                f"当前指标 {metric}={observed}，满足规则 {operator} {value}。"
            )
        elif compliance is False:
            parts.append(
                f"当前指标 {metric}={observed}，不满足规则 {operator} {value}。"
            )
        else:
            parts.append(f"当前指标 {metric}={observed}，无法判定是否符合规则。")
    elif metric and expression_result.get("expression_status") == "missing_metric":
        parts.append(f"上下文缺少指标 {metric}，无法判定。")
    if execution_status == "would_match_but_inactive":
        parts.append("规则尚未激活，不会改变剪辑。")
    elif execution_status == "would_apply_in_future":
        parts.append("规则处于只读预演阶段，不会执行。")
    if (rule.get("expression") or {}).get("constraint"):
        parts.append("布尔约束仅展示，绝不执行。")
    return "".join(parts)


def _rule_entry(
    rule: dict[str, Any],
    context: dict[str, Any],
    scope_status: str,
    scope_eval: dict[str, Any],
    missing: list[str],
) -> dict[str, Any]:
    expression_result = evaluate_expression(rule, context)
    execution_status = _execution_status(rule, scope_status)
    explanation = build_explanation(
        rule,
        context,
        scope_status,
        scope_eval,
        expression_result,
        execution_status,
    )
    approval = rule.get("approval") or {}
    return {
        "rule_id": rule.get("rule_id"),
        "status": rule.get("status"),
        "match_status": scope_status,
        "execution_status": execution_status,
        "expression": rule.get("expression"),
        "expression_status": expression_result.get("expression_status"),
        "observed_value": expression_result.get("observed_value"),
        "compliance": expression_result.get("compliance"),
        "scope_evaluation": scope_eval,
        "missing_scope_fields": missing,
        "source_candidate_id": rule.get("source_candidate_id"),
        "confidence_at_approval": rule.get("confidence_at_approval"),
        "evidence_snapshot": rule.get("evidence_snapshot", []),
        "approval": approval,
        "explanation": explanation,
    }


def _expression_key(rule: dict[str, Any]) -> tuple[str, str]:
    expression = rule.get("expression") or {}
    if expression.get("metric"):
        return ("metric", str(expression["metric"]))
    if expression.get("constraint"):
        return ("constraint", str(expression["constraint"]))
    return ("unsupported", "")


def _same_expression(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return json.dumps(
        left.get("expression"), sort_keys=True, ensure_ascii=False
    ) == json.dumps(right.get("expression"), sort_keys=True, ensure_ascii=False)


def _detect_conflicts(
    entries: list[dict[str, Any]],
    rules_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """Scope-matched entries with same metric/constraint but different
    expressions are conflicted. Returns rule_id -> conflicting rule ids."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["match_status"] != "matched":
            continue
        key = _expression_key(rules_by_id[entry["rule_id"]])
        groups.setdefault(key, []).append(entry)
    conflicts: dict[str, list[str]] = {}
    for key, group in groups.items():
        if len(group) < 2:
            continue
        base = rules_by_id[group[0]["rule_id"]]
        for entry in group[1:]:
            rule = rules_by_id[entry["rule_id"]]
            if _same_expression(base, rule):
                continue
            conflicts.setdefault(group[0]["rule_id"], []).append(entry["rule_id"])
            conflicts.setdefault(entry["rule_id"], []).append(group[0]["rule_id"])
    return conflicts


def match_rules(
    project_context: dict[str, Any],
    rules: list[dict[str, Any]],
    invalid_files: list[dict[str, Any]] | None = None,
    mode: str = "dry_run",
) -> dict[str, Any]:
    """Deterministic L0 preview. Returns a report; writes nothing."""
    invalid_files = invalid_files or []
    warnings: list[str] = []
    active_rules: list[dict[str, Any]] = []
    for rule in rules:
        if (rule.get("evidence_status") or "valid") != "valid":
            warnings.append(
                f"rule {rule.get('rule_id')} excluded: evidence source missing (stale)"
            )
            continue
        active_rules.append(rule)

    entries: list[dict[str, Any]] = []
    rules_by_id: dict[str, dict[str, Any]] = {}
    for rule in active_rules:
        rules_by_id[rule["rule_id"]] = rule
        scope_status, scope_eval, missing = evaluate_scope(rule, project_context)
        entries.append(
            _rule_entry(
                rule,
                project_context,
                scope_status,
                scope_eval,
                missing,
            )
        )

    conflict_map = _detect_conflicts(entries, rules_by_id)
    for entry in entries:
        if entry["rule_id"] in conflict_map:
            entry["match_status"] = "conflicted"
            entry["conflicts_with"] = conflict_map[entry["rule_id"]]
            entry["execution_status"] = "not_applicable"

    counts = {
        "rules_scanned": len(rules) + len(invalid_files),
        "matched": sum(1 for e in entries if e["match_status"] == "matched"),
        "not_matched": sum(1 for e in entries if e["match_status"] == "not_matched"),
        "unknown": sum(1 for e in entries if e["match_status"] == "unknown"),
        "conflicted": sum(1 for e in entries if e["match_status"] == "conflicted"),
        "invalid": len(invalid_files),
    }
    for item in invalid_files:
        warnings.append(
            f"rule file {item.get('file')} invalid: "
            + "; ".join(item.get("errors", []))
        )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "project": project_context.get("project"),
        "version": project_context.get("version"),
        "mode": mode,
        "dry_run": True,
        "summary": counts,
        "matches": entries,
        "warnings": warnings,
    }


def write_match_report(report: dict[str, Any], path: Any) -> dict[str, Any]:
    """Write a deterministic match report. Idempotent; no timestamps."""
    from pathlib import Path

    from .knowledge import _atomic_write_json

    target = Path(path).expanduser().resolve()
    _atomic_write_json(target, report)
    return {"ok": True, "path": str(target)}
