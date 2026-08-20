#!/usr/bin/env python3
"""Add one absolute-time SFX stem to a decrypted Jianying draft copy.

The script deliberately preserves every existing material, segment, and track.
It only appends one local audio material, one speed material, and one audio
track.  It can also rename the duplicated draft in its decrypted meta JSON.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path


TRACK_NAME = "06-Codex对齐音效"


def new_id() -> str:
    return uuid.uuid4().hex


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def inject_content(content: dict, media_path: Path, duration_us: int, volume: float) -> dict:
    tracks = content.setdefault("tracks", [])
    materials = content.setdefault("materials", {})
    if any(track.get("name") == TRACK_NAME for track in tracks):
        raise RuntimeError(f"draft already contains track: {TRACK_NAME}")

    material_id = new_id()
    speed_id = new_id()
    segment_id = new_id()
    track_id = new_id()
    resolved_media = media_path.resolve()

    audio_material = {
        "app_id": 0,
        "category_id": "",
        "category_name": "local",
        "check_flag": 3,
        "copyright_limit_type": "none",
        "duration": duration_us,
        "effect_id": "",
        "formula_id": "",
        "id": material_id,
        "local_material_id": material_id,
        "music_id": material_id,
        "name": resolved_media.name,
        "path": str(resolved_media),
        "source_platform": 0,
        "type": "extract_music",
        "wave_points": [],
    }
    speed_material = {
        "curve_speed": None,
        "id": speed_id,
        "mode": 0,
        "speed": 1.0,
        "type": "speed",
    }
    segment = {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": volume,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "visible": True,
        "id": segment_id,
        "material_id": material_id,
        "target_timerange": {"start": 0, "duration": duration_us},
        "common_keyframes": [],
        "keyframe_refs": [],
        "source_timerange": {"start": 0, "duration": duration_us},
        "speed": 1.0,
        "volume": volume,
        "extra_material_refs": [speed_id],
        "is_tone_modify": False,
        "clip": None,
        "hdr_settings": None,
        "render_index": len(tracks),
    }
    track = {
        "attribute": 0,
        "flag": 0,
        "id": track_id,
        "is_default_name": False,
        "name": TRACK_NAME,
        "segments": [segment],
        "type": "audio",
    }

    materials.setdefault("audios", []).append(audio_material)
    materials.setdefault("speeds", []).append(speed_material)
    tracks.append(track)
    return {
        "track_name": TRACK_NAME,
        "track_id": track_id,
        "segment_id": segment_id,
        "material_id": material_id,
        "duration_us": duration_us,
        "volume": volume,
        "path": str(resolved_media),
    }


def update_meta(meta: dict, draft_name: str, draft_folder: Path, media_name: str, duration_us: int) -> dict:
    meta["draft_name"] = draft_name
    meta["draft_fold_path"] = str(draft_folder.resolve()).replace("\\", "/")
    meta["draft_id"] = str(uuid.uuid4()).upper()
    meta["draft_need_rename_folder"] = False
    now_us = int(time.time() * 1_000_000)
    meta["tm_draft_modified"] = now_us
    meta["tm_draft_create"] = now_us
    meta["tm_duration"] = max(int(meta.get("tm_duration", 0)), duration_us)

    local_group = None
    for group in meta.setdefault("draft_materials", []):
        if group.get("type") == 0:
            local_group = group
            break
    if local_group is None:
        local_group = {"type": 0, "value": []}
        meta["draft_materials"].append(local_group)
    local_group.setdefault("value", []).append(
        {
            "ai_group_type": "",
            "create_time": -1,
            "duration": duration_us,
            "enter_from": 0,
            "extra_info": media_name,
            "file_Path": f"./Media/{media_name}",
            "height": 0,
            "id": new_id(),
            "import_time": -1,
            "import_time_ms": -1,
            "item_source": 1,
            "material_color_tag": "",
            "md5": "",
            "metetype": "music",
            "roughcut_time_range": {"duration": duration_us, "start": 0},
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0,
            "width": 0,
        }
    )
    return {"draft_id": meta["draft_id"], "draft_name": draft_name}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-in", type=Path, required=True)
    parser.add_argument("--content-out", type=Path, required=True)
    parser.add_argument("--meta-in", type=Path, required=True)
    parser.add_argument("--meta-out", type=Path, required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--draft-name", required=True)
    parser.add_argument("--draft-folder", type=Path, required=True)
    parser.add_argument("--duration-us", type=int, required=True)
    parser.add_argument("--volume", type=float, default=0.82)
    args = parser.parse_args()

    if not 0.0 < args.volume <= 2.0:
        raise ValueError("volume must be in (0, 2]")
    if not args.media.is_file():
        raise FileNotFoundError(args.media)

    content = load_json(args.content_in)
    meta = load_json(args.meta_in)
    content_result = inject_content(content, args.media, args.duration_us, args.volume)
    meta_result = update_meta(
        meta,
        args.draft_name,
        args.draft_folder,
        args.media.name,
        args.duration_us,
    )
    save_json(args.content_out, content)
    save_json(args.meta_out, meta)
    print(json.dumps({"content": content_result, "meta": meta_result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
