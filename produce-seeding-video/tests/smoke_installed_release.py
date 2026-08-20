#!/usr/bin/env python3
"""Run a lightweight fail-closed Video OS smoke against an installed Skill."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _load_checker(skill_root: Path):
    path = skill_root / "scripts" / "check_install_consistency.py"
    spec = importlib.util.spec_from_file_location("installed_consistency", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load installed consistency checker: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_input_video(ffmpeg: str, path: Path) -> None:
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x315a77:s=360x640:r=30:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg smoke input failed: {completed.stderr[-1200:]}")


def _write_project(skill_root: Path, project: Path, ffmpeg: str) -> None:
    (project / "script").mkdir(parents=True)
    (project / "raw_video").mkdir()
    (project / "config").mkdir()
    (project / "script" / "script.txt").write_text(
        "A short release smoke test.", encoding="utf-8"
    )
    _make_input_video(ffmpeg, project / "raw_video" / "clip.mp4")

    config = json.loads(
        (skill_root / "assets" / "default-config.json").read_text(encoding="utf-8-sig")
    )
    config["canvas"] = {"width": 360, "height": 640, "fps": 30}
    config["duration_seconds"] = 2.0
    config["template_segments"] = [
        {
            "id": "hook",
            "start": 0.0,
            "end": 1.0,
            "intent": "release smoke",
            "preferred_tags": ["talking"],
        },
        {
            "id": "cta",
            "start": 1.0,
            "end": 2.0,
            "intent": "release smoke CTA",
            "preferred_tags": ["talking"],
        },
    ]
    config["subtitles"]["enabled"] = False
    config["subtitles"]["margin_v"] = 160
    config["bgm"]["enabled"] = False
    config["sound_effects"]["enabled"] = False
    config["perception"]["enabled"] = False
    config["jianying_export"]["enabled"] = False
    config["video_os"]["review"].update(
        {
            "enabled": True,
            "worker_config": "config/missing-review-worker.json",
            "timeout_seconds": 5,
        }
    )
    (project / "config" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skill_root = args.skill_root.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    checker = _load_checker(skill_root)
    consistency = checker.check_consistency(source_root, skill_root)
    if not consistency["ok"]:
        raise RuntimeError(f"installed Skill drift: {json.dumps(consistency, ensure_ascii=False)}")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe are required for the installed release smoke")

    with tempfile.TemporaryDirectory(prefix="video-os-installed-smoke-") as temporary:
        project = Path(temporary) / "project"
        _write_project(skill_root, project, ffmpeg)
        completed = subprocess.run(
            [
                sys.executable,
                str(skill_root / "scripts" / "video_os.py"),
                "run",
                str(project),
                "--to",
                "FINAL",
                "--ffmpeg",
                ffmpeg,
                "--ffprobe",
                ffprobe,
            ],
            cwd=skill_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"installed Video OS returned non-JSON output: {completed.stdout[-1200:]}"
            ) from exc
        state = json.loads(
            (project / "project_state.json").read_text(encoding="utf-8-sig")
        )

    history = [entry.get("stage") for entry in state.get("history", [])]
    expected_prefix = ["ANALYZE", "PLAN", "RENDER", "QA", "REVIEW"]
    checks = {
        "consistency": consistency["ok"],
        "review_manager_installed": (
            skill_root / "scripts" / "video_os_core" / "review_manager.py"
        ).is_file(),
        "director_chain": history[: len(expected_prefix)] == expected_prefix,
        "qa_done": state["stages"]["QA"]["status"] == "done",
        "review_fail_closed": (
            completed.returncode != 0
            and result.get("ok") is False
            and result.get("stage") == "REVIEW"
            and result.get("blocked", {}).get("kind") == "needs_human"
            and result.get("blocked", {}).get("stage") == "REVIEW"
        ),
        "final_not_reached": (
            result.get("stage") != "FINAL"
            and result.get("ok") is False
            and "FINAL" not in history
        ),
    }
    summary = {
        "ok": all(checks.values()),
        "skill_root": str(skill_root),
        "source_root": str(source_root),
        "checked_files": consistency["checked_files"],
        "history": history,
        "video_os_exit_code": completed.returncode,
        "video_os_stage": result.get("stage"),
        "video_os_reason": result.get("reason"),
        "executed_stages": result.get("executed_stages"),
        "qa_status": state["stages"]["QA"]["status"],
        "qa_error": state["stages"]["QA"].get("last_error"),
        "review_status": state["stages"]["REVIEW"]["status"],
        "review_error": state["stages"]["REVIEW"].get("last_error"),
        "final_status": state["stages"]["FINAL"]["status"],
        "stdout_tail": completed.stdout[-1200:],
        "stderr_tail": completed.stderr[-1200:],
        "checks": checks,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
