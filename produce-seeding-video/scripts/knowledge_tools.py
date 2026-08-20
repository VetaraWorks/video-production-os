#!/usr/bin/env python3
"""Knowledge Layer CLI (Phase 4.1): init / migrate-feedback / validate."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from video_os_core.knowledge import (
    init_knowledge,
    load_manifest,
    migrate_feedback_file,
    validate_feedback_v2,
)
from video_os_core.rule_candidates import (
    extract_rule_candidates,
    list_candidates,
    validate_rule_candidates,
)
from video_os_core.rule_approval import (
    activate_rule,
    approve_rule,
    deactivate_rule,
    defer_candidate,
    deprecate_rule,
    explain_rule,
    list_rules,
    reject_candidate,
    review_candidate,
    reopen_candidate,
    revoke_rule,
)
from video_os_core.decision_log import list_governance_history
from video_os_core.memory_reader import load_project_context, load_rules
from video_os_core.rule_matcher import match_rules, write_match_report
from video_os_core.rule_explainer import explain_match, validate_memory_api
from video_os_core.knowledge_root import (
    KNOWLEDGE_ROOT_ENV,
    KnowledgeRootError,
    configured_knowledge_root,
    inspect_knowledge_root,
    require_knowledge_root,
)
from video_os_core.production_evidence import record_manual_evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Video OS Knowledge tooling")
    parser.add_argument(
        "--root",
        type=Path,
        help=f"Absolute Knowledge Root; defaults to {KNOWLEDGE_ROOT_ENV}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create the knowledge tree")
    init_parser.add_argument("--force", action="store_true", help="Reset manifest")

    subparsers.add_parser(
        "status", help="Report Knowledge Root configuration and data state"
    )

    migrate_parser = subparsers.add_parser(
        "migrate-feedback", help="Migrate a feedback v1 file into v2"
    )
    migrate_parser.add_argument("source", type=Path, help="feedback v1 JSON path")
    migrate_parser.add_argument("--snapshot-ref", required=True)
    migrate_parser.add_argument("--overwrite", action="store_true")

    validate_parser = subparsers.add_parser(
        "validate", help="Validate feedback records under edits/"
    )
    validate_parser.add_argument("--file", type=Path, help="Validate one file only")

    evidence_parser = subparsers.add_parser(
        "record-evidence",
        help="Record a structured human edit as human_verified evidence",
    )
    evidence_parser.add_argument("--input", type=Path, required=True)
    evidence_parser.add_argument("--reviewer", required=True)
    evidence_parser.add_argument("--reason", required=True)

    extract_parser = subparsers.add_parser(
        "extract-rules", help="Aggregate evidence into rule_candidates/"
    )
    extract_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be produced without writing"
    )

    validate_rules_parser = subparsers.add_parser(
        "validate-rules", help="Validate rule_candidates/ records"
    )

    list_parser = subparsers.add_parser(
        "list-candidates", help="List rule candidates"
    )

    review_parser = subparsers.add_parser(
        "review-candidate", help="Show full review context for a candidate"
    )
    review_parser.add_argument("candidate_id")

    approve_parser = subparsers.add_parser(
        "approve-rule", help="Human-approve a candidate into an inactive editing rule"
    )
    approve_parser.add_argument("candidate_id")
    approve_parser.add_argument("--reviewer", required=True)
    approve_parser.add_argument("--reason", required=True)
    approve_parser.add_argument("--video-type")
    approve_parser.add_argument("--client")
    approve_parser.add_argument("--style-profile")
    approve_parser.add_argument("--conflict-resolution")
    approve_parser.add_argument("--dry-run", action="store_true")

    reject_parser = subparsers.add_parser(
        "reject-candidate", help="Reject a candidate (record kept, candidate kept)"
    )
    reject_parser.add_argument("candidate_id")
    reject_parser.add_argument("--reviewer", required=True)
    reject_parser.add_argument("--reason", required=True)
    reject_parser.add_argument(
        "--rejection-category",
        choices=[
            "wrong_aggregation",
            "insufficient_evidence",
            "over_generalization",
            "business_not_applicable",
        ],
    )
    reject_parser.add_argument(
        "--no-future-recandidacy", action="store_true",
        help="Disallow future re-candidacy (default allows)",
    )
    reject_parser.add_argument("--dry-run", action="store_true")

    defer_parser = subparsers.add_parser(
        "defer-candidate", help="Defer a candidate with resume conditions"
    )
    defer_parser.add_argument("candidate_id")
    defer_parser.add_argument("--reviewer", required=True)
    defer_parser.add_argument("--reason", required=True)
    defer_parser.add_argument("--minimum-new-projects", type=int, default=0)
    defer_parser.add_argument("--minimum-weighted-evidence", type=float, default=0.0)
    defer_parser.add_argument("--dry-run", action="store_true")

    reopen_parser = subparsers.add_parser(
        "reopen-candidate",
        help="Reopen a rejected/deferred candidate for re-review",
    )
    reopen_parser.add_argument("candidate_id")
    reopen_parser.add_argument("--reviewer", required=True)
    reopen_parser.add_argument("--reason", required=True)
    reopen_parser.add_argument("--dry-run", action="store_true")

    list_rules_parser = subparsers.add_parser(
        "list-rules", help="List editing rules"
    )

    explain_rule_parser = subparsers.add_parser(
        "explain-rule", help="Explain an editing rule with full audit trail"
    )
    explain_rule_parser.add_argument("rule_id")

    activate_parser = subparsers.add_parser(
        "activate-rule", help="Human-activate one exact rule revision for advisory use"
    )
    activate_parser.add_argument("rule_id")
    activate_parser.add_argument("--reviewer", required=True)
    activate_parser.add_argument("--reason", required=True)
    activate_parser.add_argument(
        "--application-mode", choices=["advisory"], default="advisory"
    )
    activate_parser.add_argument("--dry-run", action="store_true")

    deactivate_parser = subparsers.add_parser(
        "deactivate-rule", help="Human-deactivate a rule (audit history kept)"
    )
    deactivate_parser.add_argument("rule_id")
    deactivate_parser.add_argument("--reviewer", required=True)
    deactivate_parser.add_argument("--reason", required=True)
    deactivate_parser.add_argument("--dry-run", action="store_true")

    deprecate_parser = subparsers.add_parser(
        "deprecate-rule", help="Deprecate a rule (record kept)"
    )
    deprecate_parser.add_argument("rule_id")
    deprecate_parser.add_argument("--reviewer", required=True)
    deprecate_parser.add_argument("--reason", required=True)
    deprecate_parser.add_argument("--dry-run", action="store_true")

    revoke_parser = subparsers.add_parser(
        "revoke-rule", help="Revoke a rule (record kept; no new suggestions)"
    )
    revoke_parser.add_argument("rule_id")
    revoke_parser.add_argument("--reviewer", required=True)
    revoke_parser.add_argument("--reason", required=True)
    revoke_parser.add_argument("--dry-run", action="store_true")

    governance_parser = subparsers.add_parser(
        "governance-history", help="Show read-only decision statistics by rule"
    )
    governance_parser.add_argument("--rule-id")

    match_parser = subparsers.add_parser(
        "match-rules", help="L0 read-only rule match preview"
    )
    match_parser.add_argument("--project-context", type=Path, required=True)
    match_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report to stdout without writing any file",
    )
    match_parser.add_argument(
        "--output", type=Path, help="Report path (default: <cwd>/rule_match_report.json)"
    )
    match_parser.add_argument(
        "--include-historical",
        action="store_true",
        help="Include deprecated/superseded rules in the preview",
    )

    explain_match_parser = subparsers.add_parser(
        "explain-match", help="Explain one rule match with full traceability"
    )
    explain_match_parser.add_argument("--report", type=Path, required=True)
    explain_match_parser.add_argument("--rule-id", required=True)

    validate_memory_parser = subparsers.add_parser(
        "validate-memory-api", help="Validate the memory read API against knowledge/"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "init":
            root, _ = configured_knowledge_root(args.root)
            result = init_knowledge(root, force=args.force)
            result["knowledge_root_status"] = inspect_knowledge_root(root)
        elif args.command == "status":
            result = inspect_knowledge_root(args.root)
        elif args.command == "migrate-feedback":
            root = require_knowledge_root(args.root)
            result = migrate_feedback_file(
                args.source,
                root,
                args.snapshot_ref,
                overwrite=args.overwrite,
            )
        elif args.command == "validate":
            root = require_knowledge_root(args.root)
            if args.file:
                payload = json.loads(args.file.read_text(encoding="utf-8-sig"))
                errors = validate_feedback_v2(payload)
                result = {
                    "ok": not errors,
                    "file": str(args.file),
                    "errors": errors,
                }
            else:
                edits_dir = root / "edits"
                errors_by_file: dict[str, list[str]] = {}
                for path in sorted(edits_dir.glob("*.json")):
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                    errors = validate_feedback_v2(payload)
                    if errors:
                        errors_by_file[path.name] = errors
                result = {
                    "ok": not errors_by_file,
                    "edits_dir": str(edits_dir),
                    "valid_file_count": len(list(edits_dir.glob("*.json")))
                    - len(errors_by_file),
                    "invalid": errors_by_file,
                    "manifest": load_manifest(root),
                }
        elif args.command == "record-evidence":
            root = require_knowledge_root(args.root)
            payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("manual evidence input must be a JSON object")
            result = record_manual_evidence(
                root,
                payload,
                reviewer=args.reviewer,
                verification_reason=args.reason,
            )
        elif args.command == "extract-rules":
            root = require_knowledge_root(args.root)
            result = extract_rule_candidates(root, dry_run=args.dry_run)
        elif args.command == "validate-rules":
            root = require_knowledge_root(args.root)
            result = validate_rule_candidates(root)
        elif args.command == "list-candidates":
            root = require_knowledge_root(args.root)
            result = list_candidates(root)
        elif args.command == "review-candidate":
            root = require_knowledge_root(args.root)
            result = review_candidate(root, args.candidate_id)
        elif args.command == "approve-rule":
            root = require_knowledge_root(args.root)
            result = approve_rule(
                root,
                args.candidate_id,
                reviewer=args.reviewer,
                reason=args.reason,
                video_type=args.video_type,
                client=args.client,
                style_profile=args.style_profile,
                conflict_resolution=args.conflict_resolution,
                dry_run=args.dry_run,
            )
        elif args.command == "reject-candidate":
            root = require_knowledge_root(args.root)
            result = reject_candidate(
                root,
                args.candidate_id,
                reviewer=args.reviewer,
                reason=args.reason,
                rejection_category=args.rejection_category,
                allow_future_recandidacy=not args.no_future_recandidacy,
                dry_run=args.dry_run,
            )
        elif args.command == "defer-candidate":
            root = require_knowledge_root(args.root)
            result = defer_candidate(
                root,
                args.candidate_id,
                reviewer=args.reviewer,
                reason=args.reason,
                minimum_new_projects=args.minimum_new_projects,
                minimum_weighted_evidence=args.minimum_weighted_evidence,
                dry_run=args.dry_run,
            )
        elif args.command == "reopen-candidate":
            root = require_knowledge_root(args.root)
            result = reopen_candidate(
                root,
                args.candidate_id,
                reviewer=args.reviewer,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        elif args.command == "list-rules":
            root = require_knowledge_root(args.root)
            result = list_rules(root)
        elif args.command == "explain-rule":
            root = require_knowledge_root(args.root)
            result = explain_rule(root, args.rule_id)
        elif args.command == "activate-rule":
            root = require_knowledge_root(args.root)
            result = activate_rule(
                root,
                args.rule_id,
                reviewer=args.reviewer,
                reason=args.reason,
                application_mode=args.application_mode,
                dry_run=args.dry_run,
            )
        elif args.command == "deactivate-rule":
            root = require_knowledge_root(args.root)
            result = deactivate_rule(
                root,
                args.rule_id,
                reviewer=args.reviewer,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        elif args.command == "deprecate-rule":
            root = require_knowledge_root(args.root)
            result = deprecate_rule(
                root,
                args.rule_id,
                reviewer=args.reviewer,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        elif args.command == "revoke-rule":
            root = require_knowledge_root(args.root)
            result = revoke_rule(
                root,
                args.rule_id,
                reviewer=args.reviewer,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        elif args.command == "governance-history":
            root = require_knowledge_root(args.root)
            result = list_governance_history(root, rule_id=args.rule_id)
        elif args.command == "match-rules":
            root = require_knowledge_root(args.root)
            context = load_project_context(args.project_context)
            rules, invalid = load_rules(
                root,
                include_historical=args.include_historical,
            )
            report = match_rules(context, rules, invalid)
            if args.dry_run:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            output = args.output or (Path.cwd() / "rule_match_report.json")
            result = write_match_report(report, output)
            result["report"] = report
        elif args.command == "explain-match":
            root = require_knowledge_root(args.root)
            result = explain_match(root, args.report, args.rule_id)
        elif args.command == "validate-memory-api":
            root = require_knowledge_root(args.root)
            result = validate_memory_api(root)
        else:
            raise ValueError(f"Unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
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


if __name__ == "__main__":
    raise SystemExit(main())
