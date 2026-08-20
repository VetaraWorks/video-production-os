from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ASS_TAG_RE = re.compile(r"\{[^}]*\}")
ASS_POS_RE = re.compile(r"\\pos\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)")
ASS_ROT_RE = re.compile(r"\\frz(-?[\d.]+)")


@dataclass(frozen=True)
class TextCue:
    start: float
    end: float
    text: str
    style: str = "Subtitle"
    x: float | None = None
    y: float | None = None
    rotation: float = 0.0


def _load_draft_module() -> Any:
    scripts_dir = Path(__file__).resolve().parents[1]
    vendor_dir = scripts_dir / "vendor"
    candidates = [
        os.environ.get("VIDEO_OS_PY_DEPS"),
        os.environ.get("CODEX_VIDEO_PY_DEPS"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            sys.path.insert(0, str(Path(candidate)))
    sys.path.insert(0, str(vendor_dir))
    try:
        import pyJianYingDraft as draft
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Jianying draft export dependencies are missing. Install pymediainfo "
            "and imageio, or set VIDEO_OS_PY_DEPS to their target directory "
            "(legacy CODEX_VIDEO_PY_DEPS is also accepted)."
        ) from exc
    return draft


def _ass_time(value: str) -> float:
    hour, minute, second = value.strip().split(":")
    return int(hour) * 3600 + int(minute) * 60 + float(second)


def _srt_time(value: str) -> float:
    hour, minute, rest = value.strip().split(":")
    second, millis = rest.split(",")
    return int(hour) * 3600 + int(minute) * 60 + int(second) + int(millis) / 1000


def parse_ass(path: Path, *, time_offset: float = 0.0) -> list[TextCue]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    play_width = 1080.0
    play_height = 1920.0
    styles: dict[str, tuple[float, float]] = {}
    for line in lines:
        if line.startswith("PlayResX:"):
            play_width = float(line.split(":", 1)[1].strip())
        elif line.startswith("PlayResY:"):
            play_height = float(line.split(":", 1)[1].strip())
        elif line.startswith("Style:"):
            values = line[len("Style:") :].lstrip().split(",")
            if len(values) < 22:
                continue
            alignment = int(values[18])
            margin_l = float(values[19])
            margin_r = float(values[20])
            margin_v = float(values[21])
            if alignment in (1, 4, 7):
                x = margin_l
            elif alignment in (3, 6, 9):
                x = play_width - margin_r
            else:
                x = play_width / 2
            if alignment in (7, 8, 9):
                y = margin_v
            elif alignment in (4, 5, 6):
                y = play_height / 2
            else:
                y = play_height - margin_v
            styles[values[0].strip()] = (x, y)
    cues: list[TextCue] = []
    for line in lines:
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:") :].lstrip().split(",", 9)
        if len(fields) != 10:
            continue
        raw_text = fields[9]
        pos_match = ASS_POS_RE.search(raw_text)
        rot_match = ASS_ROT_RE.search(raw_text)
        text = ASS_TAG_RE.sub("", raw_text)
        text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
        start = _ass_time(fields[1]) + time_offset
        end = _ass_time(fields[2]) + time_offset
        if end <= start or not text.strip():
            continue
        style_name = fields[3].strip() or "Subtitle"
        style_position = styles.get(style_name)
        cues.append(
            TextCue(
                start=start,
                end=end,
                text=text.strip(),
                style=style_name,
                x=float(pos_match.group(1)) if pos_match else (style_position[0] if style_position else None),
                y=float(pos_match.group(2)) if pos_match else (style_position[1] if style_position else None),
                rotation=float(rot_match.group(1)) if rot_match else 0.0,
            )
        )
    return sorted(cues, key=lambda cue: (cue.start, cue.end, cue.style))


def parse_srt(path: Path, *, time_offset: float = 0.0) -> list[TextCue]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues: list[TextCue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if " --> " in line), None)
        if timing_index is None:
            continue
        start_text, end_text = lines[timing_index].split(" --> ", 1)
        start = _srt_time(start_text) + time_offset
        end = _srt_time(end_text.split()[0]) + time_offset
        cue_text = "\n".join(lines[timing_index + 1 :]).strip()
        if cue_text and end > start:
            cues.append(TextCue(start=start, end=end, text=cue_text))
    return sorted(cues, key=lambda cue: (cue.start, cue.end))


def _safe_name(value: str) -> str:
    clean = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    return clean or "Codex剪辑工程"


def _resolve_path(
    value: str | Path,
    *,
    project_dir: Path,
    output_dir: Path,
    skill_dir: Path,
) -> Path:
    path = Path(value)
    if path.is_absolute() and path.is_file():
        return path.resolve()
    for root in (project_dir, output_dir, skill_dir, skill_dir / "assets"):
        candidate = (root / path).resolve()
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Jianying export asset not found: {value}")


def _copy_portable_asset(source: Path, media_dir: Path) -> Path:
    source = source.resolve()
    try:
        source.relative_to(media_dir.resolve())
        return source
    except ValueError:
        pass
    digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
    target = media_dir / f"{digest}_{source.name}"
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copy2(source, target)
    return target.resolve()


def _rgb_from_style(style: str) -> tuple[float, float, float]:
    lowered = style.lower()
    if "red" in lowered:
        return (1.0, 0.29, 0.24)
    if "yellow" in lowered:
        return (1.0, 0.90, 0.43)
    return (1.0, 1.0, 1.0)


def _add_text_cues(
    draft: Any,
    script: Any,
    cues: Iterable[TextCue],
    *,
    canvas_width: int,
    canvas_height: int,
    normal_track: str,
    flower_track: str,
) -> tuple[int, int]:
    normal_count = 0
    flower_count = 0
    lane_ends: dict[str, float] = {normal_track: -1.0, flower_track: -1.0}
    lane_names: dict[str, list[str]] = {
        "normal": [normal_track],
        "flower": [flower_track],
    }

    def select_lane(kind: str, cue_start: float) -> str:
        for name in lane_names[kind]:
            if lane_ends[name] <= cue_start + 0.0005:
                return name
        base_name = normal_track if kind == "normal" else flower_track
        name = f"{base_name}-{len(lane_names[kind]) + 1}"
        script.append_track(draft.TrackSpec(draft.TrackType.text, name))
        lane_names[kind].append(name)
        lane_ends[name] = -1.0
        return name

    for cue in cues:
        is_flower = cue.style.lower() in {
            "flower",
            "alert",
            "emphasis",
            "yellow",
            "red",
        }
        if is_flower:
            x = cue.x if cue.x is not None else canvas_width / 2
            y = cue.y if cue.y is not None else canvas_height * 0.18
            longest_line = max((len(line.replace(" ", "")) for line in cue.text.splitlines()), default=1)
            estimated_half_width = min(
                canvas_width * 0.36,
                max(canvas_width * 0.12, longest_line * 42.0),
            )
            safe_edge = canvas_width * 0.055
            x = max(safe_edge + estimated_half_width, min(canvas_width - safe_edge - estimated_half_width, x))
            transform_x = (x - canvas_width / 2) / (canvas_width / 2)
            transform_y = 1.0 - 2.0 * y / canvas_height
            style = draft.TextStyle(
                size=11.0,
                bold=True,
                color=_rgb_from_style(cue.style),
                align=1,
                auto_wrapping=True,
                max_line_width=0.68,
            )
            clip = draft.ClipSettings(
                transform_x=max(-0.82, min(0.82, transform_x)),
                transform_y=max(-0.86, min(0.86, transform_y)),
                rotation=cue.rotation,
            )
            border = draft.TextBorder(color=(0.04, 0.04, 0.04), width=48.0)
            shadow = draft.TextShadow(alpha=0.75, diffuse=14.0, distance=4.0)
            track = select_lane("flower", cue.start)
            flower_count += 1
        else:
            style = draft.TextStyle(
                size=7.4,
                bold=True,
                color=(1.0, 1.0, 1.0),
                align=1,
                auto_wrapping=True,
                max_line_width=0.78,
            )
            clip = draft.ClipSettings(transform_y=-0.55)
            border = draft.TextBorder(color=(0.03, 0.03, 0.03), width=42.0)
            shadow = draft.TextShadow(alpha=0.55, diffuse=10.0, distance=3.0)
            track = select_lane("normal", cue.start)
            normal_count += 1
        segment = draft.TextSegment(
            cue.text,
            draft.trange(f"{cue.start:.6f}s", f"{cue.end - cue.start:.6f}s"),
            style=style,
            clip_settings=clip,
            border=border,
            shadow=shadow,
        )
        script.add_segment(segment, track)
        lane_ends[track] = cue.end
    return normal_count, flower_count


def _update_meta(draft_dir: Path, draft_name: str, duration_us: int, draft_root: Path) -> None:
    meta_path = draft_dir / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    draft_id = str(uuid.uuid4()).upper()
    meta.update(
        {
            "draft_id": draft_id,
            "draft_name": draft_name,
            "draft_fold_path": str(draft_dir.resolve()),
            "draft_root_path": str(draft_root.resolve()),
            "tm_duration": duration_us,
            "tm_draft_cloud_modified": int(time.time()),
        }
    )
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _zip_directory(source_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir.parent))


def export_jianying_draft(
    plan: dict[str, Any],
    *,
    project_dir: Path,
    output_dir: Path,
    draft_root: Path,
    draft_name: str,
    portable_media: bool = True,
    zip_path: Path | None = None,
) -> dict[str, Any]:
    """Export a native, editable Jianying draft from a standard or fullscreen plan."""
    draft = _load_draft_module()
    project_dir = project_dir.resolve()
    output_dir = output_dir.resolve()
    draft_root = draft_root.resolve()
    draft_root.mkdir(parents=True, exist_ok=True)
    draft_name = _safe_name(draft_name)
    skill_dir = Path(__file__).resolve().parents[2]

    canvas = plan.get("canvas", {})
    width = int(canvas.get("width", 1080))
    height = int(canvas.get("height", 1920))
    fps = int(canvas.get("fps", 30))
    folder = draft.DraftFolder(str(draft_root))
    script = folder.create_draft(
        draft_name,
        width,
        height,
        fps,
        maintrack_adsorb=True,
        allow_replace=True,
    )
    script.content["id"] = str(uuid.uuid4()).upper()
    script.content["create_time"] = int(time.time())
    draft_dir = draft_root / draft_name
    media_dir = draft_dir / "Media"
    media_dir.mkdir(parents=True, exist_ok=True)

    track_names = {
        "base": "01-主口播",
        "broll": "02-补充画面",
        "sfx": "03-音效",
        "subtitle": "04-普通字幕",
        "flower": "05-重点花字",
        "bgm": "06-BGM",
    }
    script.append_tracks(
        [
            draft.TrackSpec(draft.TrackType.video, track_names["base"]),
            draft.TrackSpec(draft.TrackType.video, track_names["broll"]),
            draft.TrackSpec(draft.TrackType.audio, track_names["sfx"]),
            draft.TrackSpec(draft.TrackType.text, track_names["subtitle"]),
            draft.TrackSpec(draft.TrackType.text, track_names["flower"]),
            draft.TrackSpec(draft.TrackType.audio, track_names["bgm"]),
        ]
    )

    copied: dict[Path, Path] = {}

    def media_path(value: str | Path) -> Path:
        source = _resolve_path(
            value,
            project_dir=project_dir,
            output_dir=output_dir,
            skill_dir=skill_dir,
        )
        if not portable_media:
            return source
        if source not in copied:
            copied[source] = _copy_portable_asset(source, media_dir)
        return copied[source]

    video_materials: dict[Path, Any] = {}
    audio_materials: dict[Path, Any] = {}

    def video_material(path: Path) -> Any:
        if path not in video_materials:
            video_materials[path] = draft.VideoMaterial(str(path))
        return video_materials[path]

    def audio_material(path: Path) -> Any:
        if path not in audio_materials:
            audio_materials[path] = draft.AudioMaterial(str(path))
        return audio_materials[path]

    text_cues: list[TextCue] = []
    segment_count = 0
    fullscreen_mode = "fullscreen_events" in plan and "base_video" in plan
    if fullscreen_mode:
        main_duration = float(plan["main_duration"])
        base_path = media_path(str(plan["base_video"]))
        base_segment = draft.VideoSegment(
            video_material(base_path),
            draft.trange("0s", f"{main_duration:.6f}s"),
            source_timerange=draft.trange("0s", f"{main_duration:.6f}s"),
            volume=1.0,
        )
        script.add_segment(base_segment, track_names["base"])
        segment_count += 1

        broll_path = media_path(str(plan["broll_video"]))
        broll_material = video_material(broll_path)
        for event in sorted(plan.get("fullscreen_events", []), key=lambda item: float(item["timeline_start"])):
            start = float(event["timeline_start"])
            duration = float(event["duration"])
            source_start = float(event["source_start"])
            segment = draft.VideoSegment(
                broll_material,
                draft.trange(f"{start:.6f}s", f"{duration:.6f}s"),
                source_timerange=draft.trange(f"{source_start:.6f}s", f"{duration:.6f}s"),
                volume=0.0,
            )
            script.add_segment(segment, track_names["broll"])
            segment_count += 1

        cover_duration = float(plan.get("cover_duration", 0.0))
        if cover_duration > 0 and plan.get("cover_image"):
            cover_path = media_path(str(plan["cover_image"]))
            cover_segment = draft.VideoSegment(
                video_material(cover_path),
                draft.trange(f"{main_duration:.6f}s", f"{cover_duration:.6f}s"),
                volume=0.0,
            )
            script.add_segment(cover_segment, track_names["base"])
            segment_count += 1

        subtitle_path_text = str(
            plan.get("subtitle_file")
            or plan.get("subtitles", {}).get("filename", "subtitles.ass")
        )
        subtitle_path = _resolve_path(
            subtitle_path_text,
            project_dir=project_dir,
            output_dir=output_dir,
            skill_dir=skill_dir,
        )
        text_cues.extend(parse_ass(subtitle_path))
        if cover_duration > 0 and plan.get("cover_subtitles"):
            cover_text_path = _resolve_path(
                str(plan["cover_subtitles"]),
                project_dir=project_dir,
                output_dir=output_dir,
                skill_dir=skill_dir,
            )
            text_cues.extend(parse_ass(cover_text_path, time_offset=main_duration))
        sfx_events = [
            {
                "at": event.get("at", event.get("start", 0.0)),
                "asset": event["asset"],
                "volume": event.get("volume", 0.16),
            }
            for event in plan.get("sound_effects", [])
        ]
    else:
        for item in sorted(plan.get("segments", []), key=lambda value: float(value["timeline_start"])):
            start = float(item["timeline_start"])
            duration = float(item["duration"])
            source_start = float(item.get("source_start", 0.0))
            source_path = media_path(str(item["source"]))
            material = video_material(source_path)
            if item.get("loop"):
                source_duration = min(float(item.get("source_duration", 0.0)), material.duration / draft.SEC)
                if source_duration <= 0:
                    raise ValueError(f"Cannot loop zero-length source: {source_path}")
                cursor = start
                remaining = duration
                while remaining > 0.0005:
                    slice_duration = min(source_duration, remaining)
                    segment = draft.VideoSegment(
                        material,
                        draft.trange(f"{cursor:.6f}s", f"{slice_duration:.6f}s"),
                        source_timerange=draft.trange("0s", f"{slice_duration:.6f}s"),
                        volume=1.0 if item.get("has_audio") else 0.0,
                    )
                    script.add_segment(segment, track_names["base"])
                    segment_count += 1
                    cursor += slice_duration
                    remaining -= slice_duration
            else:
                segment = draft.VideoSegment(
                    material,
                    draft.trange(f"{start:.6f}s", f"{duration:.6f}s"),
                    source_timerange=draft.trange(f"{source_start:.6f}s", f"{duration:.6f}s"),
                    volume=1.0 if item.get("has_audio") else 0.0,
                )
                script.add_segment(segment, track_names["base"])
                segment_count += 1
        subtitle_info = plan.get("subtitles", {})
        if subtitle_info.get("enabled", True):
            subtitle_name = subtitle_info.get("filename") or subtitle_info.get("srt_filename")
            if subtitle_name:
                subtitle_path = _resolve_path(
                    str(subtitle_name),
                    project_dir=project_dir,
                    output_dir=output_dir,
                    skill_dir=skill_dir,
                )
                text_cues.extend(
                    parse_ass(subtitle_path)
                    if subtitle_path.suffix.lower() == ".ass"
                    else parse_srt(subtitle_path)
                )
        sfx_events = plan.get("sound_effects", {}).get("events", [])

    for event in sorted(sfx_events, key=lambda item: float(item.get("at", 0.0))):
        asset_path = media_path(str(event["asset"]))
        material = audio_material(asset_path)
        start = float(event.get("at", 0.0))
        duration = material.duration / draft.SEC
        segment = draft.AudioSegment(
            material,
            draft.trange(f"{start:.6f}s", f"{duration:.6f}s"),
            volume=float(event.get("volume", 0.16)),
        )
        script.add_segment(segment, track_names["sfx"])

    bgm_info = plan.get("bgm", {})
    if bgm_info.get("enabled") and bgm_info.get("path"):
        bgm_path = media_path(str(bgm_info["path"]))
        material = audio_material(bgm_path)
        total_duration = float(plan.get("duration_seconds", script.duration / draft.SEC))
        cursor = 0.0
        while cursor < total_duration - 0.001:
            duration = min(material.duration / draft.SEC, total_duration - cursor)
            segment = draft.AudioSegment(
                material,
                draft.trange(f"{cursor:.6f}s", f"{duration:.6f}s"),
                source_timerange=draft.trange("0s", f"{duration:.6f}s"),
                volume=float(bgm_info.get("volume", 0.1)),
            )
            script.add_segment(segment, track_names["bgm"])
            cursor += duration

    normal_count, flower_count = _add_text_cues(
        draft,
        script,
        sorted(text_cues, key=lambda cue: (cue.start, cue.end)),
        canvas_width=width,
        canvas_height=height,
        normal_track=track_names["subtitle"],
        flower_track=track_names["flower"],
    )
    script.save()
    _update_meta(draft_dir, draft_name, script.duration, draft_root)

    support_dir = draft_dir / "Codex"
    support_dir.mkdir(exist_ok=True)
    (support_dir / "edit_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "draft_name": draft_name,
        "draft_directory": str(draft_dir),
        "canvas": {"width": width, "height": height, "fps": fps},
        "duration_seconds": round(script.duration / draft.SEC, 3),
        "tracks": track_names,
        "video_segments": segment_count,
        "subtitle_cues": normal_count,
        "flower_text_cues": flower_count,
        "sound_effects": len(sfx_events),
        "portable_media": portable_media,
        "copied_media": [str(path) for path in sorted(copied.values())],
    }
    (support_dir / "jianying_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (draft_dir / "打开说明.txt").write_text(
        "本目录是可编辑剪映专业版草稿。\n"
        f"当前草稿目录：{draft_dir.resolve()}。\n"
        "关闭并重新打开剪映，或在草稿页刷新，即可看到此工程。\n"
        "轨道已拆分为主口播、补充画面、音效、普通字幕、重点花字和 BGM。\n",
        encoding="utf-8-sig",
    )
    if zip_path is not None:
        _zip_directory(draft_dir, zip_path.resolve())
        manifest["zip_path"] = str(zip_path.resolve())
    return manifest
