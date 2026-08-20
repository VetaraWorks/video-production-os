from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .probe import probe_media


def _selection_quality_checks(
    plan: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Validate planner provenance that cannot be recovered from the MP4 alone."""
    errors: list[str] = []
    warnings: list[str] = []
    perception_count = 0
    fallback_count = 0
    duplicate_reuse_ids: list[str] = []
    unsafe_ids: list[str] = []
    fingerprints: dict[str, list[str]] = {}

    selection_items = (
        plan.get("fullscreen_events", [])
        if "fullscreen_events" in plan
        else plan.get("segments", [])
    )
    for segment in selection_items:
        segment_id = str(segment.get("id") or "unknown")
        selection = segment.get("selection") or {}
        mode = str(selection.get("mode") or "legacy")
        if mode in {"perception", "local-verified"}:
            perception_count += 1
            source_start = float(segment.get("source_start", 0.0))
            source_end = source_start + float(segment.get("duration", 0.0))
            safe_start = float(selection.get("safe_start", source_start))
            safe_end = float(selection.get("safe_end", source_end))
            if source_start < safe_start - 0.002 or source_end > safe_end + 0.002:
                unsafe_ids.append(segment_id)
            fingerprint = str(selection.get("visual_fingerprint") or "").strip()
            if fingerprint:
                fingerprints.setdefault(fingerprint, []).append(segment_id)
            if bool(selection.get("duplicate_reuse")):
                duplicate_reuse_ids.append(segment_id)
        elif mode == "metadata-fallback":
            fallback_count += 1

    duplicate_fingerprints = {
        fingerprint: ids
        for fingerprint, ids in fingerprints.items()
        if len(ids) > 1
    }
    if unsafe_ids:
        errors.append(
            "Perception-guided segments exceed validated safe ranges: "
            + ", ".join(unsafe_ids)
        )
    if duplicate_reuse_ids:
        errors.append(
            "Planner marked prohibited perception reuse in segments: "
            + ", ".join(duplicate_reuse_ids)
        )
    if duplicate_fingerprints:
        detail = "; ".join(
            f"{fingerprint} => {', '.join(ids)}"
            for fingerprint, ids in sorted(duplicate_fingerprints.items())
        )
        errors.append("Duplicate visual fingerprints detected: " + detail)
    if perception_count and fallback_count:
        warnings.append(
            f"{fallback_count} segment(s) fell back to metadata despite available "
            "perception; human review is required for those segments."
        )
    if not perception_count:
        warnings.append(
            "No segment used validated video perception; semantic alignment, "
            "safe trim boundaries, and visual duplicate prevention are unverified."
        )

    checks = {
        "perception_segment_count": perception_count,
        "metadata_fallback_segment_count": fallback_count,
        "unsafe_perception_segment_ids": unsafe_ids,
        "duplicate_reuse_segment_ids": duplicate_reuse_ids,
        "duplicate_visual_fingerprints": duplicate_fingerprints,
        "requires_human_review": bool(fallback_count or not perception_count),
    }
    return checks, errors, warnings


def _run_ffmpeg_check(command: list[str]) -> tuple[bool, str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    detail = completed.stderr.strip() or completed.stdout.strip()
    return completed.returncode == 0, detail


def _measure_loudness(
    final_path: Path,
    ffmpeg: str,
    audio_config: dict[str, Any],
) -> tuple[dict[str, float] | None, str | None]:
    target_lufs = float(audio_config.get("target_lufs", -15.0))
    true_peak = float(audio_config.get("true_peak_db", -1.5))
    lra = float(audio_config.get("lra", 9.0))
    ok, detail = _run_ffmpeg_check(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(final_path),
            "-af",
            f"loudnorm=I={target_lufs:g}:TP={true_peak:g}:"
            f"LRA={lra:g}:print_format=json",
            "-f",
            "null",
            os.devnull,
        ]
    )
    if not ok:
        return None, f"Loudness measurement failed: {detail[-1200:]}"
    matches = re.findall(r"\{\s*\"input_i\".*?\}", detail, flags=re.DOTALL)
    if not matches:
        return None, "Loudness measurement returned no JSON metrics."
    try:
        payload = json.loads(matches[-1])
        return {
            "integrated_lufs": float(payload["input_i"]),
            "true_peak_dbtp": float(payload["input_tp"]),
            "lra_lu": float(payload["input_lra"]),
            "target_lufs": target_lufs,
            "true_peak_ceiling_dbtp": true_peak,
        }, None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"Could not parse loudness metrics: {exc}"


def validate_output(
    final_path: Path,
    plan: dict[str, Any],
    ffprobe: str,
    ffmpeg: str,
    tolerance_seconds: float,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not final_path.is_file() or final_path.stat().st_size <= 0:
        return {
            "ok": False,
            "path": str(final_path),
            "errors": ["Output file is missing or empty."],
            "warnings": [],
        }

    metadata = probe_media(final_path, ffprobe)
    canvas = plan["canvas"]
    expected_duration = float(plan["duration_seconds"])
    if not metadata["has_video"]:
        errors.append("Output has no video stream.")
    if not metadata["has_audio"]:
        errors.append("Output has no audio stream.")
    if metadata["width"] != int(canvas["width"]) or metadata["height"] != int(
        canvas["height"]
    ):
        errors.append(
            f"Output resolution is {metadata['width']}x{metadata['height']}; "
            f"expected {canvas['width']}x{canvas['height']}."
        )
    duration_delta = abs(float(metadata["duration"]) - expected_duration)
    if duration_delta > tolerance_seconds:
        errors.append(
            f"Output duration is {metadata['duration']:.3f}s; "
            f"expected {expected_duration:.3f}s ± {tolerance_seconds:.3f}s."
        )
    if metadata.get("video_codec") != "h264":
        warnings.append(
            f"Output video codec is {metadata.get('video_codec')}, not h264."
        )
    if metadata.get("audio_codec") != "aac":
        warnings.append(
            f"Output audio codec is {metadata.get('audio_codec')}, not aac."
        )

    decode_ok, decode_detail = _run_ffmpeg_check(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(final_path),
            "-f",
            "null",
            os.devnull,
        ]
    )
    if not decode_ok:
        errors.append(f"Full decode failed: {decode_detail[-1200:]}")

    fullscreen_mode = "fullscreen_events" in plan and "base_video" in plan
    segment_ids = {str(segment.get("id")) for segment in plan.get("segments", [])}
    if fullscreen_mode:
        segment_ids.update(
            str(section.get("id"))
            for section in plan.get("semantic_sections", [])
        )
    raw_sound_effects = plan.get("sound_effects", {})
    sound_effect_events = (
        raw_sound_effects.get("events", [])
        if isinstance(raw_sound_effects, dict)
        else raw_sound_effects
    )
    content_checks = {
        "hook_segment_present": "hook" in segment_ids,
        "cta_segment_present": "cta" in segment_ids,
        "subtitles_enabled": bool(plan.get("subtitles", {}).get("enabled")),
        "sound_effect_event_count": len(sound_effect_events),
        "continuous_voice_backbone": fullscreen_mode,
    }
    if not content_checks["hook_segment_present"]:
        errors.append("Edit plan has no hook segment.")
    if not content_checks["cta_segment_present"]:
        errors.append("Edit plan has no CTA segment.")

    selection_checks, selection_errors, selection_warnings = (
        _selection_quality_checks(plan)
    )
    content_checks["selection_quality"] = selection_checks
    errors.extend(selection_errors)
    warnings.extend(selection_warnings)

    subtitle_style = plan.get("subtitles", {}).get("style", {})
    if content_checks["subtitles_enabled"] and subtitle_style:
        margin_v = float(subtitle_style.get("margin_v", 0))
        baseline_ratio = (float(canvas["height"]) - margin_v) / float(
            canvas["height"]
        )
        content_checks["subtitle_baseline_y_ratio"] = round(baseline_ratio, 3)
        content_checks["subtitle_safe_zone"] = 0.65 <= baseline_ratio <= 0.82
        if not content_checks["subtitle_safe_zone"]:
            warnings.append(
                f"Subtitle baseline is at {baseline_ratio:.1%} of frame height; "
                "review face/product and platform UI clearance."
            )

    loudness, loudness_warning = _measure_loudness(
        final_path,
        ffmpeg,
        plan.get("audio", {}),
    )
    if loudness_warning:
        warnings.append(loudness_warning)
    elif loudness:
        if (
            loudness["true_peak_dbtp"]
            > loudness["true_peak_ceiling_dbtp"] + 0.3
        ):
            errors.append(
                f"True peak is {loudness['true_peak_dbtp']:.2f} dBTP; "
                f"ceiling is {loudness['true_peak_ceiling_dbtp']:.2f} dBTP."
            )
        if abs(loudness["integrated_lufs"] - loudness["target_lufs"]) > 1.5:
            warnings.append(
                f"Integrated loudness is {loudness['integrated_lufs']:.2f} LUFS; "
                f"target is {loudness['target_lufs']:.2f} LUFS."
            )

    return {
        "ok": not errors,
        "path": str(final_path),
        "size_bytes": final_path.stat().st_size,
        "expected_duration": expected_duration,
        "duration_delta": round(duration_delta, 3),
        "media": metadata,
        "decode_ok": decode_ok,
        "content_checks": content_checks,
        "audio_loudness": loudness,
        "errors": errors,
        "warnings": warnings,
    }
