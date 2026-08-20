#!/usr/bin/env python3
"""Video OS CLI: init / snapshot / validate / explain (Phase 1) and
status / run (Phase 2 project state machine with resume)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from video_os_core import knowledge
from video_os_core import project_manager
from video_os_core import repair_manager
from video_os_core import worker_manager
from video_os_core import system_manager
from video_os_core import report_manager
from video_os_core import memory_suggestions
from video_os_core import decision_log
from video_os_core.memory_reader import load_rules
from video_os_core.knowledge_root import KnowledgeRootError, require_knowledge_root
from video_os_core.rule_matcher import match_rules, write_match_report
from video_os_core.version_manager import (
    explain_snapshot,
    snapshot_project,
    validate_snapshot,
)


DEFAULT_PROJECTS_ROOT = Path(__file__).resolve().parents[2] / "projects"


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Video OS project tooling")
    parser.add_argument(
        "--projects-root",
        type=Path,
        help="Projects root (default: user setup config, then <skill>/projects)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser(
        "setup", help="Create versioned user configuration and data directories"
    )
    setup_parser.add_argument("--data-root", type=Path)
    setup_parser.add_argument(
        "--provider",
        choices=["none", "gemini-worker", "gemini_worker", "qwen-api", "qwen_api"],
        default="none",
    )
    setup_parser.add_argument("--api-key-env", default="QWEN_API_KEY")
    setup_parser.add_argument("--model")
    setup_parser.add_argument("--force", action="store_true")
    setup_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser(
        "doctor", help="Diagnose runtime, storage, and Provider readiness"
    )
    doctor_parser.add_argument("--data-root", type=Path)
    doctor_parser.add_argument("--config", type=Path)
    doctor_parser.add_argument("--json", action="store_true")

    for runtime_parser in (setup_parser, doctor_parser):
        runtime_parser.add_argument("--python")
        runtime_parser.add_argument("--node")
        runtime_parser.add_argument("--ffmpeg")
        runtime_parser.add_argument("--ffprobe")
        runtime_parser.add_argument("--browser")
        runtime_parser.add_argument("--node-modules")

    report_parser = subparsers.add_parser(
        "report", help="Create a strictly redacted diagnostic report.zip"
    )
    report_parser.add_argument("project", help="Project directory or registered name")
    report_parser.add_argument("--output", type=Path)
    report_parser.add_argument("--data-root", type=Path)

    init_parser = subparsers.add_parser("init", help="Create project skeleton")
    init_parser.add_argument("project_name")

    snapshot_parser = subparsers.add_parser(
        "snapshot", help="Archive a source directory as a version snapshot"
    )
    snapshot_parser.add_argument("source_dir", type=Path)
    snapshot_parser.add_argument("--project", required=True)
    snapshot_parser.add_argument("--as", dest="version", required=True)
    snapshot_parser.add_argument(
        "--max-size-mb", type=float, default=5.0, help="Max copied file size in MB"
    )
    snapshot_parser.add_argument(
        "--force", action="store_true", help="Replace an existing snapshot"
    )

    validate_parser = subparsers.add_parser("validate", help="Validate a snapshot")
    validate_parser.add_argument("project_name")
    validate_parser.add_argument("version")

    explain_parser = subparsers.add_parser("explain", help="Explain a snapshot")
    explain_parser.add_argument("project_name")
    explain_parser.add_argument("version")

    status_parser = subparsers.add_parser("status", help="Show project pipeline state")
    status_parser.add_argument("project", help="Project directory or registered name")
    status_parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="Knowledge Root used to validate Planner Memory artifacts",
    )

    run_parser = subparsers.add_parser("run", help="Run pipeline stages with resume")
    run_parser.add_argument("project", help="Project directory or registered name")
    run_parser.add_argument(
        "--to",
        default="FINAL",
        help="Target stage (default: FINAL)",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="Reset stage records and re-run from INIT",
    )
    run_parser.add_argument("--ffmpeg", help="Explicit ffmpeg executable path")
    run_parser.add_argument("--ffprobe", help="Explicit ffprobe executable path")
    run_parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="Optional initialized Knowledge Root for Planner Memory and evidence sync",
    )

    worker_parser = subparsers.add_parser(
        "worker", help="Manage the optional Gemini Browser Worker"
    )
    worker_actions = worker_parser.add_subparsers(dest="worker_action", required=True)

    def add_worker_root(action_parser: argparse.ArgumentParser) -> None:
        action_parser.add_argument(
            "--data-root",
            type=Path,
            help="Video OS user data root (default: VIDEO_OS_DATA_ROOT or LocalAppData)",
        )

    def add_worker_runtime(action_parser: argparse.ArgumentParser) -> None:
        add_worker_root(action_parser)
        action_parser.add_argument("--browser", help="Explicit Chrome or Edge executable")
        action_parser.add_argument("--node", help="Explicit Node.js executable")
        action_parser.add_argument("--python", help="Explicit Python executable")
        action_parser.add_argument("--node-modules", help="Directory containing Playwright")
        action_parser.add_argument("--ffmpeg", help="Explicit FFmpeg executable")
        action_parser.add_argument("--ffprobe", help="Explicit ffprobe executable")

    worker_status_parser = worker_actions.add_parser("status", help="Show Worker and login state")
    add_worker_root(worker_status_parser)
    worker_login_parser = worker_actions.add_parser(
        "login", help="Initialize and open the dedicated Gemini browser profile"
    )
    add_worker_runtime(worker_login_parser)
    worker_login_parser.add_argument(
        "--cdp-port", type=int, help="Explicit first-time CDP port (default: scan from 19222)"
    )
    worker_login_parser.add_argument("--timeout", type=float, default=15.0)
    worker_start_parser = worker_actions.add_parser("start", help="Start the managed Worker process")
    add_worker_runtime(worker_start_parser)
    worker_start_parser.add_argument(
        "--cdp-port", type=int, help="Explicit first-time CDP port (default: scan from 19222)"
    )
    worker_start_parser.add_argument("--timeout", type=float, default=6.0)
    worker_stop_parser = worker_actions.add_parser("stop", help="Stop only the managed Worker process")
    add_worker_root(worker_stop_parser)
    worker_stop_parser.add_argument("--timeout", type=float, default=6.0)

    repair_parser = subparsers.add_parser(
        "repair", help="Generate and optionally apply a repair plan"
    )
    repair_parser.add_argument("project", help="Project directory or registered name")
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="Confirm and execute the repair plan (still asks for confirmation)",
    )
    repair_parser.add_argument("--ffmpeg", help="Explicit ffmpeg executable path")
    repair_parser.add_argument("--ffprobe", help="Explicit ffprobe executable path")

    feedback_parser = subparsers.add_parser(
        "feedback", help="Collect human feedback into knowledge/edits/"
    )
    feedback_parser.add_argument("project", help="Project directory or registered name")
    feedback_parser.add_argument(
        "--from-version",
        "--source-version",
        dest="from_version",
        help="Source version (schema: from_version)",
    )
    feedback_parser.add_argument(
        "--to-version",
        "--target-version",
        dest="to_version",
        help="Target version (schema: to_version)",
    )
    feedback_parser.add_argument("--category", help="e.g. rhythm/shot_selection/subtitle/style_preference")
    feedback_parser.add_argument("--rule-class", choices=["hard", "editing", "style", "audit"])
    feedback_parser.add_argument(
        "--target-kind", choices=["segment", "time_range", "whole_video"], default="whole_video"
    )
    feedback_parser.add_argument("--segment-id", help="Required when --target-kind segment")
    feedback_parser.add_argument("--start", type=float, help="Required when --target-kind time_range")
    feedback_parser.add_argument("--end", type=float, help="Required when --target-kind time_range")
    feedback_parser.add_argument("--before", help="Before description")
    feedback_parser.add_argument("--after", help="After description")
    feedback_parser.add_argument("--reason", help="Why the change was made")
    feedback_parser.add_argument("--confidence", type=float, help="Submitter confidence 0-1")
    feedback_parser.add_argument("--rule-candidate", help="Optional draft rule text")
    feedback_parser.add_argument(
        "--source-doc", action="append", default=[], help="Source document (repeatable)"
    )
    feedback_parser.add_argument(
        "--snapshot-ref", action="append", default=[], help="Snapshot reference (repeatable)"
    )
    feedback_parser.add_argument(
        "--changes-file", type=Path, help="Load a changes[] array from a JSON file"
    )
    feedback_parser.add_argument(
        "--from-repair",
        action="store_true",
        help="Build a draft from the project's repair diff/plan (never saves to edits/)",
    )
    feedback_parser.add_argument(
        "--save-draft",
        action="store_true",
        help="With --from-repair: write the draft to <project>/feedback_drafts/",
    )
    feedback_parser.add_argument(
        "--import",
        dest="import_draft",
        type=Path,
        help="Import a validated draft file into knowledge/edits/",
    )
    feedback_parser.add_argument(
        "--dry-run", action="store_true", help="Print the feedback record without saving"
    )
    feedback_parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="Absolute Knowledge Root; defaults to VIDEO_OS_KNOWLEDGE_ROOT",
    )

    memory_parser = subparsers.add_parser(
        "memory-preview",
        help="Generate a read-only L0 rule match preview for a project",
    )
    memory_parser.add_argument("project", help="Project directory or registered name")
    memory_parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="Absolute Knowledge Root; defaults to VIDEO_OS_KNOWLEDGE_ROOT",
    )
    memory_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the preview to stdout without writing the report file",
    )

    suggest_parser = subparsers.add_parser(
        "memory-suggest",
        help="Generate a read-only memory suggestion report for a project",
    )
    suggest_parser.add_argument("project", help="Project directory or registered name")
    suggest_parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="Absolute Knowledge Root; defaults to VIDEO_OS_KNOWLEDGE_ROOT",
    )
    suggest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print suggestions to stdout without writing any file",
    )

    decide_parser = subparsers.add_parser(
        "memory-decide",
        help="Record a human decision about a memory suggestion (append-only)",
    )
    decide_parser.add_argument("project", help="Project directory or registered name")
    decide_parser.add_argument(
        "--suggestion-id", required=True, help="Current signature-bound suggestion id"
    )
    decide_parser.add_argument(
        "--decision",
        required=True,
        choices=[
            "accept",
            "reject",
            "defer",
            "accepted",
            "rejected",
            "modified",
            "deferred",
        ],
    )
    decide_parser.add_argument("--reviewer", required=True, help="Human reviewer name")
    decide_parser.add_argument("--reason", required=True, help="Why the decision was made")
    decide_parser.add_argument(
        "--modified-value",
        help="JSON value required when --decision modified",
    )
    decide_parser.add_argument(
        "--knowledge-root",
        type=Path,
        help="Absolute Knowledge Root; defaults to VIDEO_OS_KNOWLEDGE_ROOT",
    )
    decide_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the record without writing any file",
    )

    decisions_parser = subparsers.add_parser(
        "memory-decisions",
        help="List recorded memory decisions for a project",
    )
    decisions_parser.add_argument("project", help="Project directory or registered name")
    return parser.parse_args()


def main() -> int:
    _configure_console_encoding()
    args = parse_args()
    try:
        system_config = system_manager.apply_system_config()
        configured_projects = system_manager.configured_projects_root(system_config)
        projects_root = (
            args.projects_root.expanduser().resolve()
            if args.projects_root is not None
            else configured_projects or DEFAULT_PROJECTS_ROOT.resolve()
        )
        if args.command == "setup":
            overrides = {
                name: getattr(args, name)
                for name in ("python", "node", "ffmpeg", "ffprobe", "browser", "node_modules")
                if getattr(args, name, None)
            }
            result = system_manager.setup_video_os(
                args.data_root,
                provider=args.provider,
                api_key_env=args.api_key_env,
                model=args.model,
                runtime_overrides=overrides,
                force=args.force,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                _print_setup_summary(result)
            return 0
        elif args.command == "doctor":
            overrides = {
                name: getattr(args, name)
                for name in ("python", "node", "ffmpeg", "ffprobe", "browser", "node_modules")
                if getattr(args, name, None)
            }
            result = system_manager.doctor(
                data_root=args.data_root,
                config_path=args.config,
                runtime_overrides=overrides,
            )
            print(
                json.dumps(result, ensure_ascii=False, indent=2)
                if args.json
                else system_manager.format_doctor(result)
            )
            return 0 if result.get("ok") else 1
        elif args.command == "report":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = report_manager.create_report(
                project_dir,
                output=args.output,
                data_root=args.data_root,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "init":
            project_dir = projects_root / args.project_name
            (project_dir / "snapshots").mkdir(parents=True, exist_ok=True)
            readme = project_dir / "README.md"
            if not readme.exists():
                readme.write_text(
                    f"# {args.project_name}\n\nVideo OS 项目目录。\n"
                    "快照位于 `snapshots/<version>/`。\n",
                    encoding="utf-8",
                )
            result = {
                "ok": True,
                "project": args.project_name,
                "project_dir": str(project_dir),
            }
        elif args.command == "snapshot":
            result = snapshot_project(
                args.source_dir,
                projects_root,
                args.project,
                args.version,
                max_file_bytes=int(args.max_size_mb * 1024 * 1024),
                force=args.force,
            )
        elif args.command == "validate":
            result = validate_snapshot(
                projects_root,
                args.project_name,
                args.version,
            )
        elif args.command == "explain":
            result = explain_snapshot(
                projects_root,
                args.project_name,
                args.version,
            )
            print(result["summary"])
            return 0
        elif args.command == "status":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = project_manager.project_status(
                project_dir,
                knowledge_root=args.knowledge_root,
            )
            _print_status_summary(result)
        elif args.command == "run":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = project_manager.run_project(
                project_dir,
                to=args.to,
                force=args.force,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                knowledge_root=args.knowledge_root,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        elif args.command == "worker":
            data_root = args.data_root.expanduser().resolve() if args.data_root else None
            if args.worker_action == "status":
                result = worker_manager.worker_status(data_root)
            elif args.worker_action in {"login", "start"}:
                overrides = {
                    name: getattr(args, name)
                    for name in (
                        "browser",
                        "node",
                        "python",
                        "node_modules",
                        "ffmpeg",
                        "ffprobe",
                    )
                    if getattr(args, name, None)
                }
                if args.worker_action == "login":
                    result = worker_manager.worker_login(
                        data_root,
                        overrides=overrides,
                        cdp_port=args.cdp_port,
                        timeout=args.timeout,
                    )
                else:
                    result = worker_manager.worker_start(
                        data_root,
                        overrides=overrides,
                        cdp_port=args.cdp_port,
                        timeout=args.timeout,
                    )
            elif args.worker_action == "stop":
                result = worker_manager.worker_stop(data_root, timeout=args.timeout)
            else:
                raise ValueError(f"Unknown worker command: {args.worker_action}")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        elif args.command == "repair":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = repair_manager.repair_project(
                project_dir,
                projects_root,
                apply=args.apply,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
            )
            if result.get("mode") == "planned":
                _print_repair_plan(result["repair_plan"])
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        elif args.command == "feedback":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = _handle_feedback(project_dir, args)
        elif args.command == "memory-preview":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = _handle_memory_preview(project_dir, args)
            if result.get("dry_run"):
                return 0
        elif args.command == "memory-suggest":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = _handle_memory_suggest(project_dir, args)
            if result.get("dry_run"):
                return 0
        elif args.command == "memory-decide":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = _handle_memory_decide(project_dir, args)
            if result.get("dry_run"):
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
        elif args.command == "memory-decisions":
            project_dir = project_manager.resolve_project(args.project, projects_root)
            result = decision_log.list_decisions(project_dir)
        else:
            raise ValueError(f"Unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except system_manager.SystemConfigError as exc:
        payload = {"ok": False, "code": exc.code, "error": str(exc)}
        if exc.details is not None:
            payload["details"] = exc.details
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except worker_manager.WorkerError as exc:
        payload = {"ok": False, "code": exc.code, "error": str(exc)}
        if exc.details is not None:
            payload["details"] = exc.details
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except KnowledgeRootError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "knowledge_root": exc.status,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


def _print_setup_summary(result: dict) -> None:
    print("Video OS Setup")
    print("=" * 48)
    print(result.get("message") or "Setup completed.")
    print(f"Config: {result.get('config_path')}")
    config = result.get("config") or {}
    print(f"Data root: {config.get('data_root')}")
    print(f"Provider: {(config.get('provider') or {}).get('type', 'none')}")
    if result.get("alternative_data_root"):
        print(
            "Optional alternative: "
            f"{result['alternative_data_root']} (not selected; rerun setup explicitly to use it)"
        )


def _print_status_summary(status: dict) -> None:
    lines = [
        f"项目：{status['project']}",
        f"版本：{status['version']}",
        f"当前阶段：{status['stage']}",
        f"下一步：{status['next_action']}",
        f"锁定：{'是 (pid %s)' % status['lock_pid'] if status['locked'] else '否'}",
        f"需要人工：{status['needs_human']}",
        f"需要登录：{status['needs_login']}",
    ]
    if status["blocked"]:
        lines.append(
            f"阻塞：{status['blocked']['kind']} @ {status['blocked']['stage']} - "
            f"{status['blocked'].get('error') or ''}"
        )
    if status["last_error"]:
        lines.append(f"最近错误：{status['last_error']}")
    knowledge_status = status.get("knowledge")
    if isinstance(knowledge_status, dict) and knowledge_status.get("status") != "idle":
        lines.append(f"Knowledge：{knowledge_status.get('status')}")
        if knowledge_status.get("message"):
            lines.append(f"Knowledge 告警：{knowledge_status['message']}")
    if status["invalid_or_missing"]:
        lines.append("无效或缺失：" + ", ".join(status["invalid_or_missing"]))
    lines.append("各阶段：")
    for stage, record in status["stages"].items():
        lines.append(
            f"  {stage}: {record['status']} (attempts={record['attempts']})"
        )
    print("\n".join(lines))
    print()


def _print_repair_plan(plan: dict) -> None:
    print("Repair 计划：")
    for action in plan.get("actions", []):
        print(
            f"  - [{action.get('id')}] {action.get('type')} "
            f"segment={action.get('segment_id')} reason={action.get('reason')}"
        )
    for item in plan.get("needs_human", []):
        print(f"  - 需人工：{item}")
    if not plan.get("actions") and not plan.get("needs_human"):
        print("  - 无待执行修复")
    print()


def _handle_feedback(project_dir: Path, args: argparse.Namespace) -> dict:
    knowledge_root = require_knowledge_root(args.knowledge_root)

    if args.import_draft:
        draft = json.loads(args.import_draft.expanduser().resolve().read_text(encoding="utf-8-sig"))
        if not isinstance(draft, dict):
            raise ValueError("draft file must contain a feedback v2 object")
        draft["collector"] = "manual"
        draft["evidence_tier"] = "human_verified"
        errors = knowledge.validate_feedback_v2(draft)
        if errors:
            raise ValueError("Invalid feedback draft: " + "; ".join(errors))
        if args.dry_run:
            return {"ok": True, "mode": "dry-run", "feedback": draft}
        return {
            "ok": True,
            "mode": "imported",
            **knowledge.write_feedback_v2(knowledge_root, draft),
            "feedback_id": draft["feedback_id"],
        }

    project_name = project_dir.name
    if args.from_repair:
        repair_dir = project_dir / "repair"
        plan_path = repair_dir / "repair_plan.json"
        diff_path = repair_dir / "repair_diff.json"
        if not diff_path.is_file():
            raise FileNotFoundError(f"repair diff not found: {diff_path}")
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig")) if plan_path.is_file() else None
        diff = json.loads(diff_path.read_text(encoding="utf-8-sig"))
        from_version = args.from_version or "unknown"
        to_version = args.to_version or "v-next"
        draft = knowledge.build_feedback_draft_from_repair(
            project=project_name,
            from_version=from_version,
            to_version=to_version,
            repair_plan=plan,
            repair_diff=diff,
            source_docs=list(args.source_doc) or None,
            snapshot_refs=list(args.snapshot_ref) or None,
        )
        if args.save_draft:
            saved = knowledge.write_feedback_draft(project_dir, draft)
            return {
                "ok": True,
                "mode": "repair-draft-saved",
                "feedback_id": draft["feedback_id"],
                "draft": saved,
                "feedback": draft,
                "message": "draft only; run --import to confirm into knowledge/edits/",
            }
        return {
            "ok": True,
            "mode": "repair-draft",
            "feedback_id": draft["feedback_id"],
            "feedback": draft,
            "message": "draft only; use --save-draft to store it or --import to confirm",
        }

    if not (args.from_version and args.to_version and args.category and args.rule_class and args.reason):
        raise ValueError(
            "feedback requires --from-version --to-version --category --rule-class --reason "
            "(or --from-repair / --import)"
        )
    target = {"kind": args.target_kind}
    if args.target_kind == "segment":
        if not args.segment_id:
            raise ValueError("--segment-id is required for --target-kind segment")
        target["id"] = args.segment_id
    elif args.target_kind == "time_range":
        if args.start is None or args.end is None:
            raise ValueError("--start/--end are required for --target-kind time_range")
        target["start"] = args.start
        target["end"] = args.end

    changes = []
    if args.changes_file:
        loaded = json.loads(args.changes_file.expanduser().resolve().read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, list) or not loaded:
            raise ValueError("--changes-file must contain a non-empty changes[] array")
        changes = loaded
    else:
        changes = [
            {
                "category": args.category,
                "rule_class": args.rule_class,
                "target": target,
                "before": {"description": args.before or ""},
                "after": {"description": args.after or ""},
                "reason": args.reason,
                "confidence": args.confidence,
                "rule_candidate": args.rule_candidate,
                "source_docs": list(args.source_doc),
            }
        ]
    draft = knowledge.build_feedback_draft(
        project=project_name,
        from_version=args.from_version,
        to_version=args.to_version,
        changes=changes,
        source_docs=list(args.source_doc) or None,
        snapshot_refs=list(args.snapshot_ref) or None,
    )
    if args.dry_run:
        return {"ok": True, "mode": "dry-run", "feedback": draft}
    return {
        "ok": True,
        "mode": "saved",
        **knowledge.write_feedback_v2(knowledge_root, draft),
        "feedback_id": draft["feedback_id"],
        "change_count": len(draft["changes"]),
    }


def _handle_memory_preview(project_dir: Path, args: argparse.Namespace) -> dict:
    """L0 read-only preview. Never modifies project_state or edit_plan."""
    knowledge_root = require_knowledge_root(args.knowledge_root)
    context = memory_suggestions.build_project_context(project_dir)
    rules, invalid = load_rules(knowledge_root)
    report = match_rules(context, rules, invalid)
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return {"ok": True, "mode": "preview", "dry_run": True}
    report_dir = project_dir / "memory_preview"
    report_dir.mkdir(parents=True, exist_ok=True)
    write_match_report(report, report_dir / "rule_match_report.json")
    return {
        "ok": True,
        "mode": "preview",
        "dry_run": False,
        "report_file": str(report_dir / "rule_match_report.json"),
        "summary": report["summary"],
    }


def _handle_memory_suggest(project_dir: Path, args: argparse.Namespace) -> dict:
    """Read-only suggestion report. Never modifies project_state or edit_plan."""
    knowledge_root = require_knowledge_root(args.knowledge_root)
    suggestion = memory_suggestions.generate_memory_suggestions(
        project_dir, knowledge_root
    )
    if args.dry_run:
        print(json.dumps(suggestion, ensure_ascii=False, indent=2))
        return {"ok": True, "mode": "suggest", "dry_run": True}
    preview_dir = project_dir / "memory_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    path = preview_dir / "memory_suggestions.json"
    memory_suggestions.write_suggestion_report(suggestion, path)
    return {
        "ok": True,
        "mode": "suggest",
        "dry_run": False,
        "suggestion_file": str(path),
        "summary": suggestion["summary"],
    }


def _handle_memory_decide(project_dir: Path, args: argparse.Namespace) -> dict:
    """Record a human decision. Never modifies edit_plan/project_state/rules."""
    knowledge_root = require_knowledge_root(args.knowledge_root)
    modified_value = None
    if args.modified_value:
        try:
            modified_value = json.loads(args.modified_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--modified-value must be valid JSON: {exc}") from exc
    return decision_log.record_decision(
        project_dir,
        knowledge_root,
        suggestion_id=args.suggestion_id,
        decision=args.decision,
        reviewer=args.reviewer,
        reason=args.reason,
        modified_value=modified_value,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
