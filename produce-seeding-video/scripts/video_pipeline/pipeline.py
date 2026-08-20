from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analyze import attach_project_perception, build_analysis
from .config import load_config
from .jianying import export_jianying_draft
from .plan import build_edit_plan
from .probe import resolve_executable
from .render import render_plan
from .subtitles import write_subtitles
from .validate import validate_output
from video_os_core.planner_memory import (
    build_planner_memory,
    build_planner_memory_fallback,
    validate_planner_memory_artifacts,
)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _project_paths(
    project_dir: Path,
    output_dir: Path | None,
) -> tuple[Path, Path]:
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise NotADirectoryError(f"Project directory not found: {project_dir}")
    resolved_output = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else project_dir / "output"
    )
    resolved_output.mkdir(parents=True, exist_ok=True)
    return project_dir, resolved_output


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is missing or invalid: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def run_analysis_stage(
    project_dir: Path,
    output_dir: Path | None = None,
    *,
    ffprobe_path: str | None = None,
) -> dict[str, Any]:
    project_dir, output_dir = _project_paths(project_dir, output_dir)
    ffprobe = resolve_executable(ffprobe_path, "ffprobe")
    config = load_config(project_dir)
    analysis = build_analysis(
        project_dir,
        config,
        ffprobe,
        include_perception=False,
    )
    analysis_path = output_dir / "analysis.json"
    write_json(analysis_path, analysis)
    return {
        "ok": True,
        "stage": "ANALYZE",
        "project": str(project_dir),
        "output_dir": str(output_dir),
        "analysis": str(analysis_path),
        "payload": analysis,
    }


def run_plan_stage(
    project_dir: Path,
    output_dir: Path | None = None,
    *,
    knowledge_root: Path | str | None = None,
) -> dict[str, Any]:
    project_dir, output_dir = _project_paths(project_dir, output_dir)
    config = load_config(project_dir)
    analysis = _load_json_object(output_dir / "analysis.json", "analysis.json")
    analysis, perception = attach_project_perception(
        analysis,
        project_dir,
        config,
    )
    plan = build_edit_plan(analysis, project_dir, config)
    plan_override_path = project_dir / "config" / "edit_plan.json"
    if plan_override_path.is_file():
        override = _load_json_object(plan_override_path, "Edit-plan override")
        plan = override
        plan.pop("memory", None)
        plan.setdefault("warnings", [])
        plan["warnings"].append(
            "Loaded reviewed edit plan from config/edit_plan.json."
        )

    selected_perception = [
        str(selection["perception_segment_id"])
        for segment in plan.get("segments", [])
        if isinstance(segment, dict)
        and isinstance((selection := segment.get("selection")), dict)
        and selection.get("mode") == "perception"
        and selection.get("perception_segment_id")
    ]
    perception_config = config.get("perception", {})
    perception_required = bool(
        perception_config.get("enabled", True)
        and perception_config.get("required", True)
    )
    if perception_required and not selected_perception:
        raise ValueError(
            "PLAN requires current Perception evidence, but no validated perception "
            "segment was selected"
        )
    if perception:
        input_signature = perception.get("input_signature") or {}
        plan["perception"] = {
            "input_signature_digest": input_signature.get("digest_sha256"),
            "provider": perception.get("provider"),
            "source_count": len(perception.get("sources", [])),
            "segment_count": sum(
                len(source.get("segments", []))
                for source in perception.get("sources", [])
                if isinstance(source, dict)
            ),
            "selected_segment_ids": selected_perception,
        }

    timed_cues: list[dict[str, Any]] | None = None
    speech_timeline_path = project_dir / "speech_timeline.json"
    if speech_timeline_path.is_file():
        speech_timeline = _load_json_object(
            speech_timeline_path,
            "speech_timeline.json",
        )
        timed_cues = list(speech_timeline.get("cues", []))
        if not timed_cues:
            raise ValueError(
                f"Speech timeline contains no timed cues: {speech_timeline_path}"
            )
        plan.setdefault("subtitles", {})["timing_mode"] = str(
            speech_timeline.get("timing_mode", "speech-timeline")
        )
        plan["subtitles"]["speech_timeline"] = "speech_timeline.json"

    base_plan = plan
    memory_perception = perception if isinstance(perception, dict) else {}
    final_plan, memory_context, memory_application, shadow_report = build_planner_memory(
        project_dir,
        config,
        base_plan,
        memory_perception,
        knowledge_root=knowledge_root,
    )
    base_plan_path = output_dir / "edit_plan.base.json"
    memory_context_path = output_dir / "memory_context.json"
    memory_application_path = output_dir / "memory_application.json"
    plan_path = output_dir / "edit_plan.json"
    write_json(base_plan_path, base_plan)
    write_json(memory_context_path, memory_context)
    write_json(memory_application_path, memory_application)
    if shadow_report is not None:
        write_json(output_dir / "memory_shadow_report.json", shadow_report)
    else:
        shadow_path = output_dir / "memory_shadow_report.json"
        if shadow_path.is_file():
            shadow_path.unlink()
    write_json(plan_path, final_plan)
    memory_errors = validate_planner_memory_artifacts(
        project_dir,
        config,
        knowledge_root=knowledge_root,
    )
    if memory_errors:
        final_plan, memory_context, memory_application, shadow_report = (
            build_planner_memory_fallback(
                project_dir,
                config,
                base_plan,
                memory_perception,
                knowledge_root=knowledge_root,
                reason="memory_application_validation_failed",
                warnings=[
                    "Planner Memory validation failed; using the unchanged Base Plan: "
                    + "; ".join(memory_errors)
                ],
            )
        )
        write_json(memory_context_path, memory_context)
        write_json(memory_application_path, memory_application)
        if shadow_report is not None:
            write_json(output_dir / "memory_shadow_report.json", shadow_report)
        elif shadow_path.is_file():
            shadow_path.unlink()
        write_json(plan_path, final_plan)
        fallback_errors = validate_planner_memory_artifacts(
            project_dir,
            config,
            knowledge_root=knowledge_root,
        )
        if fallback_errors:
            raise ValueError(
                "Planner Memory fallback validation failed: "
                + "; ".join(fallback_errors)
            )
    plan = final_plan
    subtitle_paths: dict[str, Path] = {}
    if plan["subtitles"]["enabled"]:
        subtitle_paths = write_subtitles(
            output_dir,
            analysis["script"]["sentences"],
            float(plan["duration_seconds"]),
            plan["canvas"],
            config.get("subtitles", {}),
            hook_end=float(plan["subtitles"].get("hook_end", 3.0)),
            cta_start=plan["subtitles"].get("cta_start"),
            timed_cues=timed_cues,
        )
    return {
        "ok": True,
        "stage": "PLAN",
        "project": str(project_dir),
        "output_dir": str(output_dir),
        "edit_plan": str(plan_path),
        "base_edit_plan": str(base_plan_path),
        "memory_context": str(memory_context_path),
        "memory_application": str(memory_application_path),
        "subtitles": {
            key: str(path) for key, path in subtitle_paths.items()
        }
        if subtitle_paths
        else None,
        "warnings": plan.get("warnings", []),
        "payload": plan,
    }


def run_render_stage(
    project_dir: Path,
    output_dir: Path | None = None,
    *,
    keep_work: bool = False,
    ffmpeg_path: str | None = None,
) -> dict[str, Any]:
    project_dir, output_dir = _project_paths(project_dir, output_dir)
    config = load_config(project_dir)
    plan = _load_json_object(output_dir / "edit_plan.json", "edit_plan.json")
    ffmpeg = resolve_executable(ffmpeg_path, "ffmpeg")
    final_path = render_plan(
        plan,
        project_dir,
        output_dir,
        ffmpeg,
        config,
        keep_work=keep_work,
    )
    return {
        "ok": True,
        "stage": "RENDER",
        "project": str(project_dir),
        "output_dir": str(output_dir),
        "final": str(final_path),
    }


def run_qa_stage(
    project_dir: Path,
    output_dir: Path | None = None,
    *,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> dict[str, Any]:
    project_dir, output_dir = _project_paths(project_dir, output_dir)
    config = load_config(project_dir)
    plan = _load_json_object(output_dir / "edit_plan.json", "edit_plan.json")
    final_path = output_dir / str(
        config.get("output", {}).get("filename", "final.mp4")
    )
    ffprobe = resolve_executable(ffprobe_path, "ffprobe")
    ffmpeg = resolve_executable(ffmpeg_path, "ffmpeg")
    qa = validate_output(
        final_path,
        plan,
        ffprobe,
        ffmpeg,
        float(config.get("output", {}).get("duration_tolerance_seconds", 0.75)),
    )
    qa_path = output_dir / "qa_report.json"
    write_json(qa_path, qa)
    return {
        "ok": bool(qa["ok"]),
        "stage": "QA",
        "project": str(project_dir),
        "output_dir": str(output_dir),
        "qa_report": str(qa_path),
        "payload": qa,
    }


def run_stage(
    project_dir: Path,
    stage: str,
    output_dir: Path | None = None,
    *,
    keep_work: bool = False,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
    knowledge_root: Path | str | None = None,
) -> dict[str, Any]:
    """Run exactly one deterministic pipeline stage for the Video OS Director."""
    normalized = str(stage).upper()
    if normalized == "ANALYZE":
        return run_analysis_stage(
            project_dir,
            output_dir,
            ffprobe_path=ffprobe_path,
        )
    if normalized == "PLAN":
        return run_plan_stage(
            project_dir,
            output_dir,
            knowledge_root=knowledge_root,
        )
    if normalized == "RENDER":
        return run_render_stage(
            project_dir,
            output_dir,
            keep_work=keep_work,
            ffmpeg_path=ffmpeg_path,
        )
    if normalized == "QA":
        result = run_qa_stage(
            project_dir,
            output_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
        if not result["ok"]:
            qa = result["payload"]
            raise RuntimeError(
                "Rendered output failed QA: "
                + "; ".join(qa.get("errors", []) or ["unknown"])
            )
        return result
    raise ValueError(f"Unsupported pipeline stage: {stage}")


def run_project(
    project_dir: Path,
    output_dir: Path | None = None,
    *,
    plan_only: bool = False,
    keep_work: bool = False,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
    export_jianying: bool | None = None,
    jianying_draft_root: Path | None = None,
    jianying_draft_name: str | None = None,
    jianying_portable_media: bool | None = None,
    jianying_zip_path: Path | None = None,
    knowledge_root: Path | str | None = None,
) -> dict[str, Any]:
    project_dir, output_dir = _project_paths(project_dir, output_dir)
    config = load_config(project_dir)
    analysis_result = run_analysis_stage(
        project_dir,
        output_dir,
        ffprobe_path=ffprobe_path,
    )
    plan_result = run_plan_stage(
        project_dir,
        output_dir,
        knowledge_root=knowledge_root,
    )
    plan = plan_result["payload"]

    result: dict[str, Any] = {
        "ok": True,
        "mode": "plan-only" if plan_only else "render",
        "project": str(project_dir),
        "output_dir": str(output_dir),
        "analysis": analysis_result["analysis"],
        "edit_plan": plan_result["edit_plan"],
        "subtitles": plan_result["subtitles"],
        "warnings": plan.get("warnings", []),
    }
    jianying_config = config.get("jianying_export", {})
    jianying_enabled = (
        bool(export_jianying)
        if export_jianying is not None
        else bool(jianying_config.get("enabled", False))
    )
    if jianying_enabled:
        configured_root = jianying_config.get("draft_root")
        draft_root = (
            jianying_draft_root.expanduser().resolve()
            if jianying_draft_root is not None
            else Path(str(configured_root)).expanduser().resolve()
            if configured_root
            else output_dir / "jianying_drafts"
        )
        portable_media = (
            bool(jianying_portable_media)
            if jianying_portable_media is not None
            else bool(jianying_config.get("portable_media", False))
        )
        draft_name = (
            jianying_draft_name
            or str(jianying_config.get("draft_name") or "").strip()
            or f"{project_dir.name}-Codex可编辑工程"
        )
        jianying_result = export_jianying_draft(
            plan,
            project_dir=project_dir,
            output_dir=output_dir,
            draft_root=draft_root,
            draft_name=draft_name,
            portable_media=portable_media,
            zip_path=jianying_zip_path,
        )
        result["jianying_project"] = jianying_result
    if plan_only:
        return result

    try:
        render_result = run_render_stage(
            project_dir,
            output_dir,
            keep_work=keep_work,
            ffmpeg_path=ffmpeg_path,
        )
        qa_result = run_qa_stage(
            project_dir,
            output_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
        )
    except Exception as exc:
        qa = {
            "ok": False,
            "stage": "render",
            "errors": [str(exc)],
            "warnings": plan.get("warnings", []),
        }
        write_json(output_dir / "qa_report.json", qa)
        raise

    qa = qa_result["payload"]
    result.update(
        {
            "ok": bool(qa["ok"]),
            "final": render_result["final"],
            "qa_report": qa_result["qa_report"],
        }
    )
    if not qa["ok"]:
        raise RuntimeError(
            "Rendered output failed QA: " + "; ".join(qa.get("errors", []))
        )
    return result
