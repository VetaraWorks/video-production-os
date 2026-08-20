from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from video_pipeline.perception import source_signature  # noqa: E402


COPY_EXTENSIONS = {
    ".json",
    ".md",
    ".txt",
    ".ass",
    ".srt",
    ".py",
    ".mjs",
    ".ps1",
    ".yaml",
    ".yml",
    ".tpl",
    ".ini",
    ".cfg",
    ".csv",
}

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".wmv",
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in MEDIA_EXTENSIONS:
        return source_signature(path)
    return {
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def snapshot_project(
    source_dir: Path,
    projects_root: Path,
    project_name: str,
    version: str,
    *,
    max_file_bytes: int = 5 * 1024 * 1024,
    force: bool = False,
) -> dict[str, Any]:
    """Archive a source project directory into projects/<name>/snapshots/<version>.

    Copies small text/JSON artifacts and records fingerprints for media; large
    media stays in place and is referenced, never duplicated into the snapshot.
    """
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise NotADirectoryError(f"Source directory not found: {source_dir}")

    project_dir = projects_root.expanduser().resolve() / project_name
    target_dir = project_dir / "snapshots" / version
    if target_dir.exists():
        if not force:
            raise FileExistsError(f"Snapshot already exists: {target_dir}")
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, Any]] = []
    referenced: list[dict[str, Any]] = []
    fingerprints: dict[str, dict[str, Any]] = {}

    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_dir).as_posix()
        size = path.stat().st_size
        fingerprint = _fingerprint(path)
        fingerprints[rel] = fingerprint
        if path.suffix.lower() in COPY_EXTENSIONS and size <= max_file_bytes:
            destination = target_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied.append(
                {
                    "path": rel,
                    "size_bytes": size,
                    "sha256": fingerprint.get("sha256") or fingerprint.get("sample_sha256"),
                }
            )
        else:
            referenced.append(
                {
                    "path": rel,
                    "size_bytes": size,
                    "reason": (
                        "media-not-copied"
                        if path.suffix.lower() in MEDIA_EXTENSIONS
                        else "binary-or-large-not-copied"
                    ),
                }
            )

    created_at = datetime.now(timezone.utc).isoformat()
    source_manifest = {
        "schema_version": 1,
        "project": project_name,
        "version": version,
        "source_dir": str(source_dir),
        "generated_at": created_at,
        "fingerprints": fingerprints,
    }
    (target_dir / "source_manifest.json").write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "schema_version": 1,
        "project": project_name,
        "version": version,
        "source_dir": str(source_dir),
        "created_at": created_at,
        "copied_files": copied,
        "referenced_files": referenced,
        "contains_media": False,
        "note": "Snapshot stores small artifacts and fingerprints only; media stays in source_dir.",
    }
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    version_doc = _build_version_md(
        project_name,
        version,
        source_dir,
        created_at,
        len(copied),
        len(referenced),
    )
    (target_dir / "VERSION.md").write_text(version_doc, encoding="utf-8")

    return {
        "ok": True,
        "project": project_name,
        "version": version,
        "snapshot_dir": str(target_dir),
        "copied_file_count": len(copied),
        "referenced_file_count": len(referenced),
    }


def archive_repair_version(
    work_dir: Path,
    projects_root: Path,
    project_name: str,
    version: str,
    repair_plan: dict[str, Any],
    repair_diff: dict[str, Any],
    *,
    qa_summary: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Archive a repaired version: small artifacts + fingerprints + repair docs.
    Media is referenced by fingerprint, never duplicated (consistent with
    snapshot_project). The original version directory is never touched."""
    work_dir = work_dir.expanduser().resolve()
    project_dir = projects_root.expanduser().resolve() / project_name
    target_dir = project_dir / "snapshots" / version
    if target_dir.exists():
        if not force:
            raise FileExistsError(f"Version snapshot already exists: {target_dir}")
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    selected = [
        "output/edit_plan.json",
        "output/analysis.json",
        "output/qa_report.json",
        "output/subtitles.ass",
        "output/subtitles.srt",
        "config/config.json",
        "config/edit_plan.json",
        "script/script.txt",
        "speech_timeline.json",
    ]
    copied: list[dict[str, Any]] = []
    referenced: list[dict[str, Any]] = []
    fingerprints: dict[str, dict[str, Any]] = {}

    for relative in selected:
        source = work_dir / relative
        if not source.is_file():
            continue
        fingerprint = _fingerprint(source)
        fingerprints[relative] = fingerprint
        destination = target_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(
            {
                "path": relative,
                "size_bytes": source.stat().st_size,
                "sha256": fingerprint.get("sha256") or fingerprint.get("sample_sha256"),
            }
        )

    for group in ("raw_video", "material", "reference"):
        for path in sorted((work_dir / group).rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(work_dir).as_posix()
            fingerprints[rel] = _fingerprint(path)
            referenced.append(
                {
                    "path": rel,
                    "size_bytes": path.stat().st_size,
                    "reason": "media-not-copied",
                }
            )

    (target_dir / "repair_plan.json").write_text(
        json.dumps(repair_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target_dir / "repair_diff.json").write_text(
        json.dumps(repair_diff, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    created_at = datetime.now(timezone.utc).isoformat()
    (target_dir / "source_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": project_name,
                "version": version,
                "work_dir": str(work_dir),
                "generated_at": created_at,
                "fingerprints": fingerprints,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": project_name,
                "version": version,
                "work_dir": str(work_dir),
                "created_at": created_at,
                "kind": "repair",
                "copied_files": copied,
                "referenced_files": referenced,
                "contains_media": False,
                "note": "Repair version: small artifacts copied, media referenced by fingerprint.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    version_doc = _build_repair_version_md(
        project_name,
        version,
        work_dir,
        created_at,
        repair_plan,
        repair_diff,
        qa_summary,
    )
    (target_dir / "VERSION.md").write_text(version_doc, encoding="utf-8")
    return {
        "ok": True,
        "project": project_name,
        "version": version,
        "snapshot_dir": str(target_dir),
        "copied_file_count": len(copied),
        "referenced_file_count": len(referenced),
    }


def _build_repair_version_md(
    project_name: str,
    version: str,
    work_dir: Path,
    created_at: str,
    repair_plan: dict[str, Any],
    repair_diff: dict[str, Any],
    qa_summary: dict[str, Any] | None,
) -> str:
    lines = [
        f"# {project_name} {version}（Repair 版本）",
        "",
        f"- 归档时间：{created_at}",
        f"- 工作目录：{work_dir}",
        f"- 类型：repair（由 QA/Review 问题自动生成修复方案，人工确认后执行）",
        "",
        "## 修复内容",
    ]
    for change in repair_diff.get("changes", []):
        lines.append(
            f"- [{change.get('action_id')}] {change.get('type')} "
            f"segment={change.get('segment_id')} reason={change.get('reason')}"
        )
    if repair_plan.get("needs_human"):
        lines.append("")
        lines.append("## 需人工处理")
        lines.extend(f"- {item}" for item in repair_plan["needs_human"])
    if qa_summary is not None:
        lines.append("")
        lines.append(
            f"## 回归 QA：ok={qa_summary.get('ok')}, "
            f"errors={len(qa_summary.get('errors', []))}"
        )
    return "\n".join(lines) + "\n"


def _build_version_md(
    project_name: str,
    version: str,
    source_dir: Path,
    created_at: str,
    copied_count: int,
    referenced_count: int,
) -> str:
    return (
        f"# {project_name} {version} 快照\n\n"
        f"- 归档时间：{created_at}\n"
        f"- 来源目录：{source_dir}\n"
        f"- 复制小文件：{copied_count} 个（JSON/MD/TXT/ASS/SRT/PY 等）\n"
        f"- 引用未复制文件：{referenced_count} 个（大媒体/二进制，仍位于来源目录）\n\n"
        "## 为什么这一版通过\n\n"
        "（待补充：记录最终通过原因、人工修改、QA/Review 结论。）\n"
    )


def validate_snapshot(
    projects_root: Path,
    project_name: str,
    version: str,
) -> dict[str, Any]:
    snapshot_dir = (
        projects_root.expanduser().resolve() / project_name / "snapshots" / version
    )
    errors: list[str] = []
    required = ("manifest.json", "source_manifest.json", "VERSION.md")
    for name in required:
        if not (snapshot_dir / name).is_file():
            errors.append(f"Missing required file: {name}")
    payloads: dict[str, Any] = {}
    for name in ("manifest.json", "source_manifest.json"):
        path = snapshot_dir / name
        if path.is_file():
            try:
                payloads[name] = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{name} is not valid JSON: {exc}")
    if "manifest.json" in payloads:
        for item in payloads["manifest.json"].get("copied_files", []):
            if not (snapshot_dir / item["path"]).is_file():
                errors.append(f"Registered copy is missing: {item['path']}")
    return {
        "ok": not errors,
        "project": project_name,
        "version": version,
        "snapshot_dir": str(snapshot_dir),
        "errors": errors,
    }


def explain_snapshot(
    projects_root: Path,
    project_name: str,
    version: str,
) -> dict[str, Any]:
    snapshot_dir = (
        projects_root.expanduser().resolve() / project_name / "snapshots" / version
    )
    if not snapshot_dir.is_dir():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_dir}")
    manifest = json.loads(
        (snapshot_dir / "manifest.json").read_text(encoding="utf-8-sig")
    )
    version_doc = (snapshot_dir / "VERSION.md").read_text(encoding="utf-8-sig")

    feedback: dict[str, Any] | None = None
    feedback_path = snapshot_dir / "feedback.json"
    if feedback_path.is_file():
        feedback = json.loads(feedback_path.read_text(encoding="utf-8-sig"))

    copied_names = {item["path"] for item in manifest.get("copied_files", [])}
    qa_report: dict[str, Any] | None = None
    for candidate in (
        "output/qa_report-v5.json",
        "output/qa_report-v4.json",
        "output/qa_report-v3.json",
        "output/qa_report.json",
    ):
        if candidate in copied_names:
            qa_path = snapshot_dir / candidate
            if qa_path.is_file():
                try:
                    qa_report = json.loads(qa_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    qa_report = None
                if qa_report is not None:
                    break

    plan: dict[str, Any] | None = None
    plan_candidate: str | None = None
    for candidate in (
        "fullscreen-plan-v5.json",
        "fullscreen-plan-v4.json",
        "fullscreen-plan.json",
        "draft_test/赛逸77-V6-导出测试/Codex/edit_plan.json",
        "config/edit_plan.json",
        "output/edit_plan.json",
    ):
        if candidate in copied_names:
            plan_path = snapshot_dir / candidate
            if plan_path.is_file():
                try:
                    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    plan = None
                if plan is not None:
                    plan_candidate = candidate
                    break

    lines: list[str] = []
    lines.append(f"版本：{project_name} {version}")
    source_dir = manifest.get("source_dir") or manifest.get("work_dir") or "unknown"
    lines.append(f"来源：{source_dir}")
    lines.append(f"归档：{manifest['created_at']}")
    lines.append(f"复制文件：{len(manifest.get('copied_files', []))} 个")
    lines.append(f"引用未复制：{len(manifest.get('referenced_files', []))} 个")
    if plan is not None:
        template = _plan_template(plan, snapshot_dir)
        duration_text = _plan_duration(plan)
        segment_count = len(plan.get("segments", [])) or len(
            plan.get("fullscreen_events", [])
        )
        lines.append(
            f"剪辑方案：{plan_candidate} (template={template}, duration={duration_text}, segments={segment_count})"
        )
    if qa_report is not None:
        status = qa_report.get("status") or qa_report.get("verdict") or "unknown"
        issues = len(qa_report.get("issues", []))
        repair = qa_report.get("repair")
        lines.append(
            f"QA：status={status}, issues={issues}, "
            f"auto_repair={repair.get('auto_repair_executed') if isinstance(repair, dict) else 'n/a'}"
        )
    if feedback is not None:
        changes = feedback.get("changes", [])
        lines.append(f"人工修改记录：{len(changes)} 条")
        for change in changes:
            lines.append(
                f"  - {change.get('category')}: {change.get('what')} "
                f"({change.get('before')} -> {change.get('after')})"
            )
    else:
        lines.append("人工修改记录：无 feedback.json")
    lines.append("")
    lines.append("VERSION.md:")
    lines.append(version_doc.strip())
    return {
        "ok": True,
        "project": project_name,
        "version": version,
        "summary": "\n".join(lines),
    }


def _plan_template(plan: dict[str, Any], snapshot_dir: Path) -> str:
    value = plan.get("template") or plan.get("template_name")
    if value:
        return str(value)
    config_path = snapshot_dir / "config" / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            value = config.get("template")
            if value:
                return str(value)
        except (OSError, json.JSONDecodeError):
            pass
    return "unknown"


def _plan_duration(plan: dict[str, Any]) -> str:
    for key in ("duration_seconds", "duration"):
        value = plan.get(key)
        if value is not None:
            return f"{value}s"
    main = plan.get("main_duration")
    cover = plan.get("cover_duration") or 0.0
    if main is not None:
        if float(cover) > 0:
            return f"{main}s + {cover}s cover"
        return f"{main}s"
    return "unknown"
