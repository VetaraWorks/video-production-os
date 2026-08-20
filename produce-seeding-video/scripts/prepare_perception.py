from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from video_pipeline.config import load_config  # noqa: E402
from video_pipeline.perception import (  # noqa: E402
    perception_input_signature,
    source_signature,
    validate_perception,
)
from video_pipeline.probe import (  # noqa: E402
    VIDEO_EXTENSIONS,
    discover_files,
    probe_media,
    resolve_executable,
)


QUEUE_STATES = (
    "queued",
    "running",
    "uploading",
    "uploaded",
    "analyzing",
    "validating",
    "done",
    "failed",
    "needs_login",
    "needs_human",
)

ALLOWED_TRANSITIONS = {
    "queued": {"running", "needs_login", "needs_human", "failed"},
    "running": {"uploading", "queued", "failed", "needs_login", "needs_human"},
    "uploading": {"uploaded", "queued", "failed", "needs_login", "needs_human"},
    "uploaded": {"analyzing", "queued", "failed", "needs_login", "needs_human"},
    "analyzing": {"validating", "queued", "failed", "needs_login", "needs_human"},
    "validating": {"done", "queued", "failed", "needs_human"},
    "failed": {"queued", "needs_human"},
    "needs_login": {"queued", "running", "needs_human"},
    "needs_human": {"queued", "running", "failed"},
    "done": set(),
}

REVIEW_QUEUE_STATES = (
    "queued",
    "running",
    "uploading",
    "uploaded",
    "analyzing",
    "validating",
    "done",
    "failed",
    "needs_login",
    "needs_human",
)

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

REVIEW_SEVERITIES = {"high", "medium", "low"}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _project_video_records(project_dir: Path, ffprobe: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for group in ("raw_video", "material", "reference"):
        for path in discover_files(project_dir / group, VIDEO_EXTENSIONS):
            media = probe_media(path, ffprobe)
            if not media.get("has_video") or float(media.get("duration", 0)) <= 0:
                continue
            signature = source_signature(path)
            records.append(
                {
                    "source": path.relative_to(project_dir).as_posix(),
                    "path": path.relative_to(project_dir).as_posix(),
                    "group": group,
                    "has_video": True,
                    "absolute_source": str(path.resolve()),
                    "duration": round(float(media["duration"]), 3),
                    "signature": signature,
                }
            )
    return records


def _proxy_name(record: dict[str, Any]) -> str:
    source = Path(str(record["source"]))
    token = str(record["signature"]["sample_sha256"])[:12]
    safe_stem = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in source.stem
    ).strip("_") or "video"
    return f"{record['group']}-{safe_stem}-{token}.proxy.mp4"


def _make_proxy(
    record: dict[str, Any],
    proxy_path: Path,
    ffmpeg: str,
    ffprobe: str,
    force: bool,
) -> bool:
    if proxy_path.is_file() and not force:
        metadata = probe_media(proxy_path, ffprobe)
        if (
            metadata.get("has_video")
            and abs(float(metadata.get("duration", 0)) - float(record["duration"])) <= 0.75
        ):
            return False
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = proxy_path.with_suffix(".tmp.mp4")
    if temporary.exists():
        temporary.unlink()
    common = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(record["absolute_source"]),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        r"scale=min(720\,iw):-2",
        "-r",
        "15",
    ]
    software_encoding = [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-maxrate",
        "1200k",
        "-bufsize",
        "2400k",
    ]
    media_foundation_encoding = [
        "-c:v",
        "h264_mf",
        "-b:v",
        "900k",
    ]
    audio_and_output = [
        "-c:a",
        "aac",
        "-b:a",
        "48k",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    completed = subprocess.run(
        common + software_encoding + audio_and_output,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 and (
        "Unknown encoder 'libx264'" in completed.stderr
        or "Unrecognized option 'crf'" in completed.stderr
    ):
        temporary.unlink(missing_ok=True)
        completed = subprocess.run(
            common + media_foundation_encoding + audio_and_output,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"Proxy generation failed for {record['source']}: "
            f"{completed.stderr.strip()[-1600:]}"
        )
    metadata = probe_media(temporary, ffprobe)
    if not metadata.get("has_video"):
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Proxy has no video stream: {record['source']}")
    delta = abs(float(metadata.get("duration", 0)) - float(record["duration"]))
    if delta > 0.75:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"Proxy duration drift for {record['source']}: {delta:.3f}s"
        )
    temporary.replace(proxy_path)
    return True


def _make_review_proxy(
    target: dict[str, Any],
    proxy_path: Path,
    ffmpeg: str,
    ffprobe: str,
    force: bool,
) -> bool:
    """Build a review proxy that keeps the original timebase and legible subtitles."""
    if proxy_path.is_file() and not force:
        metadata = probe_media(proxy_path, ffprobe)
        if (
            metadata.get("has_video")
            and abs(float(metadata.get("duration", 0)) - float(target["duration"])) <= 0.75
        ):
            return False
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = proxy_path.with_suffix(".tmp.mp4")
    if temporary.exists():
        temporary.unlink()
    common = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(target["absolute_path"]),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        r"scale=min(720\,iw):-2",
        "-r",
        "30",
    ]
    encoding = [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-maxrate",
        "2500k",
        "-bufsize",
        "5000k",
    ]
    audio_and_output = [
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    completed = subprocess.run(
        common + encoding + audio_and_output,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"Review proxy generation failed: {completed.stderr.strip()[-1600:]}"
        )
    metadata = probe_media(temporary, ffprobe)
    if not metadata.get("has_video"):
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Review proxy has no video stream")
    delta = abs(float(metadata.get("duration", 0)) - float(target["duration"]))
    if delta > 0.75:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Review proxy duration drift: {delta:.3f}s")
    temporary.replace(proxy_path)
    return True


def _review_target(project_dir: Path, ffprobe: str) -> dict[str, Any]:
    config = load_config(project_dir)
    filename = str(config.get("output", {}).get("filename", "final.mp4"))
    path = (project_dir / "output" / filename).resolve()
    try:
        path.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError("review output must stay inside the project") from exc
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"Rendered output not found: {path}")
    media = probe_media(path, ffprobe)
    if not media.get("has_video") or float(media.get("duration", 0)) <= 0:
        raise ValueError(f"Rendered output is not a usable video: {path}")
    return {
        "path": path.relative_to(project_dir).as_posix(),
        "absolute_path": str(path.resolve()),
        "duration": round(float(media["duration"]), 3),
        "signature": source_signature(path),
    }


def _review_task_id(project_dir: Path, signature: dict[str, Any]) -> str:
    """Bind a Review task identity to the complete rendered-file signature."""
    encoded = json.dumps(
        signature,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{project_dir.name}-review-{digest[:16]}"


def _existing_review_task(
    queue_root: Path,
    task_id: str,
) -> dict[str, Any] | None:
    matches = [
        queue_root / state / f"{task_id}.json"
        for state in REVIEW_QUEUE_STATES
        if (queue_root / state / f"{task_id}.json").is_file()
    ]
    if len(matches) > 1:
        raise ValueError(
            f"Expected at most one review task file for {task_id}; found {len(matches)}"
        )
    if not matches:
        return None
    payload = json.loads(matches[0].read_text(encoding="utf-8-sig"))
    if payload.get("task_id") != task_id or payload.get("task_type") != "review":
        raise ValueError(f"Review task mismatch in {matches[0]}")
    return payload


def prepare_review(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise NotADirectoryError(f"Project directory not found: {project_dir}")
    ffmpeg = resolve_executable(args.ffmpeg, "ffmpeg")
    ffprobe = resolve_executable(args.ffprobe, "ffprobe")
    target = _review_target(project_dir, ffprobe)

    work_root = (
        args.work_root.expanduser().resolve()
        if args.work_root
        else project_dir / "preprocess"
    )
    proxy_root = work_root / "review_proxy"
    review_root = project_dir / "review"
    queue_root = review_root / "tasks"
    for state in REVIEW_QUEUE_STATES:
        (queue_root / state).mkdir(parents=True, exist_ok=True)
    result_root = review_root / "results"
    result_root.mkdir(parents=True, exist_ok=True)

    signature = target["signature"]
    proxy_path = proxy_root / f"{Path(target['path']).stem}-{signature['sample_sha256'][:12]}.review.mp4"
    generated = _make_review_proxy(
        target,
        proxy_path,
        ffmpeg,
        ffprobe,
        args.force,
    )
    task_id = _review_task_id(project_dir, signature)
    task = _existing_review_task(queue_root, task_id)
    if task is None:
        task = {
            "schema_version": 1,
            "task_type": "review",
            "task_id": task_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
            "project_dir": str(project_dir),
            "target": target["path"],
            "target_duration": target["duration"],
            "target_signature": signature,
            "proxy_path": str(proxy_path.resolve()),
            "script_path": str((project_dir / "script" / "script.txt").resolve()),
            "edit_plan_path": str((project_dir / "output" / "edit_plan.json").resolve()),
            "prompt_contract": "references/review-prompt.md",
            "result_path": str((result_root / f"{task_id}.json").resolve()),
            "error": None,
        }
        task_path = queue_root / "queued" / f"{task_id}.json"
        _write_json(task_path, task)
    elif task.get("target_signature") != signature:
        raise ValueError(f"Review task {task_id} does not match current target signature")
    manifest = {
        "schema_version": 1,
        "project": str(project_dir),
        "work_root": str(work_root),
        "target": target,
        "generated_proxy_count": 1 if generated else 0,
        "task": task,
    }
    _write_json(review_root / "project_review_manifest.json", manifest)
    return manifest


def review_status(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    queue_root = project_dir / "review" / "tasks"
    counts = {
        state: len(list((queue_root / state).glob("*.json")))
        if (queue_root / state).is_dir()
        else 0
        for state in REVIEW_QUEUE_STATES
    }
    return {
        "project": str(project_dir),
        "states": counts,
        "total": sum(counts.values()),
    }


def _find_review_task(project_dir: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    queue_root = project_dir / "review" / "tasks"
    matches = [
        queue_root / state / f"{task_id}.json"
        for state in REVIEW_QUEUE_STATES
        if (queue_root / state / f"{task_id}.json").is_file()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one review task file for {task_id}; found {len(matches)}"
        )
    path = matches[0]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("task_id") != task_id or payload.get("task_type") != "review":
        raise ValueError(f"Review task mismatch in {path}")
    return path, payload


def _transition_review_payload(
    project_dir: Path,
    task_path: Path,
    task: dict[str, Any],
    target_state: str,
    *,
    error: str | None = None,
    worker_id: str | None = None,
    allow_direct_done: bool = False,
) -> tuple[Path, dict[str, Any]]:
    current = str(task.get("status"))
    if target_state not in REVIEW_QUEUE_STATES:
        raise ValueError(f"Unknown review queue state: {target_state}")
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target_state not in allowed and not (allow_direct_done and target_state == "done"):
        raise ValueError(f"Invalid review task transition: {current} -> {target_state}")
    task["status"] = target_state
    task["updated_at"] = datetime.now(timezone.utc).isoformat()
    if current == "queued" and target_state == "running":
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["started_at"] = task["updated_at"]
    if worker_id:
        task["worker_id"] = worker_id
    task["error"] = (
        error
        if target_state in {"failed", "needs_login", "needs_human"}
        else None
    )
    destination = (
        project_dir
        / "review"
        / "tasks"
        / target_state
        / task_path.name
    )
    temporary = destination.with_suffix(".tmp")
    _write_json(temporary, task)
    if destination.exists():
        raise FileExistsError(f"Destination review task already exists: {destination}")
    temporary.replace(destination)
    task_path.unlink()
    return destination, task


def transition_review(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    task_path, task = _find_review_task(project_dir, args.task_id)
    destination, task = _transition_review_payload(
        project_dir,
        task_path,
        task,
        args.state,
        error=args.error,
        worker_id=args.worker_id,
    )
    return {
        "task_id": args.task_id,
        "status": task["status"],
        "task_path": str(destination),
    }


def _validate_review_result(
    raw: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    verdict = str(raw.get("verdict") or "").strip()
    if verdict not in {"pass", "fix"}:
        raise ValueError("review result must have verdict 'pass' or 'fix'")
    try:
        overall_score = float(raw.get("overall_score", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("review overall_score must be numeric") from exc
    if not 0 <= overall_score <= 100:
        raise ValueError("review overall_score must be between 0 and 100")
    issues = raw.get("issues")
    if not isinstance(issues, list):
        raise ValueError("review result must contain an issues array")
    duration = float(task["target_duration"])
    normalized_issues: list[dict[str, Any]] = []
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise ValueError(f"review issue {index} must be an object")
        category = str(issue.get("category") or "").strip()
        severity = str(issue.get("severity") or "").strip()
        if category not in REVIEW_CATEGORIES:
            raise ValueError(f"unknown review category: {category}")
        if severity not in REVIEW_SEVERITIES:
            raise ValueError(f"unknown review severity: {severity}")
        try:
            start = float(issue.get("start", 0.0))
            end = float(issue.get("end", start))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"review issue {index} timestamps must be numeric") from exc
        if start < 0 or end < start or end > duration + 0.05:
            raise ValueError(
                f"review issue {index} timestamps outside target duration: {start}-{end}"
            )
        normalized_issues.append(
            {
                "id": str(issue.get("id") or f"review-issue-{index + 1}"),
                "severity": severity,
                "category": category,
                "start": round(start, 3),
                "end": round(end, 3),
                "description": str(issue.get("description") or "").strip(),
                "evidence": str(issue.get("evidence") or "").strip(),
                "suggestion": str(issue.get("suggestion") or "").strip(),
            }
        )
    if verdict == "fix" and not normalized_issues:
        raise ValueError("review verdict 'fix' requires at least one issue")
    categories = raw.get("categories")
    if not isinstance(categories, list):
        raise ValueError("review result must contain a categories array")
    normalized_categories: list[dict[str, Any]] = []
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise ValueError(f"review category {index} must be an object")
        name = str(category.get("name") or "").strip()
        if name not in REVIEW_CATEGORIES:
            raise ValueError(f"unknown review category name: {name}")
        status = str(category.get("status") or "").strip()
        if status not in {"pass", "warning", "fail"}:
            raise ValueError(f"review category {name} has invalid status: {status}")
        try:
            score = float(category.get("score", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"review category {name} score must be numeric") from exc
        normalized_categories.append(
            {
                "name": name,
                "score": score,
                "status": status,
                "notes": str(category.get("notes") or "").strip(),
            }
        )
    return {
        "schema_version": 1,
        "task_id": task["task_id"],
        "status": "done",
        "verdict": verdict,
        "overall_score": overall_score,
        "summary": str(raw.get("summary") or "").strip(),
        "provider": raw.get("provider", {"name": "manual-import", "model": "unknown"}),
        "target": {
            "path": task["target"],
            "duration": duration,
            "signature": task["target_signature"],
        },
        "categories": normalized_categories,
        "issues": normalized_issues,
        "recommendations": [
            str(item).strip() for item in raw.get("recommendations", []) if str(item).strip()
        ],
    }


def import_review_result(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    task_path, task = _find_review_task(project_dir, args.task_id)
    result_path = args.result_json.expanduser().resolve()
    raw = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Review provider response must be a JSON object")
    normalized = _validate_review_result(raw, task)
    stored_result = Path(str(task["result_path"]))
    _write_json(stored_result, normalized)
    destination, task = _transition_review_payload(
        project_dir,
        task_path,
        task,
        "done",
        worker_id=args.worker_id,
        allow_direct_done=True,
    )
    task["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(destination, task)
    review_root = project_dir / "review"
    (review_root / "review.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "task_id": task["task_id"],
        "status": "done",
        "verdict": normalized["verdict"],
        "issue_count": len(normalized["issues"]),
        "stored_result": str(stored_result),
        "review": str(review_root / "review.json"),
    }


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise NotADirectoryError(f"Project directory not found: {project_dir}")
    ffmpeg = resolve_executable(args.ffmpeg, "ffmpeg")
    ffprobe = resolve_executable(args.ffprobe, "ffprobe")
    records = _project_video_records(project_dir, ffprobe)
    if not records:
        raise ValueError("No usable videos found for perception preparation")
    input_signature = perception_input_signature(project_dir, records)
    input_digest = str(input_signature["digest_sha256"])

    work_root = (
        args.work_root.expanduser().resolve()
        if args.work_root
        else project_dir / "preprocess"
    )
    proxy_root = work_root / "proxy"
    perception_root = project_dir / "perception"
    queue_root = perception_root / "tasks"
    for state in QUEUE_STATES:
        (queue_root / state).mkdir(parents=True, exist_ok=True)
    result_root = perception_root / "results"
    result_root.mkdir(parents=True, exist_ok=True)

    tasks: list[dict[str, Any]] = []
    generated = 0
    for record in records:
        proxy_path = proxy_root / _proxy_name(record)
        if _make_proxy(record, proxy_path, ffmpeg, ffprobe, args.force):
            generated += 1
        task_id = (
            f"{project_dir.name}-{input_digest[:12]}-"
            f"{record['signature']['sample_sha256'][:12]}"
        )
        task = {
            "schema_version": 1,
            "task_id": task_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
            "project_dir": str(project_dir),
            "source": record["source"],
            "source_duration": record["duration"],
            "source_signature": record["signature"],
            "input_signature_digest": input_digest,
            "proxy_path": str(proxy_path.resolve()),
            "script_path": str((project_dir / "script" / "script.txt").resolve()),
            "prompt_contract": "references/perception-prompt.md",
            "result_path": str((result_root / f"{task_id}.json").resolve()),
            "error": None,
        }
        existing = next(
            (
                state_dir / f"{task_id}.json"
                for state_dir in (queue_root / state for state in QUEUE_STATES)
                if (state_dir / f"{task_id}.json").is_file()
            ),
            None,
        )
        task_path = existing or queue_root / "queued" / f"{task_id}.json"
        if existing and not args.force:
            task = json.loads(existing.read_text(encoding="utf-8-sig"))
        else:
            if existing:
                existing.unlink()
            _write_json(task_path, task)
        tasks.append(
            {
                "task_id": task_id,
                "status": task["status"],
                "source": record["source"],
                "proxy_path": str(proxy_path.resolve()),
                "task_path": str(task_path.resolve()),
            }
        )

    manifest = {
        "schema_version": 1,
        "project": str(project_dir),
        "input_signature": input_signature,
        "work_root": str(work_root),
        "task_count": len(tasks),
        "generated_proxy_count": generated,
        "tasks": tasks,
    }
    _write_json(perception_root / "project_manifest.json", manifest)
    return manifest


def status(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    queue_root = project_dir / "perception" / "tasks"
    counts = {
        state: len(list((queue_root / state).glob("*.json")))
        if (queue_root / state).is_dir()
        else 0
        for state in QUEUE_STATES
    }
    return {"project": str(project_dir), "states": counts, "total": sum(counts.values())}


def _find_task(project_dir: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    queue_root = project_dir / "perception" / "tasks"
    matches = [
        queue_root / state / f"{task_id}.json"
        for state in QUEUE_STATES
        if (queue_root / state / f"{task_id}.json").is_file()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one task file for {task_id}; found {len(matches)}"
        )
    path = matches[0]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("task_id") != task_id:
        raise ValueError(f"Task id mismatch in {path}")
    return path, payload


def _transition_payload(
    project_dir: Path,
    task_path: Path,
    task: dict[str, Any],
    target_state: str,
    *,
    error: str | None = None,
    worker_id: str | None = None,
    allow_direct_done: bool = False,
) -> tuple[Path, dict[str, Any]]:
    current = str(task.get("status"))
    if target_state not in QUEUE_STATES:
        raise ValueError(f"Unknown queue state: {target_state}")
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if target_state not in allowed and not (allow_direct_done and target_state == "done"):
        raise ValueError(f"Invalid task transition: {current} -> {target_state}")
    task["status"] = target_state
    task["updated_at"] = datetime.now(timezone.utc).isoformat()
    if current == "queued" and target_state == "running":
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["started_at"] = task["updated_at"]
    if worker_id:
        task["worker_id"] = worker_id
    task["error"] = error if target_state in {"failed", "needs_login", "needs_human"} else None
    destination = (
        project_dir
        / "perception"
        / "tasks"
        / target_state
        / task_path.name
    )
    temporary = destination.with_suffix(".tmp")
    _write_json(temporary, task)
    if destination.exists():
        raise FileExistsError(f"Destination task already exists: {destination}")
    temporary.replace(destination)
    task_path.unlink()
    return destination, task


def transition(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    task_path, task = _find_task(project_dir, args.task_id)
    destination, task = _transition_payload(
        project_dir,
        task_path,
        task,
        args.state,
        error=args.error,
        worker_id=args.worker_id,
    )
    return {
        "task_id": args.task_id,
        "status": task["status"],
        "task_path": str(destination),
    }


def admit_perception_result(
    project_dir: Path,
    task_id: str,
    raw: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    """Validate and atomically admit one Provider payload through the queue contract."""
    project_dir = Path(project_dir).expanduser().resolve()
    task_path, task = _find_task(project_dir, task_id)
    source_payload = raw.get("source") if isinstance(raw, dict) else None
    if not isinstance(source_payload, dict):
        source_payload = raw
    if not isinstance(source_payload, dict) or not isinstance(
        source_payload.get("segments"), list
    ):
        raise ValueError("Worker result must contain a source object with segments")
    result_source = source_payload.get("source")
    if result_source and str(result_source) != str(task["source"]):
        raise ValueError(
            f"Worker result source mismatch: {result_source} vs {task['source']}"
        )
    source_payload["source"] = task["source"]
    source_payload["duration"] = task["source_duration"]
    source_payload["signature"] = task["source_signature"]
    normalized_result = {
        "schema_version": 1,
        "task_id": task["task_id"],
        "status": "done",
        "provider": raw.get("provider", {"name": "manual-import", "model": "unknown"}),
        "input_signature_digest": task.get("input_signature_digest"),
        "source": source_payload,
    }
    stored_result = Path(str(task["result_path"]))
    _write_json(stored_result, normalized_result)
    destination, task = _transition_payload(
        project_dir,
        task_path,
        task,
        "done",
        worker_id=worker_id,
        allow_direct_done=True,
    )
    task["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(destination, task)
    return {
        "task_id": task["task_id"],
        "status": "done",
        "stored_result": str(stored_result),
    }


def import_result(args: argparse.Namespace) -> dict[str, Any]:
    result_path = args.result_json.expanduser().resolve()
    raw = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("Worker result must be a JSON object")
    return admit_perception_result(
        args.project_dir,
        args.task_id,
        raw,
        worker_id=args.worker_id,
    )


def _namespace_cross_source_segment_ids(sources: list[dict[str, Any]]) -> None:
    """Keep Provider IDs when unique and deterministically qualify collisions."""
    seen: set[str] = set()
    for source in sources:
        source_path = str(source.get("source") or "")
        source_key = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:10]
        segments = source.get("segments")
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            provider_id = str(segment.get("id") or "").strip()
            if not provider_id or provider_id not in seen:
                if provider_id:
                    seen.add(provider_id)
                continue
            candidate = f"{provider_id}--{source_key}"
            counter = 2
            while candidate in seen:
                candidate = f"{provider_id}--{source_key}-{counter}"
                counter += 1
            segment["provider_segment_id"] = provider_id
            segment["id"] = candidate
            seen.add(candidate)


def merge(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    manifest_path = project_dir / "perception" / "project_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Current Perception manifest is missing or invalid: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tasks"), list):
        raise ValueError("Current Perception manifest is incomplete")

    ffprobe = resolve_executable(args.ffprobe, "ffprobe")
    records = _project_video_records(project_dir, ffprobe)
    current_input_signature = perception_input_signature(project_dir, records)
    manifest_signature = manifest.get("input_signature")
    if not isinstance(manifest_signature, dict) or (
        manifest_signature.get("digest_sha256")
        != current_input_signature["digest_sha256"]
    ):
        raise ValueError("Current Perception manifest is stale for the project inputs")

    expected_task_ids = [
        str(item.get("task_id") or "")
        for item in manifest["tasks"]
        if isinstance(item, dict)
    ]
    if not expected_task_ids or any(not task_id for task_id in expected_task_ids):
        raise ValueError("Current Perception manifest has invalid task identities")
    done_tasks: list[Path] = []
    active: dict[str, int] = {}
    for task_id in expected_task_ids:
        task_path, task = _find_task(project_dir, task_id)
        status = str(task.get("status") or "")
        if status != "done":
            active[status] = active.get(status, 0) + 1
        else:
            done_tasks.append(task_path)
    if active:
        raise ValueError(f"Cannot merge while current tasks are not done: {active}")

    sources: list[dict[str, Any]] = []
    providers: list[dict[str, Any]] = []
    for task_path in done_tasks:
        task = json.loads(task_path.read_text(encoding="utf-8-sig"))
        if task.get("input_signature_digest") != current_input_signature["digest_sha256"]:
            raise ValueError(f"Completed task is stale for current inputs: {task_path}")
        result_path = Path(str(task["result_path"]))
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        if result.get("input_signature_digest") != current_input_signature["digest_sha256"]:
            raise ValueError(f"Completed result is stale for current inputs: {result_path}")
        source_payload = result.get("source")
        if not isinstance(source_payload, dict):
            raise ValueError(f"Completed result has no source object: {result_path}")
        sources.append(source_payload)
        provider = result.get("provider")
        if isinstance(provider, dict):
            providers.append(provider)

    _namespace_cross_source_segment_ids(sources)

    unique_providers = {
        (str(item.get("name", "unknown")), str(item.get("model", "unknown")))
        for item in providers
    }
    provider = (
        {"name": next(iter(unique_providers))[0], "model": next(iter(unique_providers))[1]}
        if len(unique_providers) == 1
        else {"name": "mixed", "model": "mixed"}
    )
    payload = {
        "schema_version": 1,
        "status": "done",
        "input_signature": current_input_signature,
        "provider": provider,
        "sources": sources,
    }
    media = [
        {
            "path": item["source"],
            "group": item["group"],
            "has_video": True,
            "duration": item["duration"],
        }
        for item in records
    ]
    config = load_config(project_dir)
    normalized = validate_perception(payload, project_dir, media, config)
    configured_path = str(
        config.get("perception", {}).get("path", "perception/perception.json")
    )
    output_path = (project_dir / configured_path).resolve()
    try:
        output_path.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError("perception.path must stay inside the project") from exc
    _write_json(output_path, normalized)
    return {
        "ok": True,
        "output": str(output_path),
        "source_count": len(normalized["sources"]),
        "segment_count": sum(
            len(source["segments"]) for source in normalized["sources"]
        ),
    }


def validate(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = args.project_dir.expanduser().resolve()
    config = load_config(project_dir)
    ffprobe = resolve_executable(args.ffprobe, "ffprobe")
    configured_path = str(
        config.get("perception", {}).get("path", "perception/perception.json")
    )
    perception_path = (project_dir / configured_path).resolve()
    try:
        perception_path.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError("perception.path must stay inside the project") from exc
    if not perception_path.is_file():
        raise ValueError(f"Perception result not found: {perception_path}")
    payload = json.loads(perception_path.read_text(encoding="utf-8-sig"))
    records = _project_video_records(project_dir, ffprobe)
    media = [
        {
            "path": item["source"],
            "has_video": True,
            "duration": item["duration"],
        }
        for item in records
    ]
    normalized = validate_perception(payload, project_dir, media, config)
    return {
        "project": str(project_dir),
        "perception": str(perception_path),
        "source_count": len(normalized["sources"]),
        "segment_count": sum(
            len(source["segments"]) for source in normalized["sources"]
        ),
        "ok": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare and validate external video-perception tasks"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "status", "validate", "merge"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("project_dir", type=Path)
        if command in ("prepare", "validate", "merge"):
            subparser.add_argument("--ffprobe")
        if command == "prepare":
            subparser.add_argument("--ffmpeg")
            subparser.add_argument("--work-root", type=Path)
            subparser.add_argument("--force", action="store_true")
    transition_parser = subparsers.add_parser("transition")
    transition_parser.add_argument("project_dir", type=Path)
    transition_parser.add_argument("task_id")
    transition_parser.add_argument("state", choices=QUEUE_STATES)
    transition_parser.add_argument("--error")
    transition_parser.add_argument("--worker-id")
    import_parser = subparsers.add_parser("import-result")
    import_parser.add_argument("project_dir", type=Path)
    import_parser.add_argument("task_id")
    import_parser.add_argument("result_json", type=Path)
    import_parser.add_argument("--worker-id")
    prepare_review_parser = subparsers.add_parser("prepare-review")
    prepare_review_parser.add_argument("project_dir", type=Path)
    prepare_review_parser.add_argument("--ffmpeg")
    prepare_review_parser.add_argument("--ffprobe")
    prepare_review_parser.add_argument("--work-root", type=Path)
    prepare_review_parser.add_argument("--force", action="store_true")
    review_status_parser = subparsers.add_parser("review-status")
    review_status_parser.add_argument("project_dir", type=Path)
    review_transition_parser = subparsers.add_parser("review-transition")
    review_transition_parser.add_argument("project_dir", type=Path)
    review_transition_parser.add_argument("task_id")
    review_transition_parser.add_argument("state", choices=REVIEW_QUEUE_STATES)
    review_transition_parser.add_argument("--error")
    review_transition_parser.add_argument("--worker-id")
    import_review_parser = subparsers.add_parser("import-review-result")
    import_review_parser.add_argument("project_dir", type=Path)
    import_review_parser.add_argument("task_id")
    import_review_parser.add_argument("result_json", type=Path)
    import_review_parser.add_argument("--worker-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = {
            "prepare": prepare,
            "status": status,
            "validate": validate,
            "transition": transition,
            "import-result": import_result,
            "merge": merge,
            "prepare-review": prepare_review,
            "review-status": review_status,
            "review-transition": transition_review,
            "import-review-result": import_review_result,
        }[args.command](args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
