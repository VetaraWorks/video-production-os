"""Video OS Knowledge Layer (Phase 4.1): schemas, skeleton, and migration.

Responsibilities:
- Create and maintain the knowledge/ tree (edits, repair_log,
  rule_candidates, editing_rules, reviews, governance_history, good_cases, bad_cases, style_profile,
  client_preferences) with a manifest.
- Validate feedback v2 records.
- Migrate Phase 1 feedback v1 records into v2 with rule_class classification.

No model training, no automatic rule promotion: the automatic layer only ever
writes rule_candidates/ (pending). editing_rules/ is human-confirmed only.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .knowledge_root import KNOWLEDGE_DATA_DIRECTORIES, require_knowledge_root


KNOWLEDGE_SCHEMA_VERSION = 1
FEEDBACK_SCHEMA_VERSION = 2
KNOWLEDGE_DIRS = KNOWLEDGE_DATA_DIRECTORIES

FEEDBACK_CATEGORIES = {
    "rhythm",
    "hook",
    "structure",
    "shot_count",
    "semantic_split",
    "subtitle_style",
    "sound_effect",
    "cover",
    "pacing",
    "repetition",
    "audio",
    "compliance",
    "repair",
    "human_review",
    "shot_selection",
    "subtitle",
    "composition",
    "style_preference",
    "other",
}

RULE_CLASSES = {"hard", "editing", "style", "audit"}
TARGET_KINDS = {"segment", "time_range", "whole_video"}
FEEDBACK_STATUSES = {"pending", "referenced", "archived"}
# Feedback v2 remains backward-compatible with historical tiers.  New human
# edits are human_verified; production_verified is reserved for the unified
# Production Evidence Gate in production_evidence.py.
EVIDENCE_TIERS = {
    "observed",
    "human_verified",
    "production_verified",
    "demo",
    "migrated_unverified",
}

# v1 feedback categories -> rule_class. audit = process/audit event, not a
# creative rule candidate.
RULE_CLASS_MAP = {
    "structure": "editing",
    "shot_count": "editing",
    "semantic_split": "editing",
    "subtitle_style": "style",
    "rhythm": "editing",
    "sound_effect": "editing",
    "repair": "audit",
    "human_review": "audit",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", str(value)).strip("-")
    return cleaned or "record"


def manifest_path(knowledge_root: Path) -> Path:
    return Path(knowledge_root).resolve() / "manifest.json"


def load_manifest(knowledge_root: Path) -> dict[str, Any]:
    path = manifest_path(knowledge_root)
    if not path.is_file():
        return default_manifest()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default_manifest()
    if not isinstance(payload, dict):
        return default_manifest()
    return payload


def default_manifest() -> dict[str, Any]:
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "counts": {name: 0 for name in KNOWLEDGE_DIRS},
        "note": "Video OS 经验库清单。counts 为该目录下 .json 条目数。",
    }


def save_manifest(knowledge_root: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _now_iso()
    _atomic_write_json(manifest_path(knowledge_root), manifest)


def refresh_counts(knowledge_root: Path) -> dict[str, Any]:
    knowledge_root = Path(knowledge_root).resolve()
    manifest = load_manifest(knowledge_root)
    counts: dict[str, int] = {}
    for name in KNOWLEDGE_DIRS:
        directory = knowledge_root / name
        counts[name] = (
            len([path for path in directory.glob("*.json") if path.is_file()])
            if directory.is_dir()
            else 0
        )
    manifest["counts"] = counts
    save_manifest(knowledge_root, manifest)
    return manifest


def init_knowledge(knowledge_root: Path, *, force: bool = False) -> dict[str, Any]:
    """Create the knowledge/ tree and manifest. Never overwrites existing entries."""
    knowledge_root = Path(knowledge_root).expanduser().resolve()
    knowledge_root.mkdir(parents=True, exist_ok=True)
    created_dirs: list[str] = []
    for name in KNOWLEDGE_DIRS:
        directory = knowledge_root / name
        if not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)
            created_dirs.append(name)

    readme = knowledge_root / "README.md"
    if not readme.exists():
        readme.write_text(_readme_text(), encoding="utf-8")
        created_dirs.append("README.md")

    if force or not manifest_path(knowledge_root).is_file():
        save_manifest(knowledge_root, default_manifest())
    else:
        refresh_counts(knowledge_root)
    return {
        "ok": True,
        "knowledge_root": str(knowledge_root),
        "created": created_dirs,
        "manifest": str(manifest_path(knowledge_root)),
    }


def _readme_text() -> str:
    return (
        "# Video OS Knowledge（经验库）\n\n"
        "目标：把人工修改与自动修复沉淀为可追溯、可确认、未来可被 Planner 消费的经验资产。\n\n"
        "## 规则分类（重要）\n\n"
        "1. **hard（硬规则）**：工程约束，机器可直接判断（字幕安全区、时长、分辨率、黑帧）。\n"
        "2. **editing（剪辑规则）**：可量化、未来可影响 Planner（产品首次出现时间、平均镜头长度、B-roll 比例）。\n"
        "3. **style（审美偏好）**：最难，暂时只作参考，不自动执行（高级感、国风、情绪、留白）。\n"
        "4. **audit（审计）**：过程事件（自动修复、人工复核），不生成规则候选。\n\n"
        "## 目录用途\n\n"
        "- `edits/`：人工修改记录（feedback schema v2）。\n"
        "- `repair_log/`：自动修复审计（来自 repair_diff/repair_plan）。\n"
        "- `rule_candidates/`：待人工确认规则（由 production_verified evidence 聚合，status=candidate）。\n"
        "- `editing_rules/`：已批准但不自动执行的规则（人工 approve 后生成，默认 inactive）。\n"
        "- `reviews/`：人工审核记录（approve/reject/defer/deprecate/revoke，只追加、不可覆盖）。\n"
        "- `governance_history/`：L0 suggestion 的人工 accept/reject/defer 治理历史（只追加）。\n"
        "- `good_cases/`、`bad_cases/`：优秀/失败案例。\n"
        "- `style_profile/`、`client_preferences/`：风格画像与客户偏好（暂缓）。\n\n"
        "## 生命周期\n\n"
        "production_verified evidence → 聚合 → rule_candidate（candidate）\n"
        "→ 人工审核 → editing_rule（inactive）→ L0 suggestion → 人工 decision。\n\n"
        "自动阶段绝不自动转正规则；`editing_rules/` 只接受人工确认。"
    )


# ---------------------------------------------------------------- validation


def validate_feedback_v2(payload: dict[str, Any]) -> list[str]:
    """Return schema violations for a feedback v2 record."""
    errors: list[str] = []
    if int(payload.get("schema_version", 0)) != FEEDBACK_SCHEMA_VERSION:
        errors.append(f"schema_version must be {FEEDBACK_SCHEMA_VERSION}")
    for field in ("feedback_id", "project", "from_version", "to_version", "collected_at"):
        if not str(payload.get(field) or "").strip():
            errors.append(f"{field} is required")
    if str(payload.get("collector") or "") not in ("manual", "video_os_feedback"):
        errors.append("collector must be manual|video_os_feedback")
    evidence_tier = payload.get("evidence_tier")
    if evidence_tier is not None and evidence_tier not in EVIDENCE_TIERS:
        errors.append(
            "evidence_tier must be observed|human_verified|production_verified|demo|migrated_unverified"
        )
    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        errors.append("changes must be a non-empty list")
        return errors
    seen_ids: set[str] = set()
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            errors.append(f"changes[{index}] must be an object")
            continue
        change_id = str(change.get("change_id") or "")
        if not change_id or change_id in seen_ids:
            errors.append(f"changes[{index}] needs a unique change_id")
        seen_ids.add(change_id)
        category = str(change.get("category") or "")
        if category not in FEEDBACK_CATEGORIES:
            errors.append(f"changes[{index}] invalid category: {category}")
        rule_class = str(change.get("rule_class") or "")
        if rule_class not in RULE_CLASSES:
            errors.append(f"changes[{index}] invalid rule_class: {rule_class}")
        target = change.get("target")
        if not isinstance(target, dict) or str(target.get("kind") or "") not in TARGET_KINDS:
            errors.append(f"changes[{index}] target.kind must be segment|time_range|whole_video")
        for field in ("before", "after"):
            value = change.get(field)
            if not isinstance(value, dict):
                errors.append(f"changes[{index}].{field} must be an object")
        if not str(change.get("reason") or "").strip():
            errors.append(f"changes[{index}].reason is required")
        status = str(change.get("status") or "")
        if status not in FEEDBACK_STATUSES:
            errors.append(f"changes[{index}].status must be pending|referenced|archived")
    return errors


def is_valid_feedback_v2(payload: dict[str, Any]) -> bool:
    return not validate_feedback_v2(payload)


# ---------------------------------------------------------------- migration


def migrate_feedback_v1_to_v2(
    v1: dict[str, Any],
    snapshot_ref: str,
    *,
    collector: str = "manual",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Convert a Phase 1 feedback v1 record into feedback v2 plus an optional
    repair-log entry for audit-class changes. Never mutates the source."""
    project = str(v1.get("project") or "unknown")
    from_version = str(v1.get("from_version") or "unknown")
    to_version = str(v1.get("to_version") or "unknown")
    slug = _slug(f"{project}-{to_version}")
    feedback_id = f"fb-{datetime.now(timezone.utc):%Y%m%d}-{slug}"
    source_docs = list(v1.get("source_docs", []) or [])
    raw_changes = v1.get("changes", [])

    changes: list[dict[str, Any]] = []
    repair_actions: list[dict[str, Any]] = []
    for index, change in enumerate(raw_changes, start=1):
        if not isinstance(change, dict):
            continue
        category = str(change.get("category") or "other")
        rule_class = RULE_CLASS_MAP.get(category, "editing")
        description = str(change.get("what") or "")
        before_value = str(change.get("before") or "")
        after_value = str(change.get("after") or "")
        reason = str(change.get("reason") or "")
        change_doc = str(change.get("source_doc") or "")
        docs = list(source_docs)
        if change_doc and change_doc not in docs:
            docs.append(change_doc)
        if rule_class == "audit":
            repair_actions.append(
                {
                    "type": category,
                    "what": description,
                    "before": before_value,
                    "after": after_value,
                    "reason": reason,
                    "source_doc": change_doc,
                }
            )
        changes.append(
            {
                "change_id": f"{feedback_id}-{index:02d}",
                "category": category,
                "rule_class": rule_class,
                "target": {"kind": "whole_video"},
                "before": {"description": before_value},
                "after": {"description": after_value},
                "reason": reason,
                "rule_candidate": None,
                "rule_candidate_structured": None,
                "confidence": None,
                "status": "pending",
                "source_docs": docs,
            }
        )

    feedback_v2 = {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "evidence_tier": "migrated_unverified",
        "feedback_id": feedback_id,
        "project": project,
        "from_version": from_version,
        "to_version": to_version,
        "collector": collector,
        "collected_at": _now_iso(),
        "source_docs": source_docs,
        "snapshot_refs": [snapshot_ref],
        "changes": changes,
    }
    repair_log_entry: dict[str, Any] | None = None
    if repair_actions:
        repair_log_entry = {
            "schema_version": 1,
            "evidence_tier": "migrated_unverified",
            "project": project,
            "version": to_version,
            "source": snapshot_ref,
            "source_reports": list(
                v1.get("source_docs", []) or ["output/qa_report-v5.json"]
            ),
            "actions": repair_actions,
            "collected_at": _now_iso(),
        }
    return feedback_v2, repair_log_entry


def write_feedback_v2(
    knowledge_root: Path,
    feedback_v2: dict[str, Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Validate and write a feedback v2 record into knowledge/edits/, then
    refresh the manifest. Returns skip result if the record already exists."""
    errors = validate_feedback_v2(feedback_v2)
    if errors:
        raise ValueError("Invalid feedback v2: " + "; ".join(errors))
    if feedback_v2.get("evidence_tier") == "production_verified":
        raise ValueError(
            "feedback v2 cannot assign production_verified directly; use the "
            "Production Evidence Gate"
        )
    knowledge_root = require_knowledge_root(knowledge_root)
    target = knowledge_root / "edits" / f"{feedback_v2['feedback_id']}.json"
    if target.is_file() and not overwrite:
        return {"written": False, "reason": "exists", "path": str(target)}
    _atomic_write_json(target, feedback_v2)
    refresh_counts(knowledge_root)
    return {"written": True, "path": str(target)}


def write_repair_log_entry(
    knowledge_root: Path,
    entry: dict[str, Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    if entry.get("evidence_tier") == "production_verified":
        raise ValueError(
            "repair_log cannot assign production_verified directly; use the "
            "Production Evidence Gate"
        )
    knowledge_root = require_knowledge_root(knowledge_root)
    slug = _slug(f"{entry.get('project')}-{entry.get('version')}")
    target = knowledge_root / "repair_log" / f"{slug}.json"
    if target.is_file() and not overwrite:
        return {"written": False, "reason": "exists", "path": str(target)}
    _atomic_write_json(target, entry)
    refresh_counts(knowledge_root)
    return {"written": True, "path": str(target)}


# ---------------------------------------------------------------- draft building


def _feedback_id_for(project: str, to_version: str, changes: list[dict[str, Any]]) -> str:
    import hashlib

    digest = hashlib.sha1(
        json.dumps(changes, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:8]
    date_stamp = f"{datetime.now(timezone.utc):%Y%m%d}"
    slug = _slug(f"{project}-{to_version}")
    return f"fb-{date_stamp}-{slug}-{digest}"


def build_feedback_draft(
    *,
    project: str,
    from_version: str,
    to_version: str,
    changes: list[dict[str, Any]],
    collector: str = "manual",
    source_docs: list[str] | None = None,
    snapshot_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Build a feedback v2 record from raw changes (no writes)."""
    if not changes:
        raise ValueError("changes must be a non-empty list")
    feedback_id = _feedback_id_for(project, to_version, changes)
    normalized_changes: list[dict[str, Any]] = []
    for index, change in enumerate(changes, start=1):
        change = dict(change)
        change.setdefault("change_id", f"{feedback_id}-{index:02d}")
        if "target" not in change:
            change["target"] = {"kind": "whole_video"}
        target = change["target"]
        if isinstance(target, dict):
            if target.get("kind") == "segment" and not str(target.get("id") or "").strip():
                raise ValueError(f"changes[{index}] segment target needs id")
            if target.get("kind") == "time_range":
                if target.get("start") is None or target.get("end") is None:
                    raise ValueError(
                        f"changes[{index}] time_range target needs start and end"
                    )
        before = change.get("before")
        after = change.get("after")
        if not isinstance(before, dict):
            before = {"description": str(before or "")}
        if not isinstance(after, dict):
            after = {"description": str(after or "")}
        if not str(before.get("description") or "") and before.get("metric") is None:
            raise ValueError(f"changes[{index}].before needs description or metric")
        if not str(after.get("description") or "") and after.get("metric") is None:
            raise ValueError(f"changes[{index}].after needs description or metric")
        if not str(change.get("reason") or "").strip():
            raise ValueError(f"changes[{index}].reason is required")
        change["before"] = before
        change["after"] = after
        change.setdefault("rule_candidate", None)
        change.setdefault("rule_candidate_structured", None)
        change.setdefault("confidence", None)
        change.setdefault("status", "pending")
        change.setdefault("source_docs", list(source_docs or []))
        normalized_changes.append(change)
    return {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "evidence_tier": "human_verified",
        "feedback_id": feedback_id,
        "project": project,
        "from_version": from_version,
        "to_version": to_version,
        "collector": collector,
        "collected_at": _now_iso(),
        "source_docs": list(source_docs or []),
        "snapshot_refs": list(snapshot_refs or []),
        "changes": normalized_changes,
    }


REPAIR_ACTION_MAP: dict[str, tuple[str, str]] = {
    "replace_clip": ("shot_selection", "editing"),
    "adjust_trim": ("composition", "editing"),
    "fix_subtitle": ("subtitle", "editing"),
}


def build_feedback_draft_from_repair(
    *,
    project: str,
    from_version: str,
    to_version: str,
    repair_plan: dict[str, Any] | None,
    repair_diff: dict[str, Any] | None,
    source_docs: list[str] | None = None,
    snapshot_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Build a feedback draft from a repair execution (no writes).

    Only non-system actions become changes. The caller decides whether to save:
    a draft never lands in knowledge/edits/ automatically.
    """
    changes: list[dict[str, Any]] = []
    diff_changes = (repair_diff or {}).get("changes", []) or []
    for change in diff_changes:
        if not isinstance(change, dict):
            continue
        action_type = str(change.get("type") or "")
        if action_type not in REPAIR_ACTION_MAP:
            continue
        category, rule_class = REPAIR_ACTION_MAP[action_type]
        before = change.get("before") or {}
        after = change.get("after") or {}
        segment_id = str(change.get("segment_id") or "")
        target: dict[str, Any] = (
            {"kind": "segment", "id": segment_id} if segment_id else {"kind": "whole_video"}
        )
        source_docs_local = list(source_docs or [])
        if repair_plan and repair_plan.get("source_reports"):
            for item in repair_plan["source_reports"]:
                if item not in source_docs_local:
                    source_docs_local.append(item)
        changes.append(
            {
                "category": category,
                "rule_class": rule_class,
                "target": target,
                "before": {
                    "description": _clip_description(before)
                    or f"repair before ({action_type})",
                },
                "after": {
                    "description": _clip_description(after)
                    or f"repair after ({action_type})",
                },
                "reason": str(change.get("reason") or action_type),
                "source_docs": source_docs_local,
            }
        )
    if not changes:
        raise ValueError("repair diff contains no supported repair actions")
    return build_feedback_draft(
        project=project,
        from_version=from_version,
        to_version=to_version,
        changes=changes,
        collector="video_os_feedback",
        source_docs=source_docs,
        snapshot_refs=snapshot_refs,
    )


def _clip_description(change: dict[str, Any]) -> str:
    if not isinstance(change, dict):
        return ""
    parts: list[str] = []
    if change.get("source"):
        parts.append(str(change["source"]))
    if change.get("source_start") is not None:
        parts.append(f"start={change['source_start']}s")
    if change.get("duration") is not None:
        parts.append(f"duration={change['duration']}s")
    return " ".join(parts).strip()


def write_feedback_draft(
    project_dir: Path,
    feedback_v2: dict[str, Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write a feedback draft under <project>/feedback_drafts/ (never edits/)."""
    project_dir = Path(project_dir).expanduser().resolve()
    errors = validate_feedback_v2(feedback_v2)
    if errors:
        raise ValueError("Invalid feedback v2: " + "; ".join(errors))
    drafts_dir = project_dir / "feedback_drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    target = drafts_dir / f"{feedback_v2['feedback_id']}.draft.json"
    if target.is_file() and not overwrite:
        return {"written": False, "reason": "exists", "path": str(target)}
    _atomic_write_json(target, feedback_v2)
    return {"written": True, "path": str(target)}


def migrate_feedback_file(
    v1_path: Path,
    knowledge_root: Path,
    snapshot_ref: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    v1_path = v1_path.expanduser().resolve()
    payload = json.loads(v1_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"feedback v1 must be a JSON object: {v1_path}")
    feedback_v2, repair_log_entry = migrate_feedback_v1_to_v2(
        payload, snapshot_ref
    )
    feedback_result = write_feedback_v2(
        knowledge_root, feedback_v2, overwrite=overwrite
    )
    repair_result: dict[str, Any] | None = None
    if repair_log_entry is not None:
        repair_result = write_repair_log_entry(
            knowledge_root, repair_log_entry, overwrite=overwrite
        )
    return {
        "ok": True,
        "feedback_id": feedback_v2["feedback_id"],
        "change_count": len(feedback_v2["changes"]),
        "feedback": feedback_result,
        "repair_log": repair_result,
    }
