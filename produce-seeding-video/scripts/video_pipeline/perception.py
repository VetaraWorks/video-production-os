from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


PERCEPTION_INPUT_SIGNATURE_ALGORITHM = "video-os-perception-input-v1"


class PerceptionValidationError(ValueError):
    """Raised when a perception artifact is unsafe to use for editing."""


def source_signature(path: Path, sample_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Build a fast identity signature without hashing an entire large video."""

    stat = path.stat()
    size = stat.st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    offsets = sorted({0, max(0, size // 2 - sample_bytes // 2), max(0, size - sample_bytes)})
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            digest.update(offset.to_bytes(8, "little", signed=False))
            digest.update(handle.read(sample_bytes))
    return {
        "size_bytes": size,
        "mtime_ns": stat.st_mtime_ns,
        "sample_sha256": digest.hexdigest(),
    }


def perception_input_signature(
    project_dir: Path,
    media: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind Perception to the current script and original-video inputs."""

    project_dir = Path(project_dir).resolve()
    script_path = project_dir / "script" / "script.txt"
    if not script_path.is_file():
        raise PerceptionValidationError(f"Perception script input is missing: {script_path}")
    script_bytes = script_path.read_bytes()
    if not script_bytes.strip():
        raise PerceptionValidationError(f"Perception script input is empty: {script_path}")

    sources: list[dict[str, Any]] = []
    for item in media:
        if not item.get("has_video"):
            continue
        source = str(item.get("path") or item.get("source") or "")
        normalized, absolute = _inside_project(project_dir, source)
        if not absolute.is_file():
            raise PerceptionValidationError(f"Perception source is missing: {normalized}")
        try:
            duration = round(float(item.get("duration")), 3)
        except (TypeError, ValueError) as exc:
            raise PerceptionValidationError(
                f"Perception source duration is invalid: {normalized}"
            ) from exc
        group = str(item.get("group") or Path(normalized).parts[0])
        sources.append(
            {
                "source": normalized,
                "group": group,
                "duration": duration,
                "signature": source_signature(absolute),
            }
        )
    if not sources:
        raise PerceptionValidationError("Perception input contains no usable videos")
    sources.sort(key=lambda item: item["source"])
    identity = {
        "algorithm": PERCEPTION_INPUT_SIGNATURE_ALGORITHM,
        "script": {
            "path": "script/script.txt",
            "size_bytes": len(script_bytes),
            "sha256": hashlib.sha256(script_bytes).hexdigest(),
        },
        "sources": sources,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {**identity, "digest_sha256": digest}


def _inside_project(project_dir: Path, relative: str) -> tuple[str, Path]:
    normalized = Path(relative.replace("\\", "/"))
    resolved = (project_dir / normalized).resolve()
    try:
        resolved.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise PerceptionValidationError(
            f"Perception source escapes the project directory: {relative}"
        ) from exc
    return normalized.as_posix(), resolved


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PerceptionValidationError(f"{field} must be numeric") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise PerceptionValidationError(f"{field} must be finite")
    return result


def _score(value: Any, field: str, default: float) -> float:
    result = _number(default if value is None else value, field)
    if not 0.0 <= result <= 1.0:
        raise PerceptionValidationError(f"{field} must be between 0 and 1")
    return result


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PerceptionValidationError(f"{field} must be a list")
    output: list[str] = []
    for item in value:
        text = re.sub(r"\s+", "_", str(item).strip().casefold())
        if text and text not in output:
            output.append(text)
    return output


def validate_perception(
    payload: dict[str, Any],
    project_dir: Path,
    media: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PerceptionValidationError("perception.json must contain a JSON object")
    if int(payload.get("schema_version", 0)) != 1:
        raise PerceptionValidationError("Unsupported perception schema_version")
    if payload.get("status") != "done":
        raise PerceptionValidationError("perception.json status must be 'done'")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise PerceptionValidationError("perception.json must contain non-empty sources")

    perception_config = config.get("perception", {})
    verify_signature = bool(perception_config.get("verify_source_signature", True))
    duration_tolerance = float(perception_config.get("duration_tolerance_seconds", 1.0))
    media_by_path = {str(item["path"]): item for item in media if item.get("has_video")}
    require_input_signature = bool(
        perception_config.get("require_input_signature", True)
    )
    stated_input_signature = payload.get("input_signature")
    current_input_signature = perception_input_signature(project_dir, media)
    if require_input_signature:
        if not isinstance(stated_input_signature, dict):
            raise PerceptionValidationError(
                "perception.json input_signature is missing or invalid"
            )
        if stated_input_signature.get("algorithm") != PERCEPTION_INPUT_SIGNATURE_ALGORITHM:
            raise PerceptionValidationError("Unsupported Perception input signature algorithm")
        if stated_input_signature.get("digest_sha256") != current_input_signature["digest_sha256"]:
            raise PerceptionValidationError(
                "Perception input signature does not match the current project inputs"
            )
    normalized_sources: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_ids: set[str] = set()

    for source_index, source in enumerate(raw_sources):
        if not isinstance(source, dict):
            raise PerceptionValidationError(f"sources[{source_index}] must be an object")
        source_path, absolute = _inside_project(project_dir, str(source.get("source", "")))
        if source_path not in media_by_path:
            raise PerceptionValidationError(
                f"Perception source is not a discovered project video: {source_path}"
            )
        if source_path in seen_sources:
            raise PerceptionValidationError(f"Duplicate perception source: {source_path}")
        seen_sources.add(source_path)
        if not absolute.is_file():
            raise PerceptionValidationError(f"Perception source is missing: {source_path}")

        media_duration = float(media_by_path[source_path].get("duration", 0.0))
        if "duration" not in source:
            raise PerceptionValidationError(f"Missing source duration for {source_path}")
        stated_duration = _number(source.get("duration"), f"{source_path}.duration")
        if abs(stated_duration - media_duration) > duration_tolerance:
            raise PerceptionValidationError(
                f"Perception duration mismatch for {source_path}: "
                f"{stated_duration:.3f}s vs {media_duration:.3f}s"
            )

        expected_signature = source.get("signature") or {}
        if not isinstance(expected_signature, dict) or not {
            "size_bytes",
            "mtime_ns",
            "sample_sha256",
        }.issubset(expected_signature):
            raise PerceptionValidationError(
                f"Missing or incomplete source signature for {source_path}"
            )
        actual_signature: dict[str, Any] | None = None
        if verify_signature:
            actual_signature = source_signature(absolute)
            expected_size = expected_signature.get("size_bytes")
            expected_sample = expected_signature.get("sample_sha256")
            if expected_size is not None and int(expected_size) != actual_signature["size_bytes"]:
                raise PerceptionValidationError(f"Source size changed: {source_path}")
            if expected_sample and str(expected_sample) != actual_signature["sample_sha256"]:
                raise PerceptionValidationError(f"Source content changed: {source_path}")

        raw_segments = source.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise PerceptionValidationError(f"No perception segments for {source_path}")
        normalized_segments: list[dict[str, Any]] = []
        previous_start = -1.0
        for segment_index, segment in enumerate(raw_segments):
            if not isinstance(segment, dict):
                raise PerceptionValidationError(
                    f"{source_path}.segments[{segment_index}] must be an object"
                )
            required_segment_fields = {
                "id",
                "start",
                "end",
                "safe_start",
                "safe_end",
                "summary",
                "semantic_tags",
                "subjects",
                "objects",
                "actions",
                "script_alignment",
                "quality",
                "confidence",
                "visual_fingerprint",
            }
            missing_fields = sorted(required_segment_fields - set(segment))
            if missing_fields:
                raise PerceptionValidationError(
                    f"Incomplete perception segment {source_path}[{segment_index}]: "
                    + ", ".join(missing_fields)
                )
            segment_id = str(segment.get("id") or "").strip()
            if not segment_id:
                raise PerceptionValidationError(
                    f"Perception segment id is empty for {source_path}[{segment_index}]"
                )
            if segment_id in seen_ids:
                raise PerceptionValidationError(f"Duplicate perception segment id: {segment_id}")
            seen_ids.add(segment_id)
            start = _number(segment.get("start"), f"{segment_id}.start")
            end = _number(segment.get("end"), f"{segment_id}.end")
            safe_start = _number(segment.get("safe_start", start), f"{segment_id}.safe_start")
            safe_end = _number(segment.get("safe_end", end), f"{segment_id}.safe_end")
            if start < 0 or end <= start or end > media_duration + duration_tolerance:
                raise PerceptionValidationError(f"Invalid time range for {segment_id}")
            if safe_start < start or safe_end > end or safe_end - safe_start < 0.08:
                raise PerceptionValidationError(f"Invalid safe range for {segment_id}")
            if start + 0.001 < previous_start:
                raise PerceptionValidationError(
                    f"Segments are not sorted by start time for {source_path}"
                )
            previous_start = start

            quality = segment.get("quality") or {}
            if not isinstance(quality, dict):
                raise PerceptionValidationError(f"{segment_id}.quality must be an object")
            if not {"usable", "score", "issues"}.issubset(quality):
                raise PerceptionValidationError(
                    f"{segment_id}.quality is incomplete"
                )
            summary = str(segment.get("summary", "")).strip()
            if not summary:
                raise PerceptionValidationError(f"{segment_id}.summary must not be empty")
            script_alignment = segment.get("script_alignment")
            if not isinstance(script_alignment, list):
                raise PerceptionValidationError(
                    f"{segment_id}.script_alignment must be a list"
                )
            visual_fingerprint = str(segment.get("visual_fingerprint") or "").strip()
            if not visual_fingerprint:
                raise PerceptionValidationError(
                    f"{segment_id}.visual_fingerprint must not be empty"
                )
            normalized_segments.append(
                {
                    "id": segment_id,
                    **(
                        {
                            "provider_segment_id": str(
                                segment.get("provider_segment_id")
                            ).strip()
                        }
                        if str(segment.get("provider_segment_id") or "").strip()
                        else {}
                    ),
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "safe_start": round(safe_start, 3),
                    "safe_end": round(safe_end, 3),
                    "summary": summary,
                    "semantic_tags": _strings(
                        segment.get("semantic_tags"), f"{segment_id}.semantic_tags"
                    ),
                    "subjects": _strings(segment.get("subjects"), f"{segment_id}.subjects"),
                    "objects": _strings(segment.get("objects"), f"{segment_id}.objects"),
                    "actions": _strings(segment.get("actions"), f"{segment_id}.actions"),
                    "script_alignment": script_alignment,
                    "quality": {
                        "usable": bool(quality.get("usable", True)),
                        "score": _score(quality.get("score"), f"{segment_id}.quality.score", 0.5),
                        "issues": _strings(quality.get("issues"), f"{segment_id}.quality.issues"),
                    },
                    "confidence": _score(
                        segment.get("confidence"), f"{segment_id}.confidence", 0.5
                    ),
                    "visual_fingerprint": visual_fingerprint,
                }
            )

        normalized_sources.append(
            {
                "source": source_path,
                "duration": round(media_duration, 3),
                "signature": actual_signature or expected_signature,
                "segments": normalized_segments,
            }
        )

    if bool(perception_config.get("require_all_sources", True)):
        missing_sources = sorted(set(media_by_path) - seen_sources)
        if missing_sources:
            raise PerceptionValidationError(
                "Perception result does not cover current project videos: "
                + ", ".join(missing_sources)
            )

    provider = payload.get("provider") or {}
    if not isinstance(provider, dict):
        raise PerceptionValidationError("perception.json provider must be an object")
    provider_name = str(provider.get("name") or "").strip()
    provider_model = str(provider.get("model") or "").strip()
    if not provider_name or not provider_model:
        raise PerceptionValidationError(
            "perception.json provider name/model must not be empty"
        )
    return {
        "schema_version": 1,
        "status": "done",
        "input_signature": current_input_signature,
        "provider": {
            "name": provider_name,
            "model": provider_model,
        },
        "sources": normalized_sources,
    }


def load_project_perception(
    project_dir: Path,
    media: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    perception_config = config.get("perception", {})
    if not perception_config.get("enabled", True):
        return None, []
    relative = str(perception_config.get("path", "perception/perception.json"))
    normalized, path = _inside_project(project_dir, relative)
    if not path.is_file():
        if perception_config.get("required", False):
            raise PerceptionValidationError(f"Required perception file not found: {normalized}")
        return None, [
            "No validated perception.json was found; using metadata-only fallback planning."
        ]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return validate_perception(payload, project_dir, media, config), []
    except (OSError, json.JSONDecodeError, PerceptionValidationError) as exc:
        if str(perception_config.get("on_invalid", "fail")).casefold() == "fallback":
            return None, [f"Ignored invalid perception file and used fallback planning: {exc}"]
        if isinstance(exc, PerceptionValidationError):
            raise
        raise PerceptionValidationError(f"Could not read perception file: {exc}") from exc


def perception_index(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    return {str(source["source"]): source for source in payload.get("sources", [])}
