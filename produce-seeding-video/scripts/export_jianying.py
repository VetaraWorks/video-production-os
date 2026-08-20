#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from video_pipeline.jianying import export_jianying_draft


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export an editable Jianying Pro draft from edit_plan.json.")
    parser.add_argument("plan", type=Path, help="Standard edit_plan.json or fullscreen plan JSON")
    parser.add_argument("--project-dir", type=Path, required=True, help="Project root used to resolve media")
    parser.add_argument("--output-dir", type=Path, help="Rendered artifact directory used to resolve subtitles")
    parser.add_argument(
        "--draft-root",
        type=Path,
        help="Jianying draft root (default: <output-dir>/jianying_drafts)",
    )
    parser.add_argument("--draft-name", required=True, help="Name shown in Jianying")
    parser.add_argument("--no-portable-media", action="store_true", help="Reference original media instead of copying it")
    parser.add_argument("--zip", type=Path, help="Optional portable backup ZIP path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = args.plan.expanduser().resolve()
    project_dir = args.project_dir.expanduser().resolve()
    output_dir = (args.output_dir or plan_path.parent).expanduser().resolve()
    draft_root = (
        args.draft_root.expanduser().resolve()
        if args.draft_root is not None
        else output_dir / "jianying_drafts"
    )
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        result = export_jianying_draft(
            plan,
            project_dir=project_dir,
            output_dir=output_dir,
            draft_root=draft_root,
            draft_name=args.draft_name,
            portable_media=not args.no_portable_media,
            zip_path=args.zip.expanduser() if args.zip else None,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
