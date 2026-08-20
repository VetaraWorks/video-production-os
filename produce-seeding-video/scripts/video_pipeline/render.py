from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _run(command: list[str], stage: str) -> None:
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
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise RuntimeError(
            f"{stage} failed with exit code {completed.returncode}: {detail}"
        )


def _project_file(project_dir: Path, relative_path: str) -> Path:
    candidate = (project_dir / relative_path).resolve()
    try:
        candidate.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Plan path escapes the project directory: {relative_path}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"Plan source does not exist: {candidate}")
    return candidate


def _video_filter(width: int, height: int, fps: float) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps:g},format=yuv420p,setsar=1"
    )


def _ass_color(value: str, fallback: str) -> str:
    text = str(value).strip().lstrip("#")
    if len(text) != 6 or any(character not in "0123456789abcdefABCDEF" for character in text):
        text = fallback.lstrip("#")
    red, green, blue = text[0:2], text[2:4], text[4:6]
    return f"&H00{blue}{green}{red}".upper()


def _subtitle_filter(subtitle_path: Path, subtitle_config: dict[str, Any]) -> str:
    escaped_path = (
        subtitle_path.resolve().as_posix().replace("\\", "/").replace(":", r"\:")
    )
    escaped_path = escaped_path.replace("'", r"\'")
    if subtitle_path.suffix.lower() == ".ass":
        return f"subtitles=filename='{escaped_path}'"

    font = str(subtitle_config.get("font", "Microsoft YaHei")).replace("'", "")
    font_size = int(subtitle_config.get("font_size", 64))
    margin_v = int(subtitle_config.get("margin_v", 480))
    outline = int(subtitle_config.get("outline", 5))
    shadow = int(subtitle_config.get("shadow", 1))
    primary = _ass_color(subtitle_config.get("primary_color", "#FFFFFF"), "#FFFFFF")
    outline_color = _ass_color(
        subtitle_config.get("outline_color", "#151515"), "#151515"
    )
    style = (
        f"FontName={font},FontSize={font_size},"
        f"PrimaryColour={primary},OutlineColour={outline_color},"
        f"BorderStyle=1,Outline={outline},Shadow={shadow},Alignment=2,"
        f"MarginV={margin_v}"
    )
    return f"subtitles=filename='{escaped_path}':force_style='{style}'"


def _skill_file(relative_path: str) -> Path:
    skill_root = Path(__file__).resolve().parents[2]
    candidates = [
        (skill_root / relative_path).resolve(),
        (skill_root / "assets" / relative_path).resolve(),
    ]
    for candidate in candidates:
        try:
            candidate.relative_to(skill_root.resolve())
        except ValueError as exc:
            raise ValueError(
                f"Sound-effect path escapes the skill directory: {relative_path}"
            ) from exc
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Sound-effect asset does not exist. Checked: "
        + ", ".join(str(path) for path in candidates)
    )


def _render_segment(
    ffmpeg: str,
    source: Path,
    target: Path,
    segment: dict[str, Any],
    canvas: dict[str, Any],
    encoder: dict[str, Any],
) -> None:
    duration = float(segment["duration"])
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if segment.get("loop"):
        command.extend(["-stream_loop", "-1"])
    command.extend(
        [
            "-ss",
            f"{float(segment.get('source_start', 0)):.3f}",
            "-i",
            str(source),
        ]
    )
    if not segment.get("has_audio"):
        command.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                "anullsrc=r=48000:cl=stereo",
            ]
        )

    command.extend(
        [
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0" if segment.get("has_audio") else "1:a:0",
            "-vf",
            _video_filter(
                int(canvas["width"]),
                int(canvas["height"]),
                float(canvas["fps"]),
            ),
        ]
    )
    if segment.get("has_audio"):
        command.extend(
            [
                "-af",
                f"aresample=48000,aformat=channel_layouts=stereo,"
                f"apad,atrim=duration={duration:.3f}",
            ]
        )
    command.extend(
        [
            "-c:v",
            str(encoder["video_codec"]),
            "-preset",
            str(encoder["preset"]),
            "-crf",
            str(encoder["crf"]),
            "-c:a",
            str(encoder["audio_codec"]),
            "-b:a",
            str(encoder["audio_bitrate"]),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-shortest",
            str(target),
        ]
    )
    _run(command, f"segment {segment['id']}")


def _concat_segments(ffmpeg: str, segment_files: list[Path], target: Path) -> None:
    list_path = target.with_suffix(".txt")
    lines = []
    for path in segment_files:
        escaped = path.resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(target),
    ]
    _run(command, "segment concatenation")


def _finish_video(
    ffmpeg: str,
    joined: Path,
    final_path: Path,
    plan: dict[str, Any],
    project_dir: Path,
    output_dir: Path,
    config: dict[str, Any],
) -> None:
    subtitle_enabled = bool(plan.get("subtitles", {}).get("enabled"))
    subtitle_path = output_dir / str(
        plan.get("subtitles", {}).get("filename", "subtitles.ass")
    )
    subtitle_filter = (
        _subtitle_filter(subtitle_path, config.get("subtitles", {}))
        if subtitle_enabled
        and subtitle_path.is_file()
        and subtitle_path.stat().st_size
        else None
    )
    bgm_path_text = plan.get("bgm", {}).get("path")
    bgm_path = (
        _project_file(project_dir, str(bgm_path_text)) if bgm_path_text else None
    )
    raw_sound_effects = plan.get("sound_effects", {})
    sound_events = list(
        raw_sound_effects.get("events", [])
        if isinstance(raw_sound_effects, dict)
        else raw_sound_effects
    )
    sound_paths = [_skill_file(str(event["asset"])) for event in sound_events]
    encoder = plan["render"]
    total_duration = float(plan["duration_seconds"])
    audio_config = plan.get("audio", config.get("audio", {}))

    if (
        not subtitle_filter
        and not bgm_path
        and not sound_events
        and not audio_config.get("normalize", True)
    ):
        shutil.copy2(joined, final_path)
        return

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(joined),
    ]
    next_input = 1
    bgm_input: int | None = None
    if bgm_path:
        command.extend(["-stream_loop", "-1", "-i", str(bgm_path)])
        bgm_input = next_input
        next_input += 1
    sfx_inputs: list[int] = []
    for path in sound_paths:
        command.extend(["-i", str(path)])
        sfx_inputs.append(next_input)
        next_input += 1

    video_chain = subtitle_filter or "null"
    filter_parts = [f"[0:v]{video_chain}[v]"]
    voice_volume = float(audio_config.get("voice_volume", 1.0))
    filter_parts.append(
        "[0:a]aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={voice_volume:g}[voicebase]"
    )

    mix_inputs: list[str] = []
    if bgm_input is not None:
        fade_duration = min(
            float(plan.get("bgm", {}).get("fade_seconds", 0.8)),
            max(0.1, total_duration / 4),
        )
        fade_out_start = max(0.0, total_duration - fade_duration)
        volume = float(plan.get("bgm", {}).get("volume", 0.1))
        filter_parts.append(
            f"[{bgm_input}:a]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={volume:g},atrim=duration={total_duration:.3f},"
            f"afade=t=in:st=0:d={fade_duration:.3f},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}[musicbase]"
        )
        ducking = plan.get("bgm", {}).get("ducking", {})
        if ducking.get("enabled", True):
            filter_parts.append("[voicebase]asplit=2[voice][voiceside]")
            filter_parts.append(
                "[musicbase][voiceside]sidechaincompress="
                f"threshold={float(ducking.get('threshold', 0.035)):g}:"
                f"ratio={float(ducking.get('ratio', 8)):g}:"
                f"attack={float(ducking.get('attack_ms', 18)):g}:"
                f"release={float(ducking.get('release_ms', 280)):g}[music]"
            )
            mix_inputs.extend(["[voice]", "[music]"])
        else:
            mix_inputs.extend(["[voicebase]", "[musicbase]"])
    else:
        mix_inputs.append("[voicebase]")

    for index, (event, input_index) in enumerate(zip(sound_events, sfx_inputs)):
        delay_ms = max(0, round(float(event["at"]) * 1000))
        volume = float(event.get("volume", 0.18))
        label = f"sfx{index}"
        filter_parts.append(
            f"[{input_index}:a]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={volume:g},adelay={delay_ms}|{delay_ms},"
            f"atrim=duration={total_duration:.3f}[{label}]"
        )
        mix_inputs.append(f"[{label}]")

    filter_parts.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:duration=first:"
        "dropout_transition=0:normalize=0,"
        f"atrim=duration={total_duration:.3f}[premaster]"
    )
    if audio_config.get("normalize", True):
        filter_parts.append(
            "[premaster]loudnorm="
            f"I={float(audio_config.get('target_lufs', -15.0)):g}:"
            f"TP={float(audio_config.get('true_peak_db', -1.5)):g}:"
            f"LRA={float(audio_config.get('lra', 9.0)):g}[a]"
        )
    else:
        filter_parts.append("[premaster]anull[a]")

    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[v]",
            "-map",
            "[a]",
        ]
    )

    command.extend(
        [
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            str(encoder["video_codec"]),
            "-preset",
            str(encoder["preset"]),
            "-crf",
            str(encoder["crf"]),
            "-c:a",
            str(encoder["audio_codec"]),
            "-b:a",
            str(encoder["audio_bitrate"]),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(final_path),
        ]
    )
    _run(command, "subtitle/audio finishing")


def _render_fullscreen_base(
    ffmpeg: str,
    target: Path,
    plan: dict[str, Any],
    project_dir: Path,
) -> None:
    """Render clean full-screen visual replacements over one continuous voice track."""
    base_source = _project_file(project_dir, str(plan["base_video"]))
    events = sorted(
        plan.get("fullscreen_events", []),
        key=lambda item: float(item["timeline_start"]),
    )
    canvas = plan["canvas"]
    encoder = plan["render"]
    total_duration = float(plan.get("main_duration", plan["duration_seconds"]))

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(base_source)]
    event_sources: list[Path] = []
    for event in events:
        source = _project_file(
            project_dir,
            str(event.get("source") or plan.get("broll_video")),
        )
        event_sources.append(source)
        command.extend(
            [
                "-ss",
                f"{float(event['source_start']):.3f}",
                "-t",
                f"{float(event['duration']):.3f}",
                "-i",
                str(source),
            ]
        )

    video_filter = _video_filter(
        int(canvas["width"]),
        int(canvas["height"]),
        float(canvas["fps"]),
    )
    filter_parts = [
        f"[0:v]{video_filter},trim=duration={total_duration:.3f},setpts=PTS-STARTPTS[basev]",
        f"[0:a]aresample=48000,aformat=channel_layouts=stereo,apad,"
        f"atrim=duration={total_duration:.3f},asetpts=PTS-STARTPTS[a]",
    ]
    current = "basev"
    for index, event in enumerate(events, start=1):
        start = float(event["timeline_start"])
        duration = float(event["duration"])
        end = start + duration
        filter_parts.append(
            f"[{index}:v]{video_filter},trim=duration={duration:.3f},"
            f"setpts=PTS-STARTPTS+{start:.3f}/TB[event{index}]"
        )
        output = f"visual{index}"
        filter_parts.append(
            f"[{current}][event{index}]overlay=eof_action=pass:shortest=0:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{output}]"
        )
        current = output

    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            f"[{current}]",
            "-map",
            "[a]",
            "-t",
            f"{total_duration:.3f}",
            "-c:v",
            str(encoder["video_codec"]),
            "-preset",
            str(encoder["preset"]),
            "-crf",
            str(encoder["crf"]),
            "-c:a",
            str(encoder["audio_codec"]),
            "-b:a",
            str(encoder["audio_bitrate"]),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    _run(command, "fullscreen voice-backbone composition")


def render_plan(
    plan: dict[str, Any],
    project_dir: Path,
    output_dir: Path,
    ffmpeg: str,
    config: dict[str, Any],
    keep_work: bool = False,
) -> Path:
    fullscreen_mode = "base_video" in plan and "fullscreen_events" in plan
    segments = plan.get("segments", [])
    if not fullscreen_mode and not segments:
        raise ValueError("Edit plan has no segments")

    work_dir = Path(tempfile.mkdtemp(prefix=".render-", dir=output_dir))
    try:
        if fullscreen_mode:
            joined = work_dir / "joined.mp4"
            _render_fullscreen_base(
                ffmpeg,
                joined,
                plan,
                project_dir,
            )
            final_path = output_dir / str(
                plan.get("render", {}).get("output_filename", "final.mp4")
            )
            _finish_video(
                ffmpeg,
                joined,
                final_path,
                plan,
                project_dir,
                output_dir,
                config,
            )
            return final_path

        rendered_segments: list[Path] = []
        for index, segment in enumerate(segments):
            source = _project_file(project_dir, str(segment["source"]))
            target = work_dir / f"{index:02d}-{segment['id']}.mp4"
            _render_segment(
                ffmpeg,
                source,
                target,
                segment,
                plan["canvas"],
                plan["render"],
            )
            rendered_segments.append(target)

        joined = work_dir / "joined.mp4"
        _concat_segments(ffmpeg, rendered_segments, joined)
        final_path = output_dir / str(
            plan.get("render", {}).get("output_filename", "final.mp4")
        )
        _finish_video(
            ffmpeg,
            joined,
            final_path,
            plan,
            project_dir,
            output_dir,
            config,
        )
        return final_path
    finally:
        if not keep_work:
            shutil.rmtree(work_dir, ignore_errors=True)
