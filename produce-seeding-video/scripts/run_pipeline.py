#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from video_pipeline.pipeline import run_project, run_stage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze, plan, subtitle, render, and validate a fixed-template "
            "vertical product recommendation video."
        )
    )
    parser.add_argument("project_dir", type=Path, help="Input project directory")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory (default: <project>/output)",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Generate analysis, plan, and subtitles without rendering",
    )
    parser.add_argument(
        "--stage",
        choices=("analyze", "plan", "render", "qa"),
        help="Run exactly one lower-level stage for the Video OS Director",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep normalized intermediate clips for debugging",
    )
    parser.add_argument("--ffmpeg", help="Explicit ffmpeg executable path")
    parser.add_argument("--ffprobe", help="Explicit ffprobe executable path")
    parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="Knowledge Root used by the Planner Memory advisory layer",
    )
    parser.add_argument(
        "--jianying",
        action="store_true",
        help="Also export an editable Jianying Pro draft",
    )
    parser.add_argument("--jianying-draft-root", type=Path, help="Jianying draft root directory")
    parser.add_argument("--jianying-draft-name", help="Name shown in Jianying")
    parser.add_argument(
        "--jianying-portable-media",
        action="store_true",
        help="Copy referenced media into the Jianying draft folder",
    )
    parser.add_argument("--jianying-zip", type=Path, help="Optional Jianying draft backup ZIP")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.stage:
            if args.plan_only or args.jianying:
                raise ValueError("--stage cannot be combined with --plan-only or --jianying")
            result = run_stage(
                args.project_dir,
                args.stage,
                args.output,
                keep_work=args.keep_work,
                ffmpeg_path=args.ffmpeg,
                ffprobe_path=args.ffprobe,
                knowledge_root=args.knowledge_root,
            )
        else:
            result = run_project(
                args.project_dir,
                args.output,
                plan_only=args.plan_only,
                keep_work=args.keep_work,
                ffmpeg_path=args.ffmpeg,
                ffprobe_path=args.ffprobe,
                export_jianying=True if args.jianying else None,
                jianying_draft_root=args.jianying_draft_root,
                jianying_draft_name=args.jianying_draft_name,
                jianying_portable_media=True if args.jianying_portable_media else None,
                jianying_zip_path=args.jianying_zip,
                knowledge_root=args.knowledge_root,
            )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
