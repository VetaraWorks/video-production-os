from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must be a JSON object: {path}")
    return value


def _validate(config: dict[str, Any]) -> None:
    planner_memory = config.get("video_os", {}).get("planner_memory", {})
    planner_memory_mode = str(
        planner_memory.get("mode", "shadow")
        if isinstance(planner_memory, dict)
        else "shadow"
    )
    if planner_memory_mode not in {"off", "shadow", "advisory"}:
        raise ValueError(
            "video_os.planner_memory.mode must be 'off', 'shadow', or 'advisory'"
        )

    canvas = config.get("canvas", {})
    width = int(canvas.get("width", 0))
    height = int(canvas.get("height", 0))
    fps = float(canvas.get("fps", 0))
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("canvas.width, canvas.height, and canvas.fps must be positive")
    if width % 2 or height % 2:
        raise ValueError("canvas.width and canvas.height must be even for H.264 output")

    segments = config.get("template_segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("template_segments must be a non-empty array")

    expected_start = 0.0
    ids: set[str] = set()
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Every template segment must be an object")
        segment_id = str(segment.get("id", "")).strip()
        if not segment_id or segment_id in ids:
            raise ValueError("Every template segment must have a unique non-empty id")
        ids.add(segment_id)
        start = float(segment.get("start", -1))
        end = float(segment.get("end", -1))
        if abs(start - expected_start) > 0.001:
            raise ValueError(
                f"Template timeline is discontinuous before {segment_id}: "
                f"expected {expected_start}, got {start}"
            )
        if end <= start:
            raise ValueError(f"Template segment {segment_id} must have positive duration")
        expected_start = end

    duration = float(config.get("duration_seconds", 0))
    if duration <= 0 or abs(duration - expected_start) > 0.001:
        raise ValueError(
            "duration_seconds must be positive and equal the final segment end"
        )

    subtitles = config.get("subtitles", {})
    subtitle_format = str(subtitles.get("format", "ass")).lower()
    if subtitle_format not in {"ass", "srt"}:
        raise ValueError("subtitles.format must be 'ass' or 'srt'")
    for key, fallback in (
        ("filename", "subtitles.ass"),
        ("srt_filename", "subtitles.srt"),
    ):
        value = Path(str(subtitles.get(key, fallback)))
        if value.name != str(value) or value.is_absolute():
            raise ValueError(f"subtitles.{key} must be a filename, not a path")
    margin_v = int(subtitles.get("margin_v", round(height * 0.25)))
    if margin_v < 0 or margin_v > round(height * 0.45):
        raise ValueError(
            "subtitles.margin_v must stay between 0 and 45% of canvas height"
        )
    if int(subtitles.get("max_chars_per_cue", 10)) <= 0:
        raise ValueError("subtitles.max_chars_per_cue must be positive")

    audio = config.get("audio", {})
    target_lufs = float(audio.get("target_lufs", -15.0))
    true_peak = float(audio.get("true_peak_db", -1.5))
    if not -24 <= target_lufs <= -9:
        raise ValueError("audio.target_lufs must be between -24 and -9")
    if not -6 <= true_peak <= -0.5:
        raise ValueError("audio.true_peak_db must be between -6 and -0.5")

    sound_effects = config.get("sound_effects", {})
    events = sound_effects.get("events", [])
    if not isinstance(events, list):
        raise ValueError("sound_effects.events must be an array")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Every sound-effect event must be an object")
        if not str(event.get("segment", "")).strip():
            raise ValueError("Every sound-effect event must name a segment")
        if not str(event.get("asset", "")).strip():
            raise ValueError("Every sound-effect event must name an asset")


def load_config(project_dir: Path) -> dict[str, Any]:
    skill_root = Path(__file__).resolve().parents[2]
    default_path = skill_root / "assets" / "default-config.json"
    config = _read_json(default_path)
    override_path = project_dir / "config" / "config.json"
    if override_path.is_file():
        config = _deep_merge(config, _read_json(override_path))
    _validate(config)
    return config
