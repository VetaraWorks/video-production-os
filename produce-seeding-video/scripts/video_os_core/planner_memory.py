"""Auditable Planner Memory advisory layer for Video OS v7.4-D.

The deterministic edit planner remains the source of the Base Plan. This module
may propose or apply narrowly scoped, explainable changes, but every failure in
the Memory layer falls back to the unchanged Base Plan with a durable warning.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from .knowledge_root import KnowledgeRootError, require_knowledge_root
from .memory_reader import load_rules
from .rule_matcher import evaluate_scope


MEMORY_MODES = {"off", "shadow", "advisory"}
CONTEXT_GENERATION_VERSION = "video-os-planner-memory-context-v1"
CONTEXT_HASH_ALGORITHM = "video-os-planner-memory-context-v1"
APPLICATION_HASH_ALGORITHM = "video-os-planner-memory-application-v1"
PLAN_HASH_ALGORITHM = "video-os-edit-plan-v1"
SOURCE_HASH_ALGORITHM = "video-os-planner-memory-source-v1"
SUPPORTED_METRIC = "shot_duration_s"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def plan_signature(plan: dict[str, Any]) -> str:
    return _digest({"algorithm": PLAN_HASH_ALGORITHM, "plan": plan})


def _seal(payload: dict[str, Any], field: str, algorithm: str) -> dict[str, Any]:
    sealed = deepcopy(payload)
    sealed.pop(field, None)
    sealed[field] = {"algorithm": algorithm, "sha256": _digest(sealed)}
    return sealed


def _seal_errors(payload: dict[str, Any], field: str, algorithm: str) -> list[str]:
    seal = payload.get(field)
    if not isinstance(seal, dict):
        return [f"{field} is required"]
    if seal.get("algorithm") != algorithm:
        return [f"{field} algorithm is invalid"]
    material = deepcopy(payload)
    material.pop(field, None)
    if seal.get("sha256") != _digest(material):
        return [f"{field} content hash is invalid"]
    return []


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return payload


def _project_id(project_dir: Path) -> str:
    state_path = project_dir / "project_state.json"
    if state_path.is_file():
        try:
            state = _read_json(state_path)
        except (OSError, ValueError, json.JSONDecodeError):
            state = {}
        project_id = str(state.get("project_id") or "").strip()
        if project_id:
            return project_id
    return "project-" + hashlib.sha256(project_dir.name.encode("utf-8")).hexdigest()[:16]


def _perception_binding(perception: dict[str, Any]) -> dict[str, Any]:
    input_signature = perception.get("input_signature") or {}
    return {
        "input_signature_digest": input_signature.get("digest_sha256"),
        "artifact_sha256": _digest(perception),
    }


def _project_input_signature(
    project_dir: Path,
    config: dict[str, Any],
    perception: dict[str, Any],
) -> str:
    from video_pipeline.perception import source_signature

    script_path = project_dir / "script" / "script.txt"
    script = script_path.read_bytes()
    current_sources: list[dict[str, Any]] = []
    for item in (perception.get("input_signature") or {}).get("sources", []) or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        absolute = (project_dir / source).resolve()
        try:
            absolute.relative_to(project_dir)
        except ValueError:
            current_signature: dict[str, Any] = {"state": "outside_project"}
        else:
            current_signature = (
                source_signature(absolute)
                if absolute.is_file()
                else {"state": "missing"}
            )
        current_sources.append(
            {
                "source": source,
                "signature": current_signature,
            }
        )
    current_sources.sort(key=lambda item: item["source"])
    material = {
        "project_id": _project_id(project_dir),
        "script_sha256": hashlib.sha256(script).hexdigest(),
        "config_sha256": _digest(config),
        "perception_input_signature": (perception.get("input_signature") or {}).get(
            "digest_sha256"
        ),
        "current_source_signatures": current_sources,
    }
    return _digest(material)


def _rule_ref(rule: dict[str, Any]) -> dict[str, Any]:
    activation = rule.get("activation") or {}
    return {
        "rule_id": rule.get("rule_id"),
        "revision": rule.get("revision"),
        "content_hash": (rule.get("content_hash") or {}).get("sha256"),
        "review_id": rule.get("review_id"),
        "activation": {
            "reviewer": activation.get("reviewer"),
            "reason": activation.get("reason"),
            "review_id": activation.get("review_id"),
            "activated_at": activation.get("activated_at"),
            "application_mode": activation.get("application_mode"),
            "rule_revision": activation.get("rule_revision"),
            "rule_content_hash": activation.get("rule_content_hash"),
        },
        "scope": deepcopy(rule.get("scope") or {}),
        "expression": deepcopy(rule.get("expression") or {}),
        "confidence": rule.get("confidence_at_approval"),
        "source_evidence": [
            {
                "evidence_id": item.get("evidence_id"),
                "action_id": item.get("action_id"),
                "gate_material_digest": item.get("gate_material_digest"),
            }
            for item in rule.get("evidence_snapshot", [])
            if isinstance(item, dict)
        ],
    }


def _memory_source_snapshot(
    knowledge_root: Path | str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        root = require_knowledge_root(knowledge_root)
        rules, invalid = load_rules(root, statuses=("active",))
    except (KnowledgeRootError, OSError, ValueError) as exc:
        material = {
            "state": "unavailable",
            "rules": [],
            "invalid": [],
            "warning": str(exc),
        }
        return {
            **material,
            "signature": _digest({"algorithm": SOURCE_HASH_ALGORITHM, **material}),
        }, []

    eligible: list[dict[str, Any]] = []
    invalid_entries = deepcopy(invalid)
    for rule in rules:
        activation = rule.get("activation") or {}
        if (
            rule.get("status") != "active"
            or rule.get("active") is not True
            or activation.get("application_mode") != "advisory"
            or (rule.get("evidence_status") or "valid") != "valid"
        ):
            invalid_entries.append(
                {
                    "rule_id": rule.get("rule_id"),
                    "errors": ["rule is not eligible for advisory application"],
                }
            )
            continue
        eligible.append(rule)
    eligible.sort(key=lambda item: (str(item.get("rule_id")), int(item.get("revision") or 0)))
    invalid_entries.sort(key=lambda item: (str(item.get("file") or ""), str(item.get("rule_id") or "")))
    material = {
        "state": "invalid" if invalid_entries else "ready",
        "rules": [_rule_ref(rule) for rule in eligible],
        "invalid": invalid_entries,
        "warning": (
            "Planner Memory contains invalid or stale rule records"
            if invalid_entries
            else None
        ),
    }
    return {
        **material,
        "signature": _digest({"algorithm": SOURCE_HASH_ALGORITHM, **material}),
    }, eligible


def _planner_context(project_dir: Path, config: dict[str, Any], base_plan: dict[str, Any]) -> dict[str, Any]:
    configured = config.get("video_os", {}).get("planner_memory", {})
    scope = configured.get("context", {}) if isinstance(configured, dict) else {}
    state: dict[str, Any] = {}
    state_path = project_dir / "project_state.json"
    if state_path.is_file():
        try:
            state = _read_json(state_path)
        except (OSError, ValueError, json.JSONDecodeError):
            state = {}
    durations = [
        float(segment.get("duration", 0.0))
        for segment in base_plan.get("segments", [])
        if isinstance(segment, dict)
    ]
    return {
        "project": project_dir.name,
        "version": state.get("version"),
        "video_type": scope.get("video_type", config.get("video_type")),
        "client": scope.get("client", config.get("client")),
        "style_profile": scope.get("style_profile", config.get("style_profile")),
        "platform": scope.get("platform", config.get("platform")),
        "duration_target_s": base_plan.get("duration_seconds"),
        "available_metrics": {
            SUPPORTED_METRIC: max(durations, default=0.0),
        },
    }


def _numeric_interval(rule: dict[str, Any]) -> tuple[float, bool, float, bool] | None:
    expression = rule.get("expression") or {}
    try:
        value = float(expression["value"])
    except (KeyError, TypeError, ValueError):
        return None
    operator = str(expression.get("operator") or "")
    if operator == "<=":
        return float("-inf"), False, value, True
    if operator == "<":
        return float("-inf"), False, value, False
    if operator == ">=":
        return value, True, float("inf"), False
    if operator == ">":
        return value, False, float("inf"), False
    if operator == "==":
        return value, True, value, True
    return None


def _intervals_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_interval = _numeric_interval(left)
    right_interval = _numeric_interval(right)
    if left_interval is None or right_interval is None:
        return False
    low = max(left_interval[0], right_interval[0])
    high = min(left_interval[2], right_interval[2])
    if low < high:
        return False
    if low > high:
        return True
    left_allows = (
        (low > left_interval[0] or left_interval[1])
        and (low < left_interval[2] or left_interval[3])
    )
    right_allows = (
        (low > right_interval[0] or right_interval[1])
        and (low < right_interval[2] or right_interval[3])
    )
    return not (left_allows and right_allows)


def _conflict_map(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    conflicts: dict[str, set[str]] = {}
    for index, left in enumerate(entries):
        if left.get("match_status") != "matched":
            continue
        left_expression = left.get("expression") or {}
        for right in entries[index + 1 :]:
            if right.get("match_status") != "matched":
                continue
            right_expression = right.get("expression") or {}
            if left_expression.get("metric") != right_expression.get("metric"):
                continue
            if not _intervals_conflict(left, right):
                continue
            left_id = str(left["rule_id"])
            right_id = str(right["rule_id"])
            conflicts.setdefault(left_id, set()).add(right_id)
            conflicts.setdefault(right_id, set()).add(left_id)
    return {rule_id: sorted(values) for rule_id, values in sorted(conflicts.items())}


def _perception_candidates(perception: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source in perception.get("sources", []):
        if not isinstance(source, dict):
            continue
        for segment in source.get("segments", []):
            if not isinstance(segment, dict):
                continue
            quality = segment.get("quality") or {}
            try:
                safe_duration = float(segment.get("safe_end", 0.0)) - float(
                    segment.get("safe_start", 0.0)
                )
            except (TypeError, ValueError):
                continue
            if quality.get("usable") is not True or safe_duration <= 0.0:
                continue
            tags: set[str] = set()
            for field in ("semantic_tags", "subjects", "objects", "actions"):
                tags.update(str(item) for item in segment.get(field, []) or [])
            candidates.append(
                {
                    "source": source.get("source"),
                    "source_duration": source.get("duration"),
                    "segment": segment,
                    "tags": tags,
                }
            )
    candidates.sort(
        key=lambda item: (
            str(item.get("source") or ""),
            str((item.get("segment") or {}).get("id") or ""),
        )
    )
    return candidates


def _selection(candidate: dict[str, Any]) -> dict[str, Any]:
    segment = candidate["segment"]
    return {
        "mode": "perception",
        "perception_segment_id": str(segment["id"]),
        "summary": str(segment.get("summary", "")),
        "semantic_tags": list(segment.get("semantic_tags", [])),
        "subjects": list(segment.get("subjects", [])),
        "objects": list(segment.get("objects", [])),
        "actions": list(segment.get("actions", [])),
        "safe_start": round(float(segment["safe_start"]), 3),
        "safe_end": round(float(segment["safe_end"]), 3),
        "quality_score": float((segment.get("quality") or {}).get("score", 0.0)),
        "confidence": float(segment.get("confidence", 0.0)),
        "visual_fingerprint": str(segment.get("visual_fingerprint") or segment["id"]),
        "duplicate_reuse": False,
    }


def _split_plan_for_rule(
    plan: dict[str, Any],
    rule: dict[str, Any],
    perception: dict[str, Any],
    context_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    expression = rule.get("expression") or {}
    operator = str(expression.get("operator") or "")
    if expression.get("metric") != SUPPORTED_METRIC or operator not in {"<=", "<"}:
        return None, [], "only shot_duration_s <= advisory rules are safely supported"
    try:
        threshold = float(expression["value"])
    except (KeyError, TypeError, ValueError):
        return None, [], "rule threshold is not numeric"
    if operator == "<":
        threshold = round(threshold - 0.001, 3)
    if threshold < 0.5:
        return None, [], "rule threshold is below the safe 0.5 second slot minimum"
    long_segments = [
        segment
        for segment in plan.get("segments", [])
        if isinstance(segment, dict) and float(segment.get("duration", 0.0)) > threshold + 0.001
    ]
    if not long_segments:
        return deepcopy(plan), [], "base plan already satisfies this advisory"

    candidates = _perception_candidates(perception)
    used_fingerprints = {
        str((segment.get("selection") or {}).get("visual_fingerprint") or "")
        for segment in plan.get("segments", [])
        if isinstance(segment, dict)
    }
    working = deepcopy(plan)
    changes: list[dict[str, Any]] = []
    rule_ref = {
        "rule_id": rule.get("rule_id"),
        "revision": rule.get("revision"),
    }
    evidence = _rule_ref(rule)["source_evidence"]
    for original in long_segments:
        duration = float(original["duration"])
        slot_count = max(2, math.ceil(duration / threshold))
        slot_duration = round(duration / slot_count, 3)
        durations = [slot_duration] * slot_count
        durations[-1] = round(duration - sum(durations[:-1]), 3)
        original_tags = set(str(item) for item in original.get("matched_tags", []) or [])
        extras: list[dict[str, Any]] = []
        for chunk_duration in durations[1:]:
            selected = None
            for candidate in candidates:
                perception_segment = candidate["segment"]
                fingerprint = str(
                    perception_segment.get("visual_fingerprint") or perception_segment.get("id")
                )
                available = float(perception_segment["safe_end"]) - float(
                    perception_segment["safe_start"]
                )
                if fingerprint in used_fingerprints or available + 0.02 < chunk_duration:
                    continue
                if original_tags and not (original_tags & candidate["tags"]):
                    continue
                selected = candidate
                break
            if selected is None:
                return (
                    None,
                    [],
                    f"slot {original.get('id')} has insufficient unique safe Perception footage",
                )
            extras.append(selected)
            used_fingerprints.add(
                str(selected["segment"].get("visual_fingerprint") or selected["segment"]["id"])
            )

        replacement: list[dict[str, Any]] = []
        cursor = float(original["timeline_start"])
        first = deepcopy(original)
        first["duration"] = durations[0]
        first["timeline_end"] = round(cursor + durations[0], 3)
        selection = first.get("selection") or {}
        if selection.get("mode") == "perception":
            safe_end = float(selection.get("safe_end", 0.0))
            if float(first.get("source_start", 0.0)) + durations[0] > safe_end + 0.02:
                first["source_start"] = float(selection.get("safe_start", 0.0))
        replacement.append(first)
        cursor = float(first["timeline_end"])
        for index, (chunk_duration, candidate) in enumerate(zip(durations[1:], extras), start=2):
            selected = _selection(candidate)
            replacement.append(
                {
                    **{key: deepcopy(value) for key, value in original.items() if key not in {
                        "id", "timeline_start", "timeline_end", "duration", "source",
                        "source_start", "source_duration", "has_audio", "loop", "selection"
                    }},
                    "id": f"{original['id']}-memory-{index:02d}",
                    "timeline_start": round(cursor, 3),
                    "timeline_end": round(cursor + chunk_duration, 3),
                    "duration": chunk_duration,
                    "source": str(candidate["source"]),
                    "source_start": selected["safe_start"],
                    "source_duration": round(float(candidate["source_duration"]), 3),
                    "has_audio": False,
                    "loop": False,
                    "selection": selected,
                }
            )
            cursor = round(cursor + chunk_duration, 3)
        target_index = next(
            index
            for index, item in enumerate(working["segments"])
            if item.get("id") == original.get("id")
        )
        before = deepcopy(working["segments"][target_index])
        working["segments"][target_index : target_index + 1] = replacement
        changes.append(
            {
                "op": "replace_segment_with_slots",
                "rule": rule_ref,
                "context_id": context_id,
                "affected_slot": original.get("id"),
                "affected_field": "segments",
                "before": before,
                "after": replacement,
                "reason": f"apply advisory shot duration {operator} {expression.get('value')}",
                "confidence": rule.get("confidence_at_approval"),
                "source_evidence": evidence,
            }
        )

    selected_ids = [
        str(selection["perception_segment_id"])
        for segment in working.get("segments", [])
        if isinstance(segment, dict)
        and isinstance((selection := segment.get("selection")), dict)
        and selection.get("mode") == "perception"
        and selection.get("perception_segment_id")
    ]
    perception_binding = working.setdefault("perception", {})
    before_ids = deepcopy(perception_binding.get("selected_segment_ids", []))
    if before_ids != selected_ids:
        perception_binding["selected_segment_ids"] = selected_ids
        changes.append(
            {
                "op": "replace",
                "rule": rule_ref,
                "context_id": context_id,
                "affected_slot": "perception",
                "affected_field": "selected_segment_ids",
                "before": before_ids,
                "after": selected_ids,
                "reason": "bind final plan to the Perception segments introduced by this advisory",
                "confidence": rule.get("confidence_at_approval"),
                "source_evidence": evidence,
            }
        )
    return working, changes, "advisory safely split long slots using unique Perception ranges"


def _context_entry(rule: dict[str, Any], planner_context: dict[str, Any]) -> dict[str, Any]:
    scope_status, scope_evaluation, missing = evaluate_scope(rule, planner_context)
    return {
        **_rule_ref(rule),
        "match_status": scope_status,
        "scope_evaluation": scope_evaluation,
        "missing_scope_fields": missing,
    }


def build_planner_memory(
    project_dir: Path,
    config: dict[str, Any],
    base_plan: dict[str, Any],
    perception: dict[str, Any],
    *,
    knowledge_root: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Return final plan, context, application, optional shadow report."""
    project_dir = Path(project_dir).resolve()
    configured = config.get("video_os", {}).get("planner_memory", {})
    mode = str(configured.get("mode", "shadow") if isinstance(configured, dict) else "shadow")
    if mode not in MEMORY_MODES:
        raise ValueError(f"planner_memory.mode must be one of {sorted(MEMORY_MODES)}")

    bindings = {
        "project_id": _project_id(project_dir),
        "project_input_signature": _project_input_signature(project_dir, config, perception),
        "perception_signature": _perception_binding(perception),
        "base_plan_signature": plan_signature(base_plan),
    }
    planner_context = _planner_context(project_dir, config, base_plan)
    source_snapshot: dict[str, Any]
    rules: list[dict[str, Any]]
    if mode == "off":
        source_snapshot = {
            "state": "off",
            "rules": [],
            "invalid": [],
            "warning": None,
            "signature": _digest({"algorithm": SOURCE_HASH_ALGORITHM, "state": "off"}),
        }
        rules = []
    else:
        source_snapshot, rules = _memory_source_snapshot(knowledge_root)

    entries = [_context_entry(rule, planner_context) for rule in rules]
    context_status = "off" if mode == "off" else "ready"
    fallback_reason = None
    warnings: list[str] = []
    if mode != "off" and source_snapshot["state"] != "ready":
        context_status = "fallback"
        fallback_reason = (
            "knowledge_root_unavailable"
            if source_snapshot["state"] == "unavailable"
            else "rule_integrity_invalid"
        )
        warnings.append(str(source_snapshot.get("warning") or fallback_reason))
    context = _seal(
        {
            "schema_version": 1,
            "generation_version": CONTEXT_GENERATION_VERSION,
            "mode": mode,
            "status": context_status,
            "bindings": bindings,
            "planner_context": planner_context,
            "memory_source": source_snapshot,
            "rules": entries if context_status == "ready" else [],
            "warning": warnings,
            "fallback_reason": fallback_reason,
        },
        "context_signature",
        CONTEXT_HASH_ALGORITHM,
    )
    context_id = str(context["context_signature"]["sha256"])
    conflicts = _conflict_map(entries) if context_status == "ready" else {}
    decisions: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    proposed_changes: list[dict[str, Any]] = []
    working = deepcopy(base_plan)

    if context_status == "ready":
        for entry, rule in zip(entries, rules):
            rule_id = str(entry["rule_id"])
            decision = {
                "rule_id": entry["rule_id"],
                "revision": entry["revision"],
                "content_hash": entry["content_hash"],
                "context_id": context_id,
                "confidence": entry.get("confidence"),
                "source_evidence": entry.get("source_evidence", []),
            }
            if entry["match_status"] != "matched":
                decision.update(
                    result="not_applicable",
                    reason="rule scope does not match the current project",
                )
            elif rule_id in conflicts:
                decision.update(
                    result="conflict",
                    reason="deterministic rule conflict: " + ", ".join(conflicts[rule_id]),
                    conflicts_with=conflicts[rule_id],
                )
            else:
                proposed, rule_changes, reason = _split_plan_for_rule(
                    working, rule, perception, context_id
                )
                if proposed is None:
                    decision.update(result="unsafe", reason=reason)
                elif not rule_changes:
                    result = "would_skip" if mode == "shadow" else "skipped"
                    decision.update(result=result, reason=reason)
                elif mode == "shadow":
                    decision.update(result="would_apply", reason=reason)
                    proposed_changes.extend(rule_changes)
                else:
                    decision.update(result="applied", reason=reason)
                    working = proposed
                    changes.extend(rule_changes)
            decisions.append(decision)

    application_status = {
        "off": "off",
        "fallback": "fallback",
    }.get(context_status, "shadow" if mode == "shadow" else "applied" if changes else "no_change")
    application = _seal(
        {
            "schema_version": 1,
            "mode": mode,
            "status": application_status,
            "context_signature": context_id,
            "base_plan_signature": bindings["base_plan_signature"],
            "decisions": decisions,
            "changes": changes,
            "proposed_changes": proposed_changes,
            "warnings": warnings + [
                f"rule conflict excluded: {rule_id}"
                for rule_id in sorted(conflicts)
            ],
            "fallback_reason": fallback_reason,
        },
        "application_signature",
        APPLICATION_HASH_ALGORITHM,
    )
    memory_applied = bool(changes and mode == "advisory")
    final_plan = deepcopy(working if memory_applied else base_plan)
    final_plan["memory"] = {
        "status": application_status,
        "warning": application.get("warnings", []),
        "fallback_reason": fallback_reason,
        "mode": mode,
        "base_plan_signature": bindings["base_plan_signature"],
        "memory_context_signature": context_id,
        "memory_application_signature": application["application_signature"]["sha256"],
        "memory_applied": memory_applied,
        "applied_rules": [
            {"rule_id": item["rule_id"], "revision": item["revision"]}
            for item in decisions
            if item.get("result") == "applied"
        ],
        "skipped_rules": [
            {"rule_id": item["rule_id"], "revision": item["revision"], "result": item["result"]}
            for item in decisions
            if item.get("result") != "applied"
        ],
    }
    shadow_report = None
    if mode == "shadow":
        shadow_report = {
            "schema_version": 1,
            "mode": "shadow",
            "base_plan_signature": bindings["base_plan_signature"],
            "final_plan_semantically_equal": _without_memory(final_plan) == base_plan,
            "context_signature": context_id,
            "application_signature": application["application_signature"]["sha256"],
            "decisions": deepcopy(decisions),
            "proposed_changes": deepcopy(proposed_changes),
            "warnings": deepcopy(application.get("warnings", [])),
        }
    return final_plan, context, application, shadow_report


def build_planner_memory_fallback(
    project_dir: Path,
    config: dict[str, Any],
    base_plan: dict[str, Any],
    perception: dict[str, Any],
    *,
    knowledge_root: Path | str | None = None,
    reason: str,
    warnings: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Build sealed no-use artifacts after the advisory layer fails validation."""
    project_dir = Path(project_dir).resolve()
    configured = config.get("video_os", {}).get("planner_memory", {})
    mode = str(configured.get("mode", "shadow") if isinstance(configured, dict) else "shadow")
    if mode not in MEMORY_MODES:
        raise ValueError(f"planner_memory.mode must be one of {sorted(MEMORY_MODES)}")
    if mode == "off":
        source_snapshot = {
            "state": "off",
            "rules": [],
            "invalid": [],
            "warning": None,
            "signature": _digest({"algorithm": SOURCE_HASH_ALGORITHM, "state": "off"}),
        }
    else:
        source_snapshot, _rules = _memory_source_snapshot(knowledge_root)
    bindings = {
        "project_id": _project_id(project_dir),
        "project_input_signature": _project_input_signature(project_dir, config, perception),
        "perception_signature": _perception_binding(perception),
        "base_plan_signature": plan_signature(base_plan),
    }
    warning_list = [str(item) for item in warnings if str(item).strip()]
    context = _seal(
        {
            "schema_version": 1,
            "generation_version": CONTEXT_GENERATION_VERSION,
            "mode": mode,
            "status": "fallback",
            "bindings": bindings,
            "planner_context": _planner_context(project_dir, config, base_plan),
            "memory_source": source_snapshot,
            "rules": [],
            "warning": warning_list,
            "fallback_reason": reason,
        },
        "context_signature",
        CONTEXT_HASH_ALGORITHM,
    )
    context_id = str(context["context_signature"]["sha256"])
    application = _seal(
        {
            "schema_version": 1,
            "mode": mode,
            "status": "fallback",
            "context_signature": context_id,
            "base_plan_signature": bindings["base_plan_signature"],
            "decisions": [],
            "changes": [],
            "proposed_changes": [],
            "warnings": warning_list,
            "fallback_reason": reason,
        },
        "application_signature",
        APPLICATION_HASH_ALGORITHM,
    )
    final_plan = deepcopy(base_plan)
    final_plan["memory"] = {
        "status": "fallback",
        "warning": warning_list,
        "fallback_reason": reason,
        "mode": mode,
        "base_plan_signature": bindings["base_plan_signature"],
        "memory_context_signature": context_id,
        "memory_application_signature": application["application_signature"]["sha256"],
        "memory_applied": False,
        "applied_rules": [],
        "skipped_rules": [],
    }
    shadow_report = None
    if mode == "shadow":
        shadow_report = {
            "schema_version": 1,
            "mode": "shadow",
            "base_plan_signature": bindings["base_plan_signature"],
            "final_plan_semantically_equal": True,
            "context_signature": context_id,
            "application_signature": application["application_signature"]["sha256"],
            "decisions": [],
            "proposed_changes": [],
            "warnings": warning_list,
        }
    return final_plan, context, application, shadow_report


def _without_memory(plan: dict[str, Any]) -> dict[str, Any]:
    clean = deepcopy(plan)
    clean.pop("memory", None)
    return clean


def record_post_plan_repair(
    project_dir: Path,
    plan_before: dict[str, Any],
    plan_after: dict[str, Any],
    repair_diff: dict[str, Any],
) -> dict[str, Any]:
    """Bind a RENDER-only Repair overlay without regenerating Planner Memory."""
    final = deepcopy(plan_after)
    memory = final.get("memory")
    if not isinstance(memory, dict):
        raise ValueError("repaired edit plan has no Planner Memory provenance")
    history = deepcopy((plan_before.get("memory") or {}).get("post_plan_repairs") or [])
    before_executable = _without_memory(plan_before)
    after_executable = _without_memory(plan_after)
    history.append(
        {
            "schema_version": 1,
            "repair_diff_signature": _digest(repair_diff),
            "before_plan_signature": plan_signature(before_executable),
            "after_plan_signature": plan_signature(after_executable),
            "repair_diff": deepcopy(repair_diff),
        }
    )
    memory["post_plan_repairs"] = history
    final["memory"] = memory
    path = Path(project_dir).resolve() / "output" / "edit_plan.json"
    path.write_text(
        json.dumps(final, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return final


def _replay_changes(base_plan: dict[str, Any], changes: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    working = deepcopy(base_plan)
    errors: list[str] = []
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            errors.append(f"memory change {index} must be an object")
            continue
        operation = change.get("op")
        field = change.get("affected_field")
        if operation == "replace_segment_with_slots" and field == "segments":
            slot = change.get("affected_slot")
            matches = [
                position
                for position, segment in enumerate(working.get("segments", []))
                if isinstance(segment, dict) and segment.get("id") == slot
            ]
            if len(matches) != 1:
                errors.append(f"memory change {index} cannot locate affected slot {slot}")
                continue
            position = matches[0]
            if working["segments"][position] != change.get("before"):
                errors.append(f"memory change {index} before value does not match Base Plan")
                continue
            after = change.get("after")
            if not isinstance(after, list) or not after:
                errors.append(f"memory change {index} after value must be non-empty slots")
                continue
            working["segments"][position : position + 1] = deepcopy(after)
        elif operation == "replace" and field == "selected_segment_ids" and change.get("affected_slot") == "perception":
            binding = working.get("perception")
            if not isinstance(binding, dict):
                errors.append(f"memory change {index} has no Perception binding")
                continue
            if binding.get("selected_segment_ids") != change.get("before"):
                errors.append(f"memory change {index} Perception before value is invalid")
                continue
            binding["selected_segment_ids"] = deepcopy(change.get("after"))
        else:
            errors.append(f"memory change {index} modifies an unrelated or unsupported field")
    return working, errors


def _validate_memory_changes(
    changes: list[dict[str, Any]],
    perception: dict[str, Any],
) -> list[str]:
    """Reject resealed diffs that exceed the narrow advisory edit surface."""
    errors: list[str] = []
    candidates = {
        str(item["segment"].get("id")): item
        for item in _perception_candidates(perception)
        if str(item["segment"].get("id") or "")
    }
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            continue
        if not str(change.get("reason") or "").strip():
            errors.append(f"memory change {index} has no reason")
        if not isinstance(change.get("source_evidence"), list) or not change.get(
            "source_evidence"
        ):
            errors.append(f"memory change {index} has no source evidence")
        rule = change.get("rule")
        if not isinstance(rule, dict) or not rule.get("rule_id") or not rule.get("revision"):
            errors.append(f"memory change {index} has no rule identity")
        if not str(change.get("context_id") or ""):
            errors.append(f"memory change {index} has no context identity")
        if change.get("op") != "replace_segment_with_slots":
            continue
        before = change.get("before")
        after = change.get("after")
        if not isinstance(before, dict) or not isinstance(after, list) or not after:
            continue
        if str(before.get("id") or "") != str(change.get("affected_slot") or ""):
            errors.append(f"memory change {index} affected slot does not match Base Plan")
        try:
            before_start = float(before["timeline_start"])
            before_end = float(before["timeline_end"])
            before_duration = float(before["duration"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"memory change {index} Base Plan slot timing is invalid")
            continue
        if not isinstance(after[0], dict) or str(after[0].get("id") or "") != str(
            before.get("id") or ""
        ):
            errors.append(f"memory change {index} replaced the original slot identity")
        stable_fields = set(before) - {
            "id",
            "timeline_start",
            "timeline_end",
            "duration",
            "source",
            "source_start",
            "source_duration",
            "has_audio",
            "loop",
            "selection",
        }
        cursor = before_start
        total = 0.0
        seen_fingerprints: set[str] = set()
        for slot_index, slot in enumerate(after):
            if not isinstance(slot, dict):
                errors.append(f"memory change {index} slot {slot_index} is invalid")
                continue
            for field in stable_fields:
                if slot.get(field) != before.get(field):
                    errors.append(
                        f"memory change {index} slot {slot_index} modified unrelated field {field}"
                    )
            try:
                start = float(slot["timeline_start"])
                end = float(slot["timeline_end"])
                duration = float(slot["duration"])
                source_start = float(slot["source_start"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"memory change {index} slot {slot_index} timing is invalid")
                continue
            if abs(start - cursor) > 0.002 or abs((end - start) - duration) > 0.002:
                errors.append(f"memory change {index} slot {slot_index} breaks the timeline")
            cursor = end
            total += duration
            selection = slot.get("selection")
            if not isinstance(selection, dict) or selection.get("mode") != "perception":
                errors.append(
                    f"memory change {index} slot {slot_index} lacks Perception provenance"
                )
                continue
            segment_id = str(selection.get("perception_segment_id") or "")
            candidate = candidates.get(segment_id)
            if candidate is None:
                errors.append(
                    f"memory change {index} slot {slot_index} references unknown Perception segment"
                )
                continue
            observed = candidate["segment"]
            if str(slot.get("source") or "") != str(candidate.get("source") or ""):
                errors.append(f"memory change {index} slot {slot_index} source is not Perception-bound")
            expected_fingerprint = str(
                observed.get("visual_fingerprint") or observed.get("id") or ""
            )
            if str(selection.get("visual_fingerprint") or "") != expected_fingerprint:
                errors.append(
                    f"memory change {index} slot {slot_index} Perception fingerprint is invalid"
                )
            if expected_fingerprint in seen_fingerprints:
                errors.append(
                    f"memory change {index} reuses duplicate Perception footage"
                )
            seen_fingerprints.add(expected_fingerprint)
            try:
                safe_start = float(observed.get("safe_start", 0.0))
                safe_end = float(observed.get("safe_end", 0.0))
                selected_safe_start = float(selection.get("safe_start", -1.0))
                selected_safe_end = float(selection.get("safe_end", -1.0))
            except (TypeError, ValueError):
                errors.append(
                    f"memory change {index} slot {slot_index} safe range is invalid"
                )
                continue
            if (
                abs(selected_safe_start - safe_start) > 0.002
                or abs(selected_safe_end - safe_end) > 0.002
                or source_start < safe_start - 0.002
                or source_start + duration > safe_end + 0.002
            ):
                errors.append(
                    f"memory change {index} slot {slot_index} exceeds its safe Perception range"
                )
        if abs(total - before_duration) > 0.002 or abs(cursor - before_end) > 0.002:
            errors.append(f"memory change {index} changed the Base Plan slot duration")
    return errors


def _replay_repair_diff(
    plan: dict[str, Any],
    repair_diff: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    working = deepcopy(plan)
    errors: list[str] = []
    for index, change in enumerate(repair_diff.get("changes", []) or []):
        if not isinstance(change, dict) or change.get("action_id") == "system":
            continue
        action_type = str(change.get("type") or "")
        if action_type not in {"replace_clip", "adjust_trim"}:
            errors.append(f"post-PLAN Repair change {index} cannot modify edit_plan safely")
            continue
        segment_id = str(change.get("segment_id") or "")
        segment = next(
            (
                item
                for item in working.get("segments", [])
                if isinstance(item, dict) and str(item.get("id") or "") == segment_id
            ),
            None,
        )
        if segment is None:
            errors.append(f"post-PLAN Repair change {index} cannot locate segment {segment_id}")
            continue
        before = change.get("before")
        after = change.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            errors.append(f"post-PLAN Repair change {index} needs structured before/after")
            continue
        if any(segment.get(field) != value for field, value in before.items()):
            errors.append(f"post-PLAN Repair change {index} before values are stale")
            continue
        allowed = {
            "source",
            "source_start",
            "source_duration",
            "duration",
            "has_audio",
            "loop",
        }
        unrelated = sorted(set(after) - allowed)
        if unrelated:
            errors.append(
                f"post-PLAN Repair change {index} modifies unsupported fields: "
                + ", ".join(unrelated)
            )
            continue
        segment.update(deepcopy(after))
        segment["selection"] = {
            "mode": "repair",
            "repair_plan_id": str(change.get("action_id") or ""),
            "reason": str(change.get("reason") or ""),
        }
    return working, errors


def validate_planner_memory_artifacts(
    project_dir: Path,
    config: dict[str, Any],
    *,
    knowledge_root: Path | str | None = None,
    verify_memory_source: bool = True,
) -> list[str]:
    """Validate all four PLAN layers and current signature bindings."""
    project_dir = Path(project_dir).resolve()
    output = project_dir / "output"
    try:
        base_plan = _read_json(output / "edit_plan.base.json")
        context = _read_json(output / "memory_context.json")
        application = _read_json(output / "memory_application.json")
        final_plan = _read_json(output / "edit_plan.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"Planner Memory artifact invalid: {exc}"]
    perception_path = project_dir / "perception" / "perception.json"
    if perception_path.is_file():
        try:
            perception = _read_json(perception_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return [f"Planner Memory Perception binding invalid: {exc}"]
    else:
        perception_config = config.get("perception", {})
        perception_required = bool(
            perception_config.get("enabled", True)
            and perception_config.get("required", True)
        )
        if perception_required:
            return ["Planner Memory requires the current perception.json"]
        perception = {}
    errors = _seal_errors(context, "context_signature", CONTEXT_HASH_ALGORITHM)
    errors.extend(_seal_errors(application, "application_signature", APPLICATION_HASH_ALGORITHM))
    mode = str(context.get("mode") or "")
    if mode not in MEMORY_MODES:
        errors.append("memory context mode is invalid")
    if application.get("mode") != mode:
        errors.append("memory application mode does not match context")
    bindings = context.get("bindings") or {}
    expected = {
        "project_id": _project_id(project_dir),
        "project_input_signature": _project_input_signature(project_dir, config, perception),
        "perception_signature": _perception_binding(perception),
        "base_plan_signature": plan_signature(base_plan),
    }
    for field, value in expected.items():
        if bindings.get(field) != value:
            errors.append(f"memory context {field} is stale or mismatched")
    context_signature = (context.get("context_signature") or {}).get("sha256")
    application_signature = (application.get("application_signature") or {}).get("sha256")
    if application.get("context_signature") != context_signature:
        errors.append("memory application context signature is stale")
    if application.get("base_plan_signature") != expected["base_plan_signature"]:
        errors.append("memory application Base Plan signature is stale")

    if mode != "off" and verify_memory_source:
        current_source, _rules = _memory_source_snapshot(knowledge_root)
        recorded_source = context.get("memory_source") or {}
        if recorded_source.get("signature") != current_source.get("signature"):
            errors.append("memory context source state is stale")
        if (
            context.get("status") == "ready"
            and current_source.get("state") == "ready"
            and recorded_source.get("state") == "ready"
        ):
            current_refs = current_source.get("rules") or []
            context_refs = context.get("rules") or []
            normalized_context_refs = [
                {field: item.get(field) for field in current_ref}
                for item, current_ref in zip(context_refs, current_refs)
                if isinstance(item, dict) and isinstance(current_ref, dict)
            ]
            if len(context_refs) != len(current_refs) or normalized_context_refs != current_refs:
                errors.append("memory context rules do not match the current activated Rule source")

    context_rules = context.get("rules") or []
    decisions = application.get("decisions") or []
    if not isinstance(context_rules, list):
        errors.append("memory context rules must be an array")
        context_rules = []
    if not isinstance(decisions, list):
        errors.append("memory application decisions must be an array")
        decisions = []
    context_keys = [
        (str(item.get("rule_id") or ""), int(item.get("revision") or 0))
        for item in context_rules
        if isinstance(item, dict)
    ]
    decision_keys = [
        (str(item.get("rule_id") or ""), int(item.get("revision") or 0))
        for item in decisions
        if isinstance(item, dict)
    ]
    if len(context_keys) != len(set(context_keys)):
        errors.append("memory context contains duplicate Rule identities")
    if len(decision_keys) != len(set(decision_keys)):
        errors.append("memory application contains duplicate Rule decisions")
    if sorted(context_keys) != sorted(decision_keys):
        errors.append("memory application decisions do not cover the current Context Rules")
    context_by_key = {
        (str(item.get("rule_id") or ""), int(item.get("revision") or 0)): item
        for item in context_rules
        if isinstance(item, dict)
    }
    allowed_results = {
        "shadow": {"would_apply", "would_skip", "conflict", "not_applicable", "unsafe"},
        "advisory": {"applied", "skipped", "conflict", "not_applicable", "unsafe"},
        "off": set(),
    }.get(mode, set())
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            errors.append(f"memory decision {index} must be an object")
            continue
        key = (str(decision.get("rule_id") or ""), int(decision.get("revision") or 0))
        context_rule = context_by_key.get(key)
        if context_rule is None:
            continue
        if decision.get("content_hash") != context_rule.get("content_hash"):
            errors.append(f"memory decision {index} Rule content hash is stale")
        if decision.get("context_id") != context_signature:
            errors.append(f"memory decision {index} Context identity is stale")
        if decision.get("result") not in allowed_results:
            errors.append(f"memory decision {index} result is invalid for {mode} mode")
        if not str(decision.get("reason") or "").strip():
            errors.append(f"memory decision {index} has no reason")
    if context.get("status") in {"off", "fallback"} and decisions:
        errors.append("disabled or failed Memory Context must not make Rule decisions")

    memory = final_plan.get("memory")
    if not isinstance(memory, dict):
        errors.append("final edit plan has no Planner Memory provenance")
        memory = {}
    expected_memory = {
        "mode": mode,
        "base_plan_signature": expected["base_plan_signature"],
        "memory_context_signature": context_signature,
        "memory_application_signature": application_signature,
    }
    for field, value in expected_memory.items():
        if memory.get(field) != value:
            errors.append(f"final edit plan memory.{field} is stale or mismatched")
    changes = application.get("changes")
    if not isinstance(changes, list):
        errors.append("memory application changes must be an array")
        changes = []
    proposed_changes = application.get("proposed_changes")
    if not isinstance(proposed_changes, list):
        errors.append("memory application proposed_changes must be an array")
        proposed_changes = []
    if mode != "advisory" and changes:
        errors.append(f"{mode} mode must not contain applied changes")
    if mode != "shadow" and proposed_changes:
        errors.append(f"{mode} mode must not contain shadow proposed changes")
    errors.extend(_validate_memory_changes(changes, perception))
    errors.extend(_validate_memory_changes(proposed_changes, perception))
    applied_keys = {
        (str(item.get("rule_id") or ""), int(item.get("revision") or 0))
        for item in decisions
        if isinstance(item, dict) and item.get("result") == "applied"
    }
    changed_keys = {
        (
            str((item.get("rule") or {}).get("rule_id") or ""),
            int((item.get("rule") or {}).get("revision") or 0),
        )
        for item in changes
        if isinstance(item, dict)
    }
    if applied_keys != changed_keys:
        errors.append("applied Rule decisions do not match the structured Memory diff")
    for index, change in enumerate(changes + proposed_changes):
        if not isinstance(change, dict):
            continue
        rule = change.get("rule") or {}
        key = (str(rule.get("rule_id") or ""), int(rule.get("revision") or 0))
        if key not in context_by_key:
            errors.append(f"memory change {index} is not bound to a current Context Rule")
        if change.get("context_id") != context_signature:
            errors.append(f"memory change {index} Context identity is stale")
    replayed, replay_errors = _replay_changes(base_plan, changes)
    errors.extend(replay_errors)
    executable_final = _without_memory(final_plan)
    applied_decisions = [
        item
        for item in application.get("decisions", [])
        if isinstance(item, dict) and item.get("result") == "applied"
    ]
    claimed_applied = memory.get("memory_applied") is True
    if claimed_applied != bool(changes and applied_decisions and mode == "advisory"):
        errors.append("memory applied claim does not match decisions and structured diff")
    expected_applied_rules = [
        {"rule_id": item["rule_id"], "revision": item["revision"]}
        for item in decisions
        if isinstance(item, dict) and item.get("result") == "applied"
    ]
    expected_skipped_rules = [
        {
            "rule_id": item["rule_id"],
            "revision": item["revision"],
            "result": item["result"],
        }
        for item in decisions
        if isinstance(item, dict) and item.get("result") != "applied"
    ]
    if memory.get("applied_rules") != expected_applied_rules:
        errors.append("final edit plan applied Rule metadata is invalid")
    if memory.get("skipped_rules") != expected_skipped_rules:
        errors.append("final edit plan skipped Rule metadata is invalid")
    expected_executable = replayed
    repair_history = memory.get("post_plan_repairs") or []
    if not isinstance(repair_history, list):
        errors.append("memory post_plan_repairs must be an array")
        repair_history = []
    for index, overlay in enumerate(repair_history):
        if not isinstance(overlay, dict):
            errors.append(f"post-PLAN Repair overlay {index} must be an object")
            continue
        repair_diff = overlay.get("repair_diff")
        if not isinstance(repair_diff, dict):
            errors.append(f"post-PLAN Repair overlay {index} has no repair_diff")
            continue
        if overlay.get("repair_diff_signature") != _digest(repair_diff):
            errors.append(f"post-PLAN Repair overlay {index} diff signature is invalid")
        if overlay.get("before_plan_signature") != plan_signature(expected_executable):
            errors.append(f"post-PLAN Repair overlay {index} is stale")
        expected_executable, repair_errors = _replay_repair_diff(
            expected_executable, repair_diff
        )
        errors.extend(repair_errors)
        if overlay.get("after_plan_signature") != plan_signature(expected_executable):
            errors.append(f"post-PLAN Repair overlay {index} result signature is invalid")

    if mode in {"off", "shadow"}:
        if not repair_history and executable_final != base_plan:
            errors.append(f"{mode} mode modified the executable Final Plan")
    if executable_final != expected_executable:
        errors.append("Final Plan contains unexplained or unrelated Memory modifications")
    if changes and not repair_history and executable_final == base_plan:
        errors.append("memory application claims changes but Final Plan has no corresponding diff")
    if not changes and not repair_history and executable_final != base_plan:
        errors.append("Final Plan changed without a structured Memory diff")
    if mode == "shadow":
        try:
            shadow = _read_json(output / "memory_shadow_report.json")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"memory shadow report invalid: {exc}")
        else:
            if shadow.get("final_plan_semantically_equal") is not True:
                errors.append("memory shadow report does not prove Base Plan equivalence")
            if shadow.get("application_signature") != application_signature:
                errors.append("memory shadow report application signature is stale")
    return errors
