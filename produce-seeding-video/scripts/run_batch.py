#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from video_pipeline.pipeline import run_project


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the fixed-template video pipeline for each child project."
    )
    parser.add_argument("batch_root", type=Path, help="Root containing project folders")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Generate planning artifacts without rendering",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep per-project render intermediates",
    )
    parser.add_argument("--ffmpeg", help="Explicit ffmpeg executable path")
    parser.add_argument("--ffprobe", help="Explicit ffprobe executable path")
    parser.add_argument(
        "--report",
        type=Path,
        help="Batch report path (default: <batch-root>/batch_report.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_root = args.batch_root.expanduser().resolve()
    if not batch_root.is_dir():
        print(
            json.dumps(
                {"ok": False, "error": f"Batch root not found: {batch_root}"},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    projects = sorted(
        {
            script_path.parent.parent.resolve()
            for script_path in batch_root.glob("*/script/script.txt")
            if script_path.is_file()
        },
        key=lambda path: path.name.casefold(),
    )
    if not projects:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "No child project with script/script.txt was found under "
                        f"{batch_root}"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    results: list[dict[str, object]] = []
    for project in projects:
        try:
            result = run_project(
                project,
                plan_only=args.plan_only,
                keep_work=args.keep_work,
                ffmpeg_path=args.ffmpeg,
                ffprobe_path=args.ffprobe,
            )
            results.append(
                {
                    "project": project.name,
                    "ok": bool(result["ok"]),
                    "result": result,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "project": project.name,
                    "ok": False,
                    "error": str(exc),
                }
            )

    failed = len([item for item in results if not item["ok"]])
    report = {
        "ok": failed == 0,
        "mode": "plan-only" if args.plan_only else "render",
        "batch_root": str(batch_root),
        "project_count": len(projects),
        "success_count": len(projects) - failed,
        "failure_count": failed,
        "projects": results,
    }
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else batch_root / "batch_report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
