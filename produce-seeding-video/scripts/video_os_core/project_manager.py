"""Project-level state machine driver for Video OS (Phase 2).

Responsibilities:
- Persist project_state.json with atomic writes.
- Determine stage validity from artifacts, input fingerprints, and upstream
  stage validity ("artifact truth", not just file existence).
- Run stages through the existing v6 CLI (run_pipeline.py / export_jianying.py)
  without changing their semantics.
- Resume from the last valid stage after an interrupted run.
- Enforce the per-project single-instance lock and retry limits.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locks import ProjectLock, ProjectLockError, lock_status
from .state_machine import (
    BLOCKER_STATES,
    DONE_STATES,
    OPTIONAL_STAGES,
    STAGE_ORDER,
    TransitionError,
    is_done,
    next_stage,
    stage_index,
    validate_transition,
)


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from video_pipeline.config import load_config  # noqa: E402
from video_pipeline.perception import source_signature  # noqa: E402
from video_pipeline.probe import (  # noqa: E402
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    discover_files,
    probe_media,
    resolve_executable,
)


STATE_FILENAME = "project_state.json"
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_MAX_REPAIR_ATTEMPTS = 2
MEDIA_DIRS = ("raw_video", "material", "reference")
MAX_HISTORY = 200
REVIEW_CATEGORIES = {
    "subtitles",
    "continuity",
    "jump_frame",
    "freeze_frame",
    "music",
    "voiceover",
    "sound_effect",
    "picture",
    "duplicate_shot",
    "semantic_alignment",
    "cover",
}


class ProjectNotFoundError(FileNotFoundError):
    """Raised when the project directory cannot be resolved."""


class StageExecutionError(RuntimeError):
    """Raised when executing a stage via the underlying pipeline fails."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_project_id(project: str, created_at: str) -> str:
    material = f"{project}\n{created_at}".encode("utf-8")
    return "project-" + hashlib.sha256(material).hexdigest()[:16]


def _default_stage_record(stage: str) -> dict[str, Any]:
    return {
        "status": "idle",
        "attempts": 0,
        "started_at": None,
        "ended_at": None,
        "last_error": None,
        "artifacts": [],
        "inputs": [],
        "input_fingerprint": None,
        "missing_inputs": [],
    }


def default_state(project_dir: Path) -> dict[str, Any]:
    created_at = _now_iso()
    return {
        "schema_version": 1,
        "project": project_dir.name,
        "project_id": _stable_project_id(project_dir.name, created_at),
        "project_dir": str(project_dir.resolve()),
        "version": "unreleased",
        "stage": "INIT",
        "created_at": created_at,
        "updated_at": created_at,
        "blocked": None,
        "knowledge": {
            "status": "idle",
            "evidence_id": None,
            "message": None,
            "updated_at": _now_iso(),
        },
        "stages": {stage: _default_stage_record(stage) for stage in STAGE_ORDER},
        "history": [],
    }


def state_path(project_dir: Path) -> Path:
    return Path(project_dir).resolve() / STATE_FILENAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_project_state(project_dir: Path) -> dict[str, Any]:
    path = state_path(project_dir)
    if not path.is_file():
        raise FileNotFoundError(f"Project state not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Project state must be a JSON object: {path}")
    return payload


def save_project_state(project_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    _atomic_write_json(state_path(project_dir), state)


def _record_knowledge_status(state: dict[str, Any], result: dict[str, Any]) -> None:
    """Persist auxiliary evidence status without changing production stage truth."""
    status = str(result.get("status") or "unknown")
    if status in {"no_evidence", "no_verified_evidence"}:
        return
    previous = state.get("knowledge") if isinstance(state.get("knowledge"), dict) else {}
    state["knowledge"] = {
        "status": status,
        "evidence_id": result.get("evidence_id") or previous.get("evidence_id"),
        "message": result.get("warning"),
        "synced": result.get("synced"),
        "updated_at": _now_iso(),
    }


def _transition_stage(
    state: dict[str, Any],
    target: str,
    *,
    allow_rewind: bool = False,
) -> None:
    current = str(state.get("stage") or "INIT")
    if current == target:
        return
    validate_transition(current, target, allow_rewind=allow_rewind)
    state["stage"] = target


def _synchronize_stage(state: dict[str, Any], target: str) -> None:
    """Move the stage pointer through validated hops, or an explicit rewind."""
    current = str(state.get("stage") or "INIT")
    if current == target:
        return
    if stage_index(target) < stage_index(current):
        _transition_stage(state, target, allow_rewind=True)
        return
    while current != target:
        candidate = next_stage(current)
        if candidate is None:
            raise TransitionError(f"Cannot advance stage from {current} to {target}")
        _transition_stage(state, candidate)
        current = candidate


def ensure_project_state(project_dir: Path) -> dict[str, Any]:
    project_dir = Path(project_dir).resolve()
    if state_path(project_dir).is_file():
        state = load_project_state(project_dir)
        changed = False
        stages = state.setdefault("stages", {})
        if not str(state.get("project_id") or "").strip():
            state["project_id"] = _stable_project_id(
                str(state.get("project") or project_dir.name),
                str(state.get("created_at") or state_path(project_dir)),
            )
            changed = True
        if "knowledge" not in state:
            state["knowledge"] = {
                "status": "idle",
                "evidence_id": None,
                "message": None,
                "updated_at": _now_iso(),
            }
            changed = True
        for stage in STAGE_ORDER:
            if stage not in stages:
                stages[stage] = _default_stage_record(stage)
                changed = True
        if changed:
            save_project_state(project_dir, state)
        return state
    state = default_state(project_dir)
    save_project_state(project_dir, state)
    return state


def resolve_project(arg: str, projects_root: Path) -> Path:
    """Resolve a project directory path or a name under projects_root."""
    direct = Path(arg).expanduser()
    if direct.is_dir() and (direct / "script" / "script.txt").is_file():
        return direct.resolve()
    root = Path(projects_root).expanduser().resolve()
    for candidate in (root / arg / "work", root / arg):
        if candidate.is_dir() and (candidate / "script" / "script.txt").is_file():
            return candidate.resolve()
    raise ProjectNotFoundError(
        f"Project not found: {arg} "
        f"(pass a directory containing script/script.txt or a name under {root})"
    )


# ---------------------------------------------------------------- inputs / artifacts


def media_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    for group in MEDIA_DIRS:
        files.extend(discover_files(project_dir / group, VIDEO_EXTENSIONS | AUDIO_EXTENSIONS))
    return files


def _video_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    for group in ("raw_video", "material"):
        files.extend(discover_files(project_dir / group, VIDEO_EXTENSIONS))
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
        return source_signature(path)
    return {"size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def fingerprint_bundle(files: list[Path], project_dir: Path) -> dict[str, dict[str, Any]]:
    bundle: dict[str, dict[str, Any]] = {}
    for path in files:
        if path.is_file():
            bundle[path.relative_to(project_dir).as_posix()] = _fingerprint(path)
    return bundle


def input_files(project_dir: Path, stage: str, config: dict[str, Any]) -> list[Path]:
    """Existing input files that define the stage's validity fingerprint."""
    project_dir = Path(project_dir).resolve()
    candidates: list[Path] = []
    if stage in ("INIT", "ANALYZE", "PERCEPTION"):
        candidates += [
            project_dir / "script" / "script.txt",
            project_dir / "config" / "config.json",
        ]
        candidates += media_files(project_dir)
    elif stage == "PLAN":
        candidates += [
            project_dir / "script" / "script.txt",
            project_dir / "config" / "config.json",
            project_dir / "output" / "analysis.json",
            project_dir / "perception" / "perception.json",
        ]
        candidates += media_files(project_dir)
    elif stage == "RENDER":
        candidates += [
            project_dir / "config" / "config.json",
            project_dir / "output" / "edit_plan.json",
        ]
        candidates += media_files(project_dir)
        subtitles = config.get("subtitles", {})
        subtitle_format = str(subtitles.get("format", "ass")).lower()
        if subtitle_format == "ass":
            candidates.append(project_dir / "output" / subtitles.get("filename", "subtitles.ass"))
        else:
            candidates.append(project_dir / "output" / subtitles.get("srt_filename", "subtitles.srt"))
        bgm_path = config.get("bgm", {}).get("path")
        if bgm_path:
            candidates.append(project_dir / str(bgm_path))
    elif stage == "QA":
        candidates += [
            project_dir / "config" / "config.json",
            project_dir / "output" / "edit_plan.json",
            project_dir / "output" / str(config.get("output", {}).get("filename", "final.mp4")),
        ]
    elif stage == "REVIEW":
        candidates += [
            project_dir / "script" / "script.txt",
            project_dir / "config" / "config.json",
            project_dir / "output" / "edit_plan.json",
            project_dir / "output" / str(config.get("output", {}).get("filename", "final.mp4")),
        ]
    elif stage == "REPAIR":
        candidates += [
            project_dir / "review" / "review.json",
            project_dir / "output" / "qa_report.json",
        ]
    elif stage == "JIANYING_EXPORT":
        candidates += [
            project_dir / "config" / "config.json",
            project_dir / "output" / "edit_plan.json",
        ]
        candidates += media_files(project_dir)
    return [path for path in candidates if path.is_file()]


def artifact_paths(project_dir: Path, stage: str, config: dict[str, Any]) -> list[Path]:
    project_dir = Path(project_dir).resolve()
    if stage == "ANALYZE":
        return [project_dir / "output" / "analysis.json"]
    if stage == "PERCEPTION":
        perception_config = config.get("perception", {})
        relative = str(perception_config.get("path", "perception/perception.json"))
        return [project_dir / relative]
    if stage == "PLAN":
        paths = [
            project_dir / "output" / "edit_plan.json",
            project_dir / "output" / "edit_plan.base.json",
            project_dir / "output" / "memory_context.json",
            project_dir / "output" / "memory_application.json",
        ]
        memory_config = config.get("video_os", {}).get("planner_memory", {})
        if isinstance(memory_config, dict) and memory_config.get("mode", "shadow") == "shadow":
            paths.append(project_dir / "output" / "memory_shadow_report.json")
        return paths
    if stage == "RENDER":
        return [
            project_dir
            / "output"
            / str(config.get("output", {}).get("filename", "final.mp4"))
        ]
    if stage == "QA":
        return [project_dir / "output" / "qa_report.json"]
    if stage == "REVIEW":
        return [project_dir / "review" / "review.json"]
    if stage == "REPAIR":
        return [project_dir / "repair" / "repair_diff.json"]
    if stage == "JIANYING_EXPORT":
        jianying = config.get("jianying_export", {})
        draft_root = Path(
            str(jianying.get("draft_root") or project_dir / "output" / "jianying_drafts")
        )
        draft_name = str(
            jianying.get("draft_name") or f"{project_dir.name}-Codex可编辑工程"
        )
        return [draft_root / draft_name / "Codex" / "jianying_manifest.json"]
    return []


def artifact_valid(
    project_dir: Path,
    stage: str,
    config: dict[str, Any],
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    knowledge_root: Path | str | None = None,
) -> tuple[bool, list[str]]:
    paths = artifact_paths(project_dir, stage, config)
    missing = [_safe_relative(path, project_dir) for path in paths if not path.is_file()]
    if missing:
        return False, [f"missing artifact: {item}" for item in missing]
    if stage == "RENDER":
        media_errors = _validate_final_media(
            project_dir,
            paths[0],
            config,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        if media_errors:
            return False, media_errors
    if stage in ("ANALYZE", "PLAN", "QA", "REPAIR"):
        payload = _read_json_object(paths[0])
        if payload is None:
            return False, [f"invalid JSON artifact: {paths[0].relative_to(project_dir)}"]
        if stage == "QA":
            if payload.get("ok") is not True:
                return False, ["qa_report.json does not report ok:true"]
            final_path = artifact_paths(project_dir, "RENDER", config)[0]
            media_errors = _validate_final_media(
                project_dir,
                final_path,
                config,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
            if media_errors:
                return False, media_errors
        if stage == "PLAN" and "segments" not in payload and "fullscreen_events" not in payload:
            return False, ["edit_plan.json has no segments/fullscreen_events"]
        if stage == "PLAN":
            binding_errors = _validate_plan_perception_binding(
                project_dir,
                payload,
                config,
            )
            if binding_errors:
                return False, binding_errors
            from .planner_memory import validate_planner_memory_artifacts

            memory_errors = validate_planner_memory_artifacts(
                project_dir,
                config,
                knowledge_root=knowledge_root,
            )
            if memory_errors:
                return False, memory_errors
    elif stage == "PERCEPTION":
        from .perception_manager import validate_perception_artifact

        try:
            validate_perception_artifact(project_dir, ffprobe=ffprobe)
        except (FileNotFoundError, OSError) as exc:
            return False, [f"needs_human: perception validation unavailable: {exc}"]
        except Exception as exc:  # noqa: BLE001 - invalid/stale contracts fail closed
            return False, [f"perception validation failed: {exc}"]
    elif stage == "REVIEW":
        payload = _read_json_object(paths[0])
        if payload is None:
            return False, [f"invalid JSON artifact: {paths[0].relative_to(project_dir)}"]
        review_errors = _validate_review_artifact(project_dir, payload, config)
        if review_errors:
            return False, review_errors
    elif stage == "JIANYING_EXPORT":
        payload = _read_json_object(paths[0])
        if payload is None:
            return False, [f"invalid JSON artifact: {paths[0].relative_to(project_dir)}"]
    return True, []


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validate_plan_perception_binding(
    project_dir: Path,
    plan: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    perception_config = config.get("perception", {})
    required = bool(
        perception_config.get("enabled", True)
        and perception_config.get("required", True)
    )
    if not required:
        return []
    perception = _read_json_object(project_dir / "perception" / "perception.json")
    if perception is None:
        return ["edit_plan.json requires a readable current perception.json"]
    input_signature = perception.get("input_signature")
    expected_digest = (
        str(input_signature.get("digest_sha256") or "")
        if isinstance(input_signature, dict)
        else ""
    )
    binding = plan.get("perception")
    if not isinstance(binding, dict):
        return ["edit_plan.json has no Perception consumption binding"]
    if not expected_digest or binding.get("input_signature_digest") != expected_digest:
        return ["edit_plan.json Perception binding is stale or mismatched"]

    known_ids = {
        str(segment.get("id") or "")
        for source in perception.get("sources", [])
        if isinstance(source, dict)
        for segment in source.get("segments", [])
        if isinstance(segment, dict) and segment.get("id")
    }
    selected: list[str] = []
    evidence_fields = {
        "summary",
        "semantic_tags",
        "subjects",
        "objects",
        "actions",
        "safe_start",
        "safe_end",
        "visual_fingerprint",
    }
    for segment in plan.get("segments", []):
        if not isinstance(segment, dict):
            continue
        selection = segment.get("selection")
        if not isinstance(selection, dict) or selection.get("mode") != "perception":
            continue
        segment_id = str(selection.get("perception_segment_id") or "")
        if not segment_id:
            return ["edit_plan.json has a Perception selection without segment id"]
        missing = sorted(evidence_fields - set(selection))
        if missing:
            return [
                "edit_plan.json Perception selection lacks consumed evidence: "
                + ", ".join(missing)
            ]
        selected.append(segment_id)
    if not selected:
        return ["edit_plan.json did not select any Perception segment"]
    declared = binding.get("selected_segment_ids")
    if not isinstance(declared, list) or [str(item) for item in declared] != selected:
        return ["edit_plan.json Perception selected_segment_ids do not match its segments"]
    unknown = sorted(set(selected) - known_ids)
    if unknown:
        return [
            "edit_plan.json references unknown Perception segments: "
            + ", ".join(unknown)
        ]
    return []


def _validate_review_artifact(
    project_dir: Path,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    """Validate the durable Review result against the current rendered video."""
    errors: list[str] = []
    if int(payload.get("schema_version", 0)) != 1:
        errors.append("review.json schema_version is not 1")
    if payload.get("status") != "done":
        errors.append("review.json status is not done")
    verdict = payload.get("verdict")
    if verdict not in ("pass", "fix"):
        errors.append("review.json verdict must be pass or fix")
    target = payload.get("target")
    final_path = artifact_paths(project_dir, "RENDER", config)[0]
    if not isinstance(target, dict):
        errors.append("review.json target is missing or invalid")
        return errors
    expected_path = _safe_relative(final_path, project_dir)
    if str(target.get("path") or "") != expected_path:
        errors.append(
            f"review.json target path is {target.get('path')!r}, expected {expected_path!r}"
        )
    signature = target.get("signature")
    if not isinstance(signature, dict):
        errors.append("review.json target signature is missing or invalid")
    elif final_path.is_file():
        current_signature = source_signature(final_path)
        if signature != current_signature:
            errors.append("review.json is stale for the current final.mp4 signature")
    try:
        duration = float(target["duration"])
    except (KeyError, TypeError, ValueError):
        duration = -1.0
        errors.append("review.json target duration is missing or invalid")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        errors.append("review.json issues must be an array")
        issues = []
    if verdict == "fix" and not issues:
        errors.append("review.json verdict fix requires at least one issue")
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            errors.append(f"review.json issue {index} must be an object")
            continue
        category = str(issue.get("category") or "")
        severity = str(issue.get("severity") or "")
        if category not in REVIEW_CATEGORIES:
            errors.append(f"review.json issue {index} has unknown category: {category}")
        if severity not in {"high", "medium", "low"}:
            errors.append(f"review.json issue {index} has invalid severity: {severity}")
        try:
            start = float(issue.get("start", 0.0))
            end = float(issue.get("end", start))
        except (TypeError, ValueError):
            errors.append(f"review.json issue {index} timestamps must be numeric")
            continue
        if start < 0 or end < start or duration < 0 or end > duration + 0.05:
            errors.append(
                f"review.json issue {index} timestamps outside target duration: {start}-{end}"
            )
    if not isinstance(payload.get("categories"), list):
        errors.append("review.json categories must be an array")
    return errors


def _decode_media(path: Path, ffmpeg: str) -> tuple[bool, str]:
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    detail = (completed.stderr or completed.stdout).strip()
    return completed.returncode == 0, detail


def _validate_final_media(
    project_dir: Path,
    final_path: Path,
    config: dict[str, Any],
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> list[str]:
    """Independently verify the final media instead of trusting qa_report.json."""
    errors: list[str] = []
    if not final_path.is_file() or final_path.stat().st_size <= 0:
        return ["final media validation failed: rendered output is missing or empty"]

    plan_path = project_dir / "output" / "edit_plan.json"
    plan = _read_json_object(plan_path)
    if plan is None:
        errors.append("final media validation failed: output/edit_plan.json is missing or invalid")
    else:
        canvas = plan.get("canvas")
        if not isinstance(canvas, dict):
            errors.append("final media validation failed: edit plan canvas is missing or invalid")
        try:
            expected_duration = float(plan["duration_seconds"])
        except (KeyError, TypeError, ValueError):
            expected_duration = None
            errors.append("final media validation failed: edit plan duration_seconds is missing or invalid")

    metadata: dict[str, Any] | None = None
    try:
        resolved_ffprobe = resolve_executable(ffprobe, "ffprobe")
        metadata = probe_media(final_path, resolved_ffprobe)
    except FileNotFoundError as exc:
        errors.append(f"needs_human: final media validation unavailable: {exc}")
    except OSError as exc:
        errors.append(f"needs_human: final media validation unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001 - invalid media must become a stage failure
        errors.append(f"final media validation failed: {exc}")

    if metadata is not None:
        if metadata.get("has_video") is not True:
            errors.append("final media validation failed: output has no video stream")
        if metadata.get("has_audio") is not True:
            errors.append("final media validation failed: output has no audio stream")
        if metadata.get("video_codec") != "h264":
            errors.append(
                "final media validation failed: "
                f"video codec is {metadata.get('video_codec')!r}, expected 'h264'"
            )
        if metadata.get("audio_codec") != "aac":
            errors.append(
                "final media validation failed: "
                f"audio codec is {metadata.get('audio_codec')!r}, expected 'aac'"
            )
        if plan is not None and isinstance(plan.get("canvas"), dict):
            canvas = plan["canvas"]
            try:
                expected_width = int(canvas["width"])
                expected_height = int(canvas["height"])
            except (KeyError, TypeError, ValueError):
                errors.append("final media validation failed: edit plan resolution is invalid")
            else:
                if (
                    metadata.get("width") != expected_width
                    or metadata.get("height") != expected_height
                ):
                    errors.append(
                        "final media validation failed: "
                        f"resolution is {metadata.get('width')}x{metadata.get('height')}, "
                        f"expected {expected_width}x{expected_height}"
                    )
        if plan is not None and expected_duration is not None:
            try:
                actual_duration = float(metadata.get("duration"))
                tolerance = float(
                    config.get("output", {}).get("duration_tolerance_seconds", 0.75)
                )
            except (TypeError, ValueError):
                errors.append("final media validation failed: output duration is invalid")
            else:
                if abs(actual_duration - expected_duration) > tolerance:
                    errors.append(
                        "final media validation failed: "
                        f"duration is {actual_duration:.3f}s, expected "
                        f"{expected_duration:.3f}s ± {tolerance:.3f}s"
                    )

    try:
        resolved_ffmpeg = resolve_executable(ffmpeg, "ffmpeg")
        decode_ok, decode_detail = _decode_media(final_path, resolved_ffmpeg)
    except FileNotFoundError as exc:
        errors.append(f"needs_human: final media decode unavailable: {exc}")
    except OSError as exc:
        errors.append(f"needs_human: final media decode unavailable: {exc}")
    else:
        if not decode_ok:
            errors.append(
                "final media validation failed: full decode failed: "
                + (decode_detail[-1200:] or "unknown decode error")
            )
    return errors


def _artifact_failure_status(errors: list[str]) -> str:
    if errors and all(error.startswith("needs_human:") for error in errors):
        return "needs_human"
    return "failed"


def _safe_relative(path: Path, project_dir: Path) -> str:
    try:
        return path.relative_to(project_dir).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- validity computation


def _init_errors(project_dir: Path, config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    script_path = project_dir / "script" / "script.txt"
    if not script_path.is_file() or not script_path.read_text(encoding="utf-8-sig").strip():
        errors.append("script/script.txt is missing or empty")
    if not _video_files(project_dir):
        errors.append("no video found under raw_video/ or material/")
    return errors


def _perception_blocked_kind(project_dir: Path) -> str | None:
    manifest = _read_json_object(project_dir / "perception" / "project_manifest.json")
    tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
    if not isinstance(tasks, list):
        return None
    task_ids = {
        str(item.get("task_id") or "")
        for item in tasks
        if isinstance(item, dict) and item.get("task_id")
    }
    for kind in ("needs_login", "needs_human", "failed"):
        task_dir = project_dir / "perception" / "tasks" / kind
        if task_dir.is_dir() and any(path.stem in task_ids for path in task_dir.glob("*.json")):
            return kind
    return None


def compute_stage_status(
    project_dir: Path,
    stage: str,
    record: dict[str, Any],
    config: dict[str, Any],
    upstream_ok: bool,
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    knowledge_root: Path | str | None = None,
) -> tuple[str, str | None, list[str]]:
    """Return (status, reason, missing_inputs) for one stage."""
    if stage == "INIT":
        errors = _init_errors(project_dir, config)
        if errors:
            return "invalid", "; ".join(errors), []
        return "done", None, []
    if record.get("status") in BLOCKER_STATES and stage not in {
        "PERCEPTION",
        "REVIEW",
        "REPAIR",
    }:
        return (
            record["status"],
            record.get("last_error"),
            record.get("missing_inputs") or [],
        )

    artifacts_ok, artifact_errors = artifact_valid(
        project_dir,
        stage,
        config,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        knowledge_root=knowledge_root,
    )
    stored_inputs = record.get("inputs") or []
    missing_inputs = [
        item for item in stored_inputs if not (project_dir / item).is_file()
    ]

    if stage == "PERCEPTION":
        perception_config = config.get("perception", {})
        if not perception_config.get("enabled", True):
            return "skipped", "perception disabled", []
        if artifacts_ok:
            if record.get("status") == "done":
                fresh = fingerprint_bundle(
                    input_files(project_dir, stage, config), project_dir
                )
                if record.get("input_fingerprint") != fresh:
                    return "invalid", "inputs changed; re-perception required", missing_inputs
            if upstream_ok:
                return "done", None, missing_inputs
            return "invalid", "upstream stage invalid", missing_inputs
        if missing_inputs:
            return "invalid", "missing inputs: " + ", ".join(missing_inputs), missing_inputs
        fresh_inputs = fingerprint_bundle(
            input_files(project_dir, stage, config), project_dir
        )
        if record.get("input_fingerprint") == fresh_inputs:
            blocked_kind = _perception_blocked_kind(project_dir)
            if blocked_kind:
                return blocked_kind, f"perception worker {blocked_kind}", []
        if perception_config.get("required", False):
            automatic = bool(perception_config.get("auto_run", True))
            if automatic:
                if record.get("status") in BLOCKER_STATES:
                    if record.get("input_fingerprint") == fresh_inputs:
                        return (
                            record["status"],
                            record.get("last_error"),
                            record.get("missing_inputs") or [],
                        )
                return "idle", "automatic perception required for current inputs", []
            return "needs_human", "required automatic perception is disabled", []
        return "skipped", "no perception result; metadata fallback", []

    if stage == "REVIEW":
        review_config = config.get("video_os", {}).get("review", {})
        automatic_review = bool(
            review_config.get("enabled", True)
            if isinstance(review_config, dict)
            else True
        )
        if artifacts_ok:
            if record.get("status") == "done":
                fresh = fingerprint_bundle(
                    input_files(project_dir, stage, config), project_dir
                )
                if record.get("input_fingerprint") != fresh:
                    return "invalid", "inputs changed; re-review required", missing_inputs
            if upstream_ok:
                return "done", None, missing_inputs
            return "invalid", "upstream stage invalid", missing_inputs
        if missing_inputs:
            return "invalid", "missing inputs: " + ", ".join(missing_inputs), missing_inputs
        if automatic_review:
            if record.get("status") in BLOCKER_STATES:
                fresh = fingerprint_bundle(
                    input_files(project_dir, stage, config), project_dir
                )
                if record.get("input_fingerprint") == fresh:
                    return (
                        record["status"],
                        record.get("last_error"),
                        record.get("missing_inputs") or [],
                    )
            return "idle", "automatic review required for current final.mp4", []
        review_path = project_dir / "review" / "review.json"
        if review_path.is_file():
            return "needs_human", "; ".join(artifact_errors), []
        if record.get("required"):
            return "needs_human", "new review required for repaired final.mp4", []
        return "skipped", "review optional; skipped", []

    if stage == "REPAIR":
        review = _read_json_object(project_dir / "review" / "review.json")
        if not review or review.get("verdict") != "fix":
            return "skipped", "review does not request repair", []
        if record.get("status") in BLOCKER_STATES:
            fresh = fingerprint_bundle(input_files(project_dir, stage, config), project_dir)
            if record.get("input_fingerprint") == fresh:
                return (
                    record["status"],
                    record.get("last_error"),
                    record.get("missing_inputs") or [],
                )
        if not upstream_ok:
            return "invalid", "upstream stage invalid", missing_inputs
        if record.get("status") == "done" and artifacts_ok:
            fresh = fingerprint_bundle(input_files(project_dir, stage, config), project_dir)
            if record.get("input_fingerprint") == fresh:
                return "done", None, missing_inputs
        return "idle", "review requests deterministic repair", missing_inputs

    if stage == "JIANYING_EXPORT":
        if not config.get("jianying_export", {}).get("enabled", False):
            return "skipped", "jianying export disabled", []
        if artifacts_ok and upstream_ok:
            if record.get("status") == "done":
                fresh = fingerprint_bundle(
                    input_files(project_dir, stage, config), project_dir
                )
                if record.get("input_fingerprint") != fresh:
                    return "invalid", "inputs changed; re-export required", missing_inputs
            return "done", None, missing_inputs
        if missing_inputs:
            return "invalid", "missing inputs: " + ", ".join(missing_inputs), missing_inputs
        return "idle", "needs jianying export", []

    # Regular mandatory stages (ANALYZE / PLAN / RENDER / QA / FINAL).
    if record.get("status") == "invalid":
        return "invalid", record.get("last_error") or "inputs changed; re-execution required", missing_inputs
    if missing_inputs:
        return "invalid", "missing inputs: " + ", ".join(missing_inputs), missing_inputs
    if artifacts_ok and upstream_ok:
        if record.get("status") == "done":
            fresh = fingerprint_bundle(input_files(project_dir, stage, config), project_dir)
            if record.get("input_fingerprint") != fresh:
                return "invalid", "inputs changed; re-execution required", missing_inputs
        return "done", None, missing_inputs
    if artifacts_ok and not upstream_ok:
        return "invalid", "upstream stage invalid", missing_inputs
    if artifact_errors:
        if stage in ("RENDER", "QA") and all(
            path.is_file() for path in artifact_paths(project_dir, stage, config)
        ):
            return _artifact_failure_status(artifact_errors), "; ".join(artifact_errors), missing_inputs
        return "invalid", "; ".join(artifact_errors), missing_inputs
    return "idle", "needs execution", missing_inputs


def refresh_state_validity(
    project_dir: Path,
    state: dict[str, Any],
    *,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    knowledge_root: Path | str | None = None,
) -> bool:
    """Recompute stage statuses from artifacts and inputs; return changed flag."""
    project_dir = Path(project_dir).resolve()
    changed = False
    try:
        config = load_config(project_dir)
        config_error = None
    except Exception as exc:  # noqa: BLE001 - config errors become stage blockers
        config = {}
        config_error = str(exc)

    upstream_ok = True
    for stage in STAGE_ORDER:
        record = state["stages"].get(stage) or _default_stage_record(stage)
        state["stages"][stage] = record
        if record.get("status") == "running":
            record["status"] = "idle"
            record["last_error"] = "interrupted; resume re-executes this stage"
            changed = True
        if config_error is not None:
            if record["status"] != "invalid":
                record["status"] = "invalid"
                record["last_error"] = f"config error: {config_error}"
                changed = True
            upstream_ok = False
            continue
        status, reason, missing = compute_stage_status(
            project_dir,
            stage,
            record,
            config,
            upstream_ok,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            knowledge_root=knowledge_root,
        )
        if record["status"] != status:
            record["status"] = status
            changed = True
        if reason is not None and record.get("last_error") != reason:
            record["last_error"] = reason
            changed = True
        if status == "done" and record.get("last_error"):
            record["last_error"] = None
            changed = True
        if missing != record.get("missing_inputs", []):
            record["missing_inputs"] = missing
            changed = True
        if status == "done" and not record.get("input_fingerprint"):
            files = input_files(project_dir, stage, config)
            record["inputs"] = [path.relative_to(project_dir).as_posix() for path in files]
            record["input_fingerprint"] = fingerprint_bundle(files, project_dir)
            record["artifacts"] = [
                _safe_relative(path, project_dir)
                for path in artifact_paths(project_dir, stage, config)
            ]
            changed = True
        if is_done(status):
            upstream_ok = True
        else:
            upstream_ok = False

    blocker: dict[str, Any] | None = None
    for stage in STAGE_ORDER:
        record = state["stages"][stage]
        if record["status"] in BLOCKER_STATES:
            blocker = {
                "kind": record["status"],
                "stage": stage,
                "error": record.get("last_error"),
                "at": _now_iso(),
            }
            break
    if blocker != state.get("blocked"):
        state["blocked"] = blocker
        changed = True

    current = next(
        (stage for stage in STAGE_ORDER if state["stages"][stage]["status"] not in DONE_STATES),
        "FINAL",
    )
    if state.get("stage") != current:
        _synchronize_stage(state, current)
        changed = True
    if changed:
        state["updated_at"] = _now_iso()
    return changed


def next_action(state: dict[str, Any], project_dir: Path | None = None) -> str:
    if state.get("blocked"):
        if (
            state.get("blocked", {}).get("stage") == "REPAIR"
            and project_dir is not None
            and (
                Path(project_dir) / "repair" / "repair_plan.json"
            ).is_file()
        ):
            return "apply_repair_plan"
        blocked = state["blocked"]
        return f"resolve:{blocked['kind']}@{blocked['stage']}"
    stage = state.get("stage", "INIT")
    if stage == "FINAL":
        return "none"
    status = state["stages"][stage]["status"]
    if is_done(status):
        return f"advance_from:{stage}"
    if status in BLOCKER_STATES:
        return f"resolve:{status}"
    return f"execute:{stage}"


# ---------------------------------------------------------------- execution


def _classify_failure(error: str) -> str:
    lowered = error.lower()
    if "needs_login" in lowered:
        return "needs_login"
    if "needs_human" in lowered:
        return "needs_human"
    if "executable not found" in lowered or "was not found on path" in lowered:
        return "needs_human"
    return "failed"


def _run_command(command: list[str], stage: str) -> None:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(SCRIPT_DIR),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise StageExecutionError(f"{stage} failed: {detail[-1500:]}")


def _export_jianying(project_dir: Path, config: dict[str, Any]) -> None:
    jianying = config.get("jianying_export", {})
    plan_path = project_dir / "output" / "edit_plan.json"
    if not plan_path.is_file():
        raise StageExecutionError("JIANYING_EXPORT failed: edit_plan.json missing")
    draft_root = str(jianying.get("draft_root") or project_dir / "output" / "jianying_drafts")
    draft_name = str(jianying.get("draft_name") or f"{project_dir.name}-Codex可编辑工程")
    command = [
        sys.executable,
        str(SCRIPT_DIR / "export_jianying.py"),
        str(plan_path),
        "--project-dir",
        str(project_dir),
        "--output-dir",
        str(project_dir / "output"),
        "--draft-root",
        draft_root,
        "--draft-name",
        draft_name,
    ]
    if not jianying.get("portable_media", False):
        command.append("--no-portable-media")
    _run_command(command, "JIANYING_EXPORT")


def execute_stage(
    project_dir: Path,
    stage: str,
    config: dict[str, Any],
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    knowledge_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Run one stage through the existing v6 CLI. Raises StageExecutionError."""
    if stage == "PERCEPTION":
        from .perception_manager import (
            PerceptionFailedError,
            PerceptionNeedsHumanError,
            PerceptionNeedsLoginError,
            run_automatic_perception,
        )

        try:
            return run_automatic_perception(
                project_dir,
                config,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        except PerceptionNeedsLoginError as exc:
            raise StageExecutionError(f"PERCEPTION needs_login: {exc}") from exc
        except PerceptionNeedsHumanError as exc:
            raise StageExecutionError(f"PERCEPTION needs_human: {exc}") from exc
        except PerceptionFailedError as exc:
            raise StageExecutionError(f"PERCEPTION failed: {exc}") from exc
    if stage == "REVIEW":
        from .review_manager import ReviewNeedsHumanError, run_automatic_review

        try:
            return run_automatic_review(
                project_dir,
                config,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        except ReviewNeedsHumanError as exc:
            raise StageExecutionError(f"REVIEW needs_human: {exc}") from exc
    if stage == "REPAIR":
        from .repair_manager import (  # Local import avoids the existing PM/Repair cycle.
            RepairNeedsHumanError,
            prepare_automatic_repair,
        )

        try:
            return prepare_automatic_repair(
                project_dir,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        except RepairNeedsHumanError as exc:
            raise StageExecutionError(f"REPAIR needs_human: {exc}") from exc
    if stage == "JIANYING_EXPORT":
        _export_jianying(project_dir, config)
        return
    command = [sys.executable, str(SCRIPT_DIR / "run_pipeline.py"), str(project_dir)]
    if stage in ("ANALYZE", "PLAN", "RENDER", "QA"):
        command += ["--stage", stage.lower()]
    if ffmpeg:
        command += ["--ffmpeg", ffmpeg]
    if ffprobe:
        command += ["--ffprobe", ffprobe]
    if stage == "PLAN" and knowledge_root is not None:
        command += ["--knowledge-root", str(knowledge_root)]
    _run_command(command, stage)


def _record_execution(
    project_dir: Path,
    state: dict[str, Any],
    stage: str,
    outcome: str,
) -> None:
    record = state["stages"][stage]
    record["ended_at"] = _now_iso()
    state["history"].append(
        {
            "at": _now_iso(),
            "stage": stage,
            "status": outcome,
            "attempts": record["attempts"],
            "error": record.get("last_error"),
        }
    )
    if len(state["history"]) > MAX_HISTORY:
        state["history"] = state["history"][-MAX_HISTORY:]


def _execute_stage_record(
    project_dir: Path,
    state: dict[str, Any],
    stage: str,
    config: dict[str, Any],
    ffmpeg: str | None,
    ffprobe: str | None,
    knowledge_root: Path | str | None = None,
) -> str:
    """Run one stage and update its record. Returns done/failed/interrupted."""
    record = state["stages"][stage]
    record["status"] = "running"
    record["started_at"] = _now_iso()
    record["attempts"] = int(record.get("attempts", 0)) + 1
    if stage in {"PERCEPTION", "REVIEW"}:
        files = input_files(project_dir, stage, config)
        record["inputs"] = [
            path.relative_to(project_dir).as_posix() for path in files
        ]
        record["input_fingerprint"] = fingerprint_bundle(files, project_dir)
    try:
        if knowledge_root is None:
            stage_result = execute_stage(
                project_dir,
                stage,
                config,
                ffmpeg,
                ffprobe,
            )
        else:
            stage_result = execute_stage(
                project_dir,
                stage,
                config,
                ffmpeg,
                ffprobe,
                knowledge_root=knowledge_root,
            )
        artifacts_ok, artifact_errors = artifact_valid(
            project_dir,
            stage,
            config,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            knowledge_root=knowledge_root,
        )
        if not artifacts_ok:
            detail = "; ".join(artifact_errors) or "stage produced no valid artifact"
            raise StageExecutionError(f"{stage} artifact verification failed: {detail}")
    except StageExecutionError as exc:
        record["status"] = _classify_failure(str(exc))
        record["last_error"] = str(exc)
        _record_execution(project_dir, state, stage, record["status"])
        return "failed"
    except KeyboardInterrupt:
        record["status"] = "running"
        record["last_error"] = "interrupted"
        _record_execution(project_dir, state, stage, "interrupted")
        return "interrupted"
    record["status"] = "done"
    record["last_error"] = None
    record["ended_at"] = _now_iso()
    files = input_files(project_dir, stage, config)
    record["inputs"] = [path.relative_to(project_dir).as_posix() for path in files]
    record["input_fingerprint"] = fingerprint_bundle(files, project_dir)
    record["artifacts"] = [
        _safe_relative(path, project_dir)
        for path in artifact_paths(project_dir, stage, config)
    ]
    record["missing_inputs"] = []
    if stage == "REPAIR" and isinstance(stage_result, dict):
        resume_stage = str(stage_result.get("rerun_from") or "RENDER").upper()
        if resume_stage not in {"ANALYZE", "PLAN", "RENDER"}:
            raise StageExecutionError(
                f"REPAIR returned invalid rerun stage: {resume_stage}"
            )
        record["resume_stage"] = resume_stage
        capture = stage_result.get("evidence_capture")
        if isinstance(capture, dict):
            _record_knowledge_status(state, capture)
    _record_execution(project_dir, state, stage, "done")
    return "done"


def _repair_issue_fingerprint(project_dir: Path) -> str:
    review = _read_json_object(project_dir / "review" / "review.json") or {}
    issues: list[dict[str, Any]] = []
    for issue in review.get("issues", []) or []:
        if not isinstance(issue, dict):
            continue
        issues.append(
            {
                "id": issue.get("id"),
                "category": issue.get("category"),
                "severity": issue.get("severity"),
                "start": issue.get("start"),
                "end": issue.get("end"),
                "description": issue.get("description"),
                "suggestion": issue.get("suggestion"),
            }
        )
    encoded = json.dumps(issues, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_repair_attempt(
    project_dir: Path,
    record: dict[str, Any],
    config: dict[str, Any],
) -> None:
    issue_fingerprint = _repair_issue_fingerprint(project_dir)
    if record.get("repair_issue_fingerprint") != issue_fingerprint:
        record["attempts"] = 0
        record["repair_issue_fingerprint"] = issue_fingerprint
    files = input_files(project_dir, "REPAIR", config)
    record["inputs"] = [path.relative_to(project_dir).as_posix() for path in files]
    record["input_fingerprint"] = fingerprint_bundle(files, project_dir)


def _invalidate_after_repair(
    state: dict[str, Any],
    resume_stage: str,
) -> None:
    start = stage_index(resume_stage)
    for stage in STAGE_ORDER[start:]:
        if stage == "REPAIR":
            continue
        record = state["stages"][stage]
        record["status"] = "idle" if stage == "FINAL" else "invalid"
        record["attempts"] = 0
        record["last_error"] = "invalidated by repair"
        record["inputs"] = []
        record["input_fingerprint"] = None
        record["missing_inputs"] = []
        record["artifacts"] = []
    state["stages"]["REVIEW"]["required"] = True
    state["blocked"] = None


def run_project(
    project_dir: Path,
    to: str = "FINAL",
    force: bool = False,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    knowledge_root: Path | str | None = None,
) -> dict[str, Any]:
    project_dir = Path(project_dir).expanduser().resolve()
    target = str(to).upper()
    if target not in STAGE_ORDER:
        raise ValueError(f"Unknown target stage: {to}")
    if target == "INIT":
        raise ValueError("Target stage INIT is not runnable")

    executed: list[str] = []
    with ProjectLock(project_dir) as lock:  # noqa: F841 - lock lifetime guards the run
        state = ensure_project_state(project_dir)
        if force:
            for stage in STAGE_ORDER:
                if stage in ("INIT", "PERCEPTION", "REVIEW"):
                    continue
                record = state["stages"][stage]
                record.update(
                    {
                        "status": "idle" if stage == "FINAL" else "invalid",
                        "attempts": 0,
                        "last_error": "forced re-run",
                        "inputs": [],
                        "input_fingerprint": None,
                        "missing_inputs": [],
                        "artifacts": [],
                    }
                )
            state["blocked"] = None
            _synchronize_stage(state, "INIT")
        config = load_config(project_dir)
        max_attempts = int(
            config.get("video_os", {}).get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        )
        max_repair_attempts = int(
            config.get("video_os", {}).get(
                "max_repair_attempts",
                DEFAULT_MAX_REPAIR_ATTEMPTS,
            )
        )
        reason = "unknown"
        blocked = None

        while True:
            if refresh_state_validity(
                project_dir,
                state,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                knowledge_root=knowledge_root,
            ):
                save_project_state(project_dir, state)
            stage = state["stage"]
            record = state["stages"][stage]
            status = record["status"]

            if status in BLOCKER_STATES:
                if (
                    status == "failed"
                    and int(record.get("attempts", 0)) < max_attempts
                ):
                    state["blocked"] = None
                else:
                    blocked = state.get("blocked")
                    reason = f"blocked:{status}:{record.get('last_error') or ''}"
                    break
            if stage == "FINAL":
                reason = "final"
                break
            if stage_index(stage) > stage_index(target):
                reason = "past_target"
                break
            if stage == target and is_done(status):
                reason = "reached_target"
                break
            if stage == "INIT":
                blocked = {
                    "kind": "needs_human",
                    "stage": "INIT",
                    "error": record.get("last_error") or "project contract invalid",
                    "at": _now_iso(),
                }
                state["blocked"] = blocked
                reason = "blocked:needs_human:project contract invalid"
                save_project_state(project_dir, state)
                break
            if record.get("missing_inputs"):
                blocked = {
                    "kind": "needs_human",
                    "stage": stage,
                    "error": "missing project inputs: " + ", ".join(record["missing_inputs"]),
                    "at": _now_iso(),
                }
                state["blocked"] = blocked
                reason = "blocked:needs_human:missing project inputs"
                save_project_state(project_dir, state)
                break

            if stage == "REPAIR":
                _prepare_repair_attempt(project_dir, record, config)
                if int(record.get("attempts", 0)) >= max_repair_attempts:
                    record["status"] = "needs_human"
                    record["last_error"] = (
                        "automatic repair retry limit reached for the same review issues "
                        f"({max_repair_attempts})"
                    )
                    blocked = {
                        "kind": "needs_human",
                        "stage": "REPAIR",
                        "error": record["last_error"],
                        "at": _now_iso(),
                    }
                    state["blocked"] = blocked
                    reason = "blocked:needs_human:repair retry limit reached"
                    save_project_state(project_dir, state)
                    break

            outcome = _execute_stage_record(
                project_dir,
                state,
                stage,
                config,
                ffmpeg,
                ffprobe,
                knowledge_root,
            )
            save_project_state(project_dir, state)
            if outcome == "done":
                executed.append(stage)
                if stage == "REVIEW":
                    try:
                        from .production_evidence import process_after_review

                        evidence_result = process_after_review(
                            project_dir,
                            knowledge_root=knowledge_root,
                            ffmpeg=ffmpeg,
                            ffprobe=ffprobe,
                        )
                    except Exception as exc:  # Knowledge cannot invalidate a real video.
                        evidence_result = {
                            "ok": False,
                            "status": "capture_failed",
                            "warning": (
                                "Review completed, but production evidence processing failed: "
                                + str(exc)
                            ),
                        }
                    _record_knowledge_status(state, evidence_result)
                    save_project_state(project_dir, state)
                if stage == "REPAIR":
                    resume_stage = str(record.pop("resume_stage", "RENDER"))
                    _invalidate_after_repair(state, resume_stage)
                    _transition_stage(state, resume_stage)
                    save_project_state(project_dir, state)
                    if target == "REPAIR":
                        reason = "reached_target"
                        break
                continue
            if outcome == "interrupted":
                reason = "interrupted"
                break
            # failed
            if record["status"] in {"needs_human", "needs_login"}:
                kind = record["status"]
                blocked = {
                    "kind": kind,
                    "stage": stage,
                    "error": record.get("last_error"),
                    "at": _now_iso(),
                }
                state["blocked"] = blocked
                reason = f"blocked:{kind}:{record.get('last_error') or ''}"
                save_project_state(project_dir, state)
                break
            attempt_limit = (
                max_repair_attempts if stage == "REPAIR" else max_attempts
            )
            if record["attempts"] >= attempt_limit:
                kind = _classify_failure(str(record.get("last_error") or ""))
                if stage == "REPAIR":
                    kind = "needs_human"
                record["status"] = kind
                blocked = {
                    "kind": kind,
                    "stage": stage,
                    "error": record.get("last_error"),
                    "at": _now_iso(),
                }
                state["blocked"] = blocked
                reason = f"blocked:{kind}:max attempts reached ({record['attempts']})"
                save_project_state(project_dir, state)
                break
            # retry the same stage in this invocation

        try:
            from .production_evidence import sync_verified_evidence

            sync_result = sync_verified_evidence(
                project_dir,
                knowledge_root=knowledge_root,
            )
        except Exception as exc:  # Knowledge remains auxiliary to production.
            sync_result = {
                "ok": False,
                "status": "sync_failed",
                "warning": f"video state preserved, but Knowledge evidence sync failed: {exc}",
            }
        _record_knowledge_status(state, sync_result)
        save_project_state(project_dir, state)
        skipped = [
            stage
            for stage in STAGE_ORDER
            if state["stages"][stage]["status"] == "skipped"
        ]
        knowledge = state.get("knowledge") if isinstance(state.get("knowledge"), dict) else {}
        warnings = [str(knowledge.get("message"))] if knowledge.get("message") else []
        return {
            "ok": reason in ("final", "reached_target", "past_target"),
            "project": project_dir.name,
            "project_dir": str(project_dir),
            "version": state.get("version", "unreleased"),
            "stage": state["stage"],
            "to": target,
            "force": force,
            "reason": reason,
            "blocked": blocked or state.get("blocked"),
            "executed_stages": executed,
            "skipped_stages": skipped,
            "state_file": str(state_path(project_dir)),
            "knowledge": knowledge,
            "warnings": warnings,
        }


def project_status(
    project_dir: Path,
    *,
    knowledge_root: Path | str | None = None,
) -> dict[str, Any]:
    project_dir = Path(project_dir).expanduser().resolve()
    state = ensure_project_state(project_dir)
    if refresh_state_validity(
        project_dir,
        state,
        knowledge_root=knowledge_root,
    ):
        save_project_state(project_dir, state)
    lock = lock_status(project_dir)
    stages: dict[str, dict[str, Any]] = {}
    invalid_or_missing: list[str] = []
    for stage, record in state["stages"].items():
        stages[stage] = {
            "status": record.get("status"),
            "attempts": record.get("attempts"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "last_error": record.get("last_error"),
            "artifacts": record.get("artifacts", []),
        }
        if record.get("status") in ("invalid",) or record.get("missing_inputs"):
            for item in record.get("missing_inputs", []):
                invalid_or_missing.append(item)
            if record.get("status") == "invalid":
                invalid_or_missing.append(stage)
    blocked = state.get("blocked")
    last_error = (
        blocked.get("error")
        if isinstance(blocked, dict)
        else state["stages"][state["stage"]].get("last_error")
    )
    return {
        "ok": True,
        "project": state.get("project"),
        "project_id": state.get("project_id"),
        "project_dir": str(project_dir),
        "version": state.get("version", "unreleased"),
        "stage": state["stage"],
        "locked": lock["locked"],
        "lock_pid": lock["pid"],
        "blocked": blocked,
        "needs_human": bool(blocked and blocked.get("kind") == "needs_human"),
        "needs_login": bool(blocked and blocked.get("kind") == "needs_login"),
        "next_action": next_action(state, project_dir),
        "last_error": last_error,
        "invalid_or_missing": sorted(set(invalid_or_missing)),
        "stages": stages,
        "history_count": len(state.get("history", [])),
        "state_file": str(state_path(project_dir)),
        "knowledge": state.get("knowledge"),
    }
