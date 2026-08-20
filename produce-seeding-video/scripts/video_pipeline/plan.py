from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def _score_candidate(
    media: dict[str, Any],
    preferred_tags: list[str],
    reuse_count: int,
) -> tuple[int, int, int, str]:
    tags = set(media.get("tags", []))
    match_score = 0
    first_match_position = len(preferred_tags) + 1
    for index, tag in enumerate(preferred_tags):
        if tag in tags:
            match_score += max(1, len(preferred_tags) - index)
            first_match_position = min(first_match_position, index)
    audio_bonus = 1 if "talking" in preferred_tags and media.get("has_audio") else 0
    return (-match_score, first_match_position, reuse_count - audio_bonus, media["path"])


def _slot_durations(total: float, maximum: float, minimum: float = 1.0) -> list[float]:
    if total <= 0:
        return []
    maximum = max(minimum, maximum)
    count = max(1, math.ceil(total / maximum))
    while count > 1 and total / count < minimum:
        count -= 1
    base = round(total / count, 3)
    durations = [base] * count
    durations[-1] = round(total - sum(durations[:-1]), 3)
    return durations


def _perception_candidates(
    videos: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    perception_config = config.get("perception", {})
    minimum_confidence = float(perception_config.get("minimum_confidence", 0.55))
    minimum_quality = float(perception_config.get("minimum_quality_score", 0.55))
    candidates: list[dict[str, Any]] = []
    for media in videos:
        source_perception = media.get("perception") or {}
        for segment in source_perception.get("segments", []):
            quality = segment.get("quality") or {}
            if not quality.get("usable", True):
                continue
            confidence = float(segment.get("confidence", 0.0))
            quality_score = float(quality.get("score", 0.0))
            if confidence < minimum_confidence or quality_score < minimum_quality:
                continue
            safe_start = float(segment["safe_start"])
            safe_end = float(segment["safe_end"])
            tags = set(media.get("tags", []))
            for field in ("semantic_tags", "subjects", "objects", "actions"):
                tags.update(str(value) for value in segment.get(field, []))
            candidates.append(
                {
                    "source": media["path"],
                    "source_duration": float(media["duration"]),
                    "has_audio": bool(media.get("has_audio")),
                    "perception_segment_id": str(segment["id"]),
                    "safe_start": safe_start,
                    "safe_end": safe_end,
                    "available_duration": round(safe_end - safe_start, 3),
                    "tags": sorted(tags),
                    "summary": str(segment.get("summary", "")),
                    "semantic_tags": list(segment.get("semantic_tags", [])),
                    "subjects": list(segment.get("subjects", [])),
                    "objects": list(segment.get("objects", [])),
                    "actions": list(segment.get("actions", [])),
                    "quality_score": quality_score,
                    "confidence": confidence,
                    "visual_fingerprint": str(
                        segment.get("visual_fingerprint") or segment["id"]
                    ),
                }
            )
    return candidates


def _perception_rank(
    candidate: dict[str, Any],
    preferred_tags: list[str],
    used_fingerprints: set[str],
    used_source_intervals: dict[str, list[tuple[float, float]]],
) -> tuple[int, int, float, float, str, str]:
    tags = set(candidate["tags"])
    match_score = sum(
        max(1, len(preferred_tags) - index)
        for index, tag in enumerate(preferred_tags)
        if tag in tags
    )
    duplicate_penalty = (
        1 if candidate["visual_fingerprint"] in used_fingerprints else 0
    )
    overlap_penalty = 0
    for used_start, used_end in used_source_intervals[candidate["source"]]:
        overlap = max(
            0.0,
            min(candidate["safe_end"], used_end)
            - max(candidate["safe_start"], used_start),
        )
        if overlap > 0.08:
            overlap_penalty = 1
            break
    return (
        duplicate_penalty + overlap_penalty,
        -match_score,
        -float(candidate["quality_score"]),
        -float(candidate["confidence"]),
        str(candidate["source"]),
        str(candidate["perception_segment_id"]),
    )


def _build_perception_guided_segments(
    videos: list[dict[str, Any]],
    config: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]] | None:
    candidates = _perception_candidates(videos, config)
    if not candidates:
        return None

    perception_config = config.get("perception", {})
    default_maximum = float(perception_config.get("max_shot_seconds", 3.5))
    talking_maximum = float(
        perception_config.get("max_talking_shot_seconds", 6.0)
    )
    allow_duplicate_fingerprint = bool(
        perception_config.get("allow_duplicate_fingerprint", False)
    )
    used_fingerprints: set[str] = set()
    used_source_intervals: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
    source_cursors: defaultdict[str, float] = defaultdict(float)
    reuse_counts: defaultdict[str, int] = defaultdict(int)
    segments: list[dict[str, Any]] = []
    fallback_count = 0
    duplicate_count = 0

    for template_segment in config["template_segments"]:
        preferred = [str(tag) for tag in template_segment.get("preferred_tags", [])]
        segment_start = float(template_segment["start"])
        segment_end = float(template_segment["end"])
        total_duration = round(segment_end - segment_start, 3)
        maximum = (
            talking_maximum
            if preferred and preferred[0] == "talking"
            else min(default_maximum, total_duration)
        )
        durations = _slot_durations(total_duration, maximum)
        cursor = segment_start
        for slot_index, duration in enumerate(durations):
            eligible = [
                candidate
                for candidate in candidates
                if float(candidate["available_duration"]) + 0.02 >= duration
            ]
            if not allow_duplicate_fingerprint:
                eligible = [
                    candidate
                    for candidate in eligible
                    if _perception_rank(
                        candidate,
                        preferred,
                        used_fingerprints,
                        used_source_intervals,
                    )[0]
                    == 0
                ]
            selected: dict[str, Any] | None = None
            if eligible:
                selected = min(
                    eligible,
                    key=lambda item: _perception_rank(
                        item,
                        preferred,
                        used_fingerprints,
                        used_source_intervals,
                    ),
                )

            segment_id = (
                str(template_segment["id"])
                if slot_index == 0
                else f"{template_segment['id']}-{slot_index + 1:02d}"
            )
            if selected is not None:
                duplicate = selected["visual_fingerprint"] in used_fingerprints
                if duplicate:
                    duplicate_count += 1
                available_slack = max(0.0, selected["available_duration"] - duration)
                offset = (
                    source_cursors[selected["perception_segment_id"]] % available_slack
                    if available_slack > 0.01
                    else 0.0
                )
                source_start = float(selected["safe_start"]) + offset
                source_cursors[selected["perception_segment_id"]] += max(
                    0.25, duration * 0.41
                )
                tags = set(selected["tags"])
                matched_tags = [tag for tag in preferred if tag in tags]
                segments.append(
                    {
                        "id": segment_id,
                        "template_segment": str(template_segment["id"]),
                        "intent": template_segment.get("intent", ""),
                        "timeline_start": round(cursor, 3),
                        "timeline_end": round(cursor + duration, 3),
                        "duration": duration,
                        "source": selected["source"],
                        "source_start": round(source_start, 3),
                        "source_duration": round(selected["source_duration"], 3),
                        "has_audio": selected["has_audio"],
                        "loop": False,
                        "matched_tags": matched_tags,
                        "selection": {
                            "mode": "perception",
                            "perception_segment_id": selected[
                                "perception_segment_id"
                            ],
                            "summary": selected["summary"],
                            "semantic_tags": selected["semantic_tags"],
                            "subjects": selected["subjects"],
                            "objects": selected["objects"],
                            "actions": selected["actions"],
                            "safe_start": round(float(selected["safe_start"]), 3),
                            "safe_end": round(float(selected["safe_end"]), 3),
                            "quality_score": selected["quality_score"],
                            "confidence": selected["confidence"],
                            "visual_fingerprint": selected[
                                "visual_fingerprint"
                            ],
                            "duplicate_reuse": duplicate,
                        },
                    }
                )
                used_fingerprints.add(selected["visual_fingerprint"])
                used_source_intervals[selected["source"]].append(
                    (source_start, source_start + duration)
                )
                reuse_counts[selected["source"]] += 1
            else:
                fallback_count += 1
                ranked = sorted(
                    videos,
                    key=lambda item: _score_candidate(
                        item, preferred, reuse_counts[item["path"]]
                    ),
                )
                selected_media = ranked[0]
                source_duration = float(selected_media["duration"])
                loop = source_duration + 0.02 < duration
                available_start = max(0.0, source_duration - duration)
                source_start = (
                    source_cursors[selected_media["path"]] % available_start
                    if available_start > 0.01
                    else 0.0
                )
                source_cursors[selected_media["path"]] += max(0.5, duration * 0.37)
                tags = set(selected_media.get("tags", []))
                segments.append(
                    {
                        "id": segment_id,
                        "template_segment": str(template_segment["id"]),
                        "intent": template_segment.get("intent", ""),
                        "timeline_start": round(cursor, 3),
                        "timeline_end": round(cursor + duration, 3),
                        "duration": duration,
                        "source": selected_media["path"],
                        "source_start": round(source_start, 3),
                        "source_duration": round(source_duration, 3),
                        "has_audio": bool(selected_media.get("has_audio")),
                        "loop": loop,
                        "matched_tags": [tag for tag in preferred if tag in tags],
                        "selection": {
                            "mode": "metadata-fallback",
                            "reason": "no validated safe perception range was long enough",
                        },
                    }
                )
                reuse_counts[selected_media["path"]] += 1
            cursor = round(cursor + duration, 3)

    warnings.append(
        f"Perception-guided planning created {len(segments)} short visual segments."
    )
    if fallback_count:
        warnings.append(
            f"Perception planning used metadata fallback for {fallback_count} segment(s)."
        )
    if duplicate_count:
        warnings.append(
            f"Perception planning reused {duplicate_count} visual fingerprint(s) because "
            "unique safe footage was insufficient."
        )
    return segments


def _choose_bgm(
    analysis: dict[str, Any],
    project_dir: Path,
    config: dict[str, Any],
) -> str | None:
    bgm = config.get("bgm", {})
    if not bgm.get("enabled", True):
        return None
    explicit = bgm.get("path")
    if explicit:
        candidate = (project_dir / str(explicit)).resolve()
        try:
            candidate.relative_to(project_dir.resolve())
        except ValueError as exc:
            raise ValueError("bgm.path must resolve inside the project directory") from exc
        if not candidate.is_file():
            raise FileNotFoundError(f"Configured BGM not found: {candidate}")
        return candidate.relative_to(project_dir).as_posix()
    candidate = next(
        (
            item["path"]
            for item in analysis["media"]
            if item.get("has_audio")
            and not item.get("has_video")
            and "bgm" in item.get("tags", [])
        ),
        None,
    )
    return candidate


def _build_sound_effects(
    config: dict[str, Any],
    segments: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    sound_config = config.get("sound_effects", {})
    if not sound_config.get("enabled", True):
        return []

    segment_starts = {
        str(segment["id"]): float(segment["timeline_start"]) for segment in segments
    }
    skill_assets = Path(__file__).resolve().parents[2] / "assets"
    events: list[dict[str, Any]] = []
    for event in sound_config.get("events", []):
        segment_id = str(event.get("segment", "")).strip()
        if segment_id not in segment_starts:
            warnings.append(
                f"Skipped sound effect for unknown segment: {segment_id or '<empty>'}."
            )
            continue
        relative_asset = Path(str(event.get("asset", "")))
        asset_path = (skill_assets / relative_asset).resolve()
        try:
            asset_path.relative_to(skill_assets.resolve())
        except ValueError:
            warnings.append(
                f"Skipped sound effect outside skill assets: {relative_asset.as_posix()}."
            )
            continue
        if not asset_path.is_file():
            warnings.append(
                f"Skipped missing sound effect asset: {relative_asset.as_posix()}."
            )
            continue
        at = segment_starts[segment_id] + float(event.get("offset", 0))
        if at < 0 or at >= float(config["duration_seconds"]):
            warnings.append(
                f"Skipped out-of-range sound effect at {at:.3f}s for {segment_id}."
            )
            continue
        events.append(
            {
                "segment": segment_id,
                "at": round(at, 3),
                "asset": f"assets/{relative_asset.as_posix()}",
                "volume": float(event.get("volume", 0.18)),
            }
        )
    return events


def build_edit_plan(
    analysis: dict[str, Any],
    project_dir: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    videos = [
        item
        for item in analysis["media"]
        if item.get("has_video") and float(item.get("duration", 0)) > 0
    ]
    if not videos:
        raise ValueError("Cannot build an edit plan without usable video")

    warnings = list(analysis.get("warnings", []))
    segments = _build_perception_guided_segments(videos, config, warnings)
    if segments is None:
        reuse_counts: defaultdict[str, int] = defaultdict(int)
        source_cursors: defaultdict[str, float] = defaultdict(float)
        segments = []
        for template_segment in config["template_segments"]:
            preferred = [
                str(tag) for tag in template_segment.get("preferred_tags", [])
            ]
            ranked = sorted(
                videos,
                key=lambda item: _score_candidate(
                    item, preferred, reuse_counts[item["path"]]
                ),
            )
            selected = ranked[0]
            segment_start = float(template_segment["start"])
            segment_end = float(template_segment["end"])
            segment_duration = round(segment_end - segment_start, 3)
            source_duration = float(selected["duration"])
            loop = source_duration + 0.02 < segment_duration
            if loop:
                source_start = 0.0
                warnings.append(
                    f"{template_segment['id']} loops {selected['path']} because "
                    f"{source_duration:.2f}s is shorter than {segment_duration:.2f}s."
                )
            else:
                available_start = max(0.0, source_duration - segment_duration)
                source_start = (
                    source_cursors[selected["path"]] % available_start
                    if available_start > 0.01
                    else 0.0
                )
                source_cursors[selected["path"]] += max(
                    0.5, segment_duration * 0.37
                )

            tags = set(selected.get("tags", []))
            matched_tags = [tag for tag in preferred if tag in tags]
            if not matched_tags:
                warnings.append(
                    f"{template_segment['id']} uses fallback media {selected['path']} "
                    "without a preferred role tag."
                )

            segments.append(
                {
                    "id": template_segment["id"],
                    "intent": template_segment.get("intent", ""),
                    "timeline_start": segment_start,
                    "timeline_end": segment_end,
                    "duration": segment_duration,
                    "source": selected["path"],
                    "source_start": round(source_start, 3),
                    "source_duration": round(source_duration, 3),
                    "has_audio": bool(selected.get("has_audio")),
                    "loop": loop,
                    "matched_tags": matched_tags,
                }
            )
            reuse_counts[selected["path"]] += 1

        reused = [path for path, count in reuse_counts.items() if count > 1]
        if reused:
            warnings.append("Reused source files: " + ", ".join(sorted(reused)))

    bgm_path = _choose_bgm(analysis, project_dir, config)
    if config.get("bgm", {}).get("enabled", True) and not bgm_path:
        warnings.append("BGM is enabled but no explicit or tagged BGM asset was found.")

    sound_effects = _build_sound_effects(config, segments, warnings)
    subtitle_config = config.get("subtitles", {})
    subtitle_format = str(subtitle_config.get("format", "ass")).lower()
    subtitle_filename = (
        subtitle_config.get("filename", "subtitles.ass")
        if subtitle_format == "ass"
        else subtitle_config.get("srt_filename", "subtitles.srt")
    )
    cta_start = next(
        (
            float(segment["timeline_start"])
            for segment in segments
            if segment["id"] == "cta"
        ),
        None,
    )

    return {
        "schema_version": 2,
        "template": config["template"],
        "canvas": config["canvas"],
        "duration_seconds": float(config["duration_seconds"]),
        "segments": segments,
        "subtitles": {
            "enabled": bool(subtitle_config.get("enabled", True)),
            "source": "script/script.txt",
            "format": subtitle_format,
            "filename": subtitle_filename,
            "srt_filename": subtitle_config.get("srt_filename", "subtitles.srt"),
            "preset": subtitle_config.get("preset", "social-bold"),
            "timing_mode": "short-phrase-length-heuristic",
            "style": {
                "font": subtitle_config.get("font", "Microsoft YaHei"),
                "font_size": int(subtitle_config.get("font_size", 64)),
                "hook_font_size": int(
                    subtitle_config.get(
                        "hook_font_size",
                        subtitle_config.get("font_size", 64),
                    )
                ),
                "margin_v": int(subtitle_config.get("margin_v", 480)),
                "outline": int(subtitle_config.get("outline", 5)),
                "primary_color": subtitle_config.get(
                    "primary_color", "#FFFFFF"
                ),
                "highlight_color": subtitle_config.get(
                    "highlight_color", "#FFE66D"
                ),
            },
            "hook_end": next(
                (
                    float(segment["timeline_end"])
                    for segment in segments
                    if segment["id"] == "hook"
                ),
                3.0,
            ),
            "cta_start": cta_start,
        },
        "bgm": {
            "enabled": bool(bgm_path),
            "path": bgm_path,
            "volume": float(config.get("bgm", {}).get("volume", 0.12)),
            "fade_seconds": float(
                config.get("bgm", {}).get("fade_seconds", 0.8)
            ),
            "ducking": config.get("bgm", {}).get("ducking", {}),
        },
        "sound_effects": {
            "enabled": bool(sound_effects),
            "events": sound_effects,
        },
        "audio": config.get("audio", {}),
        "render": {
            **config["encoder"],
            "output_filename": config.get("output", {}).get(
                "filename", "final.mp4"
            ),
        },
        "warnings": warnings,
    }
