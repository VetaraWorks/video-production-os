from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def resolve_executable(explicit: str | None, default_name: str) -> str:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        raise FileNotFoundError(f"Executable not found: {explicit}")
    resolved = shutil.which(default_name)
    if not resolved:
        raise FileNotFoundError(
            f"{default_name} was not found on PATH. Install FFmpeg or pass an explicit path."
        )
    return resolved


def _frame_rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    try:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_media(path: Path, ffprobe: str) -> dict[str, Any]:
    if Path(ffprobe).stem.casefold() == "ffmpeg":
        return _probe_media_with_ffmpeg(path, ffprobe)
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"ffprobe failed for {path}: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}") from exc

    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    duration_text = payload.get("format", {}).get("duration")
    try:
        duration = float(duration_text)
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "duration": round(duration, 3),
        "format": payload.get("format", {}).get("format_name"),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "fps": round(_frame_rate(video.get("r_frame_rate")) if video else 0.0, 3),
    }


def _probe_media_with_ffmpeg(path: Path, ffmpeg: str) -> dict[str, Any]:
    """Fallback metadata probe for FFmpeg builds that do not ship ffprobe."""
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(path),
            "-t",
            "0.001",
            "-f",
            "null",
            "NUL",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    detail = completed.stderr or completed.stdout
    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", detail
    )
    duration = 0.0
    if duration_match:
        duration = (
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + float(duration_match.group(3))
        )
    video_line = next(
        (line for line in detail.splitlines() if "Video:" in line and "Stream #" in line),
        "",
    )
    audio_line = next(
        (line for line in detail.splitlines() if "Audio:" in line and "Stream #" in line),
        "",
    )
    video_codec_match = re.search(r"Video:\s*([^,\s]+)", video_line)
    audio_codec_match = re.search(r"Audio:\s*([^,\s]+)", audio_line)
    resolution_match = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", video_line)
    fps_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s+fps\b", video_line)
    format_match = re.search(r"Input #\d+,\s*([^,\r\n]+(?:,[^,\r\n]+)*)?,\s+from", detail)
    if not video_line and not audio_line:
        raise RuntimeError(f"ffmpeg metadata probe failed for {path}: {detail[-1200:]}")
    return {
        "duration": round(duration, 3),
        "format": format_match.group(1).strip() if format_match else None,
        "has_video": bool(video_line),
        "has_audio": bool(audio_line),
        "video_codec": video_codec_match.group(1) if video_codec_match else None,
        "audio_codec": audio_codec_match.group(1) if audio_codec_match else None,
        "width": int(resolution_match.group(1)) if resolution_match else 0,
        "height": int(resolution_match.group(2)) if resolution_match else 0,
        "fps": round(float(fps_match.group(1)), 3) if fps_match else 0.0,
    }


def discover_files(folder: Path, extensions: set[str]) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in extensions
        ),
        key=lambda item: item.as_posix().casefold(),
    )
