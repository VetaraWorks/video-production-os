from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import init_knowledge, load_manifest  # noqa: E402
from video_os_core.rule_approval import (  # noqa: E402
    ApprovalError,
    activate_rule,
    approve_rule,
    deactivate_rule,
    defer_candidate,
    deprecate_rule,
    explain_rule,
    list_rules,
    reject_candidate,
    review_candidate,
    validate_rule_integrity,
)
from video_os_core.rule_candidates import (  # noqa: E402
    extract_rule_candidates,
    validate_rule_candidate,
)
from governance_fixtures import write_production_evidence  # noqa: E402


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_feedback(project: str, version: str, feedback_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "evidence_tier": "production_verified",
        "feedback_id": feedback_id,
        "project": project,
        "from_version": "v001",
        "to_version": version,
        "collector": "manual",
        "collected_at": "2026-08-05T00:00:00+00:00",
        "source_docs": ["review.json"],
        "snapshot_refs": [f"projects/{project}/snapshots/{version}"],
        "changes": [
            {
                "change_id": "c-1",
                "category": "timing",
                "rule_class": "editing",
                "target": {"kind": "whole_video"},
                "before": {
                    "description": "x",
                    "metric": {"name": "product_first_appearance_s", "value": 22.0},
                },
                "after": {
                    "description": "y",
                    "metric": {"name": "product_first_appearance_s", "value": 8.0},
                },
                "reason": "test",
                "status": "pending",
                "source_docs": ["review.json"],
            }
        ],
    }


class RuleApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rule-approval-test-")
        self.root = Path(self._tmp.name) / "knowledge"
        init_knowledge(self.root)
        self.evidence_dir = self.root / "repair_log"
        write_production_evidence(
            self.root,
            project_id="project-demo",
            project="demo",
            run_id="v002",
            evidence_id="evidence-a",
        )
        write_production_evidence(
            self.root,
            project_id="project-demo2",
            project="demo2",
            run_id="v003",
            evidence_id="evidence-b",
        )
        extract_rule_candidates(self.root)
        candidates_dir = self.root / "rule_candidates"
        files = list(candidates_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.candidate_id = files[0].stem
        self.candidate_file = files[0]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load_candidate(self) -> dict[str, Any]:
        return json.loads(self.candidate_file.read_text(encoding="utf-8"))

    def _set_candidate_status(self, status: str) -> None:
        candidate = self._load_candidate()
        candidate["status"] = status
        write_json(self.candidate_file, candidate)

    def test_approve_creates_inactive_rule(self) -> None:
        result = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="两个独立项目支持，无有效反例",
            video_type="口播种草",
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["idempotent"])
        rule_file = Path(result["rule_file"])
        rule = json.loads(rule_file.read_text(encoding="utf-8"))
        self.assertEqual(rule["status"], "inactive")
        self.assertEqual(rule["source_candidate_id"], self.candidate_id)
        self.assertEqual(rule["expression"]["metric"], "product_first_appearance_s")
        self.assertEqual(rule["scope"]["video_type"], "口播种草")
        self.assertEqual(len(rule["evidence_snapshot"]), 2)
        self.assertTrue(rule["approval"]["review_id"].startswith("review-"))
        # candidate marked approved, file kept
        self.assertEqual(self._load_candidate()["status"], "approved")
        # review record written
        reviews = list((self.root / "reviews").glob("*.json"))
        self.assertEqual(len(reviews), 1)

    def test_duplicate_approval_idempotent(self) -> None:
        first = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="批准",
        )
        second = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="批准",
        )
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["rule_id"], second["rule_id"])
        rules = list((self.root / "editing_rules").glob("*.json"))
        self.assertEqual(len(rules), 1)

    def test_approved_candidate_is_immutable_and_new_evidence_creates_revision(self) -> None:
        result = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="lifecycle-reviewer",
            reason="approved evidence must remain immutable",
        )
        self.assertTrue(result["ok"])
        approved_bytes = self.candidate_file.read_bytes()
        review_files = sorted((self.root / "reviews").glob("*.json"))
        rule_files = sorted((self.root / "editing_rules").glob("*.json"))
        review_bytes = {path.name: path.read_bytes() for path in review_files}
        rule_bytes = {path.name: path.read_bytes() for path in rule_files}
        review_record = json.loads(review_files[0].read_text(encoding="utf-8"))
        rule_record = json.loads(rule_files[0].read_text(encoding="utf-8"))
        self.assertEqual(review_record["reviewer"]["name"], "lifecycle-reviewer")
        self.assertEqual(
            review_record["reason"], "approved evidence must remain immutable"
        )
        self.assertEqual(len(rule_record["evidence_snapshot"]), 2)

        unchanged = extract_rule_candidates(self.root)
        self.assertEqual(unchanged["unchanged"], 1)
        self.assertEqual(unchanged["revisions_created"], 0)
        self.assertEqual(self.candidate_file.read_bytes(), approved_bytes)
        self.assertEqual(
            {path.name: path.read_bytes() for path in review_files}, review_bytes
        )
        self.assertEqual(
            {path.name: path.read_bytes() for path in rule_files}, rule_bytes
        )

        write_production_evidence(
            self.root,
            project_id="project-demo3",
            project="demo3",
            run_id="v004",
            evidence_id="evidence-c",
        )
        revised = extract_rule_candidates(self.root)
        self.assertEqual(revised["revisions_created"], 1)
        self.assertEqual(self.candidate_file.read_bytes(), approved_bytes)
        candidate_files = sorted((self.root / "rule_candidates").glob("*.json"))
        self.assertEqual(len(candidate_files), 2)
        revision_file = next(path for path in candidate_files if path != self.candidate_file)
        revision = json.loads(revision_file.read_text(encoding="utf-8"))
        original = self._load_candidate()
        self.assertEqual(revision["status"], "candidate")
        self.assertEqual(revision["revision"], 2)
        self.assertEqual(revision["lineage_id"], original["lineage_id"])
        self.assertEqual(revision["supersedes_candidate_id"], self.candidate_id)

        all_candidate_bytes = {
            path.name: path.read_bytes() for path in candidate_files
        }
        rerun = extract_rule_candidates(self.root)
        self.assertEqual(rerun["revisions_created"], 0)
        self.assertEqual(rerun["unchanged"], 1)
        self.assertEqual(
            {path.name: path.read_bytes() for path in candidate_files},
            all_candidate_bytes,
        )
        self.assertEqual(
            {path.name: path.read_bytes() for path in review_files}, review_bytes
        )
        self.assertEqual(
            {path.name: path.read_bytes() for path in rule_files}, rule_bytes
        )

    def test_rejected_candidate_is_not_resurrected_by_extraction(self) -> None:
        result = reject_candidate(
            self.root,
            self.candidate_id,
            reviewer="lifecycle-reviewer",
            reason="rejected evidence remains rejected",
            rejection_category="insufficient_scope",
        )
        self.assertTrue(result["ok"])
        rejected_bytes = self.candidate_file.read_bytes()
        review_files = sorted((self.root / "reviews").glob("*.json"))
        review_bytes = {path.name: path.read_bytes() for path in review_files}
        review_record = json.loads(review_files[0].read_text(encoding="utf-8"))
        self.assertEqual(review_record["reviewer"]["name"], "lifecycle-reviewer")
        self.assertEqual(
            review_record["reason"], "rejected evidence remains rejected"
        )

        rerun = extract_rule_candidates(self.root)
        self.assertEqual(rerun["unchanged"], 1)
        self.assertEqual(rerun["revisions_created"], 0)
        self.assertEqual(self.candidate_file.read_bytes(), rejected_bytes)
        self.assertEqual(self._load_candidate()["status"], "rejected")
        self.assertEqual(
            {path.name: path.read_bytes() for path in review_files}, review_bytes
        )

    def test_stale_candidate_denied(self) -> None:
        self._set_candidate_status("stale")
        result = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="试试",
        )
        self.assertFalse(result["ok"])
        self.assertIn("stale", " ".join(result["reasons"]))
        self.assertEqual(len(list((self.root / "editing_rules").glob("*.json"))), 0)

    def test_conflicted_candidate_needs_resolution(self) -> None:
        self._set_candidate_status("conflicted")
        denied = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="有冲突",
        )
        self.assertFalse(denied["ok"])
        self.assertTrue(
            any("conflict_resolution" in item for item in denied["reasons"])
        )
        approved = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="有冲突",
            conflict_resolution="已人工复核，确认适用",
        )
        self.assertTrue(approved["ok"])

    def test_missing_source_denied(self) -> None:
        candidate = self._load_candidate()
        candidate["evidence"][0]["source_file"] = "missing-evidence.json"
        write_json(self.candidate_file, candidate)
        result = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="试试",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing" in item for item in result["reasons"]))

    def test_confidence_not_recomputable_denied(self) -> None:
        candidate = self._load_candidate()
        candidate["confidence"] = 0.99
        write_json(self.candidate_file, candidate)
        result = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="试试",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("confidence" in item for item in result["reasons"]))

    def test_scope_can_narrow_but_not_expand(self) -> None:
        # Candidate scope has null video_type; narrowing to a type is allowed.
        ok = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="批准",
            video_type="口播种草",
        )
        self.assertTrue(ok["ok"])
        # A second candidate for expansion test: reset status then try a different type.
        candidate = self._load_candidate()
        candidate["status"] = "candidate"
        candidate["scope"]["video_type"] = "口播种草"
        write_json(self.candidate_file, candidate)
        denied = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="试试",
            video_type="国风宣传",
        )
        self.assertFalse(denied["ok"])
        self.assertTrue(any("scope cannot be expanded" in item for item in denied["reasons"]))

    def test_reject_keeps_candidate_and_records(self) -> None:
        result = reject_candidate(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="证据来自同一项目，不能泛化",
            rejection_category="over_generalization",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(self.candidate_file.is_file())
        self.assertEqual(self._load_candidate()["status"], "rejected")
        self.assertEqual(len(list((self.root / "reviews").glob("*.json"))), 1)

    def test_defer_records_resume_conditions(self) -> None:
        result = defer_candidate(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="等待更多独立项目",
            minimum_new_projects=2,
            minimum_weighted_evidence=4.0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["resume_when"]["minimum_new_projects"], 2)
        self.assertEqual(result["resume_when"]["minimum_weighted_evidence"], 4.0)
        self.assertEqual(self._load_candidate()["status"], "deferred")
        review = json.loads(
            list((self.root / "reviews").glob("*.json"))[0].read_text(encoding="utf-8")
        )
        self.assertEqual(review["decision"], "defer")
        self.assertEqual(review["resume_when"]["minimum_new_projects"], 2)

    def test_evidence_snapshot_preserved(self) -> None:
        approve_rule(self.root, self.candidate_id, reviewer="user", reason="批准")
        # Delete the original production evidence afterwards.
        (self.evidence_dir / "evidence-a.json").unlink()
        result = explain_rule(
            self.root,
            list_rules(self.root)["rules"][0]["rule_id"],
        )
        self.assertEqual(result["evidence_status"], "invalid")
        rule = result["rule"]
        self.assertEqual(len(rule["evidence_snapshot"]), 2)

    def test_rule_conflict_triggers_needs_human(self) -> None:
        # First rule approved with a specific scope.
        approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="批准",
            video_type="口播种草",
        )
        # Construct a conflicting candidate deterministically (same metric,
        # different expression) and approve it against the existing rule.
        from video_os_core.rule_approval import candidate_path

        conflicting = json.loads(self.candidate_file.read_text(encoding="utf-8"))
        conflicting["candidate_id"] = "cand-conflict-fixture"
        conflicting["rule_id"] = "cand-conflict-fixture"
        conflicting["expression"] = {
            "metric": "product_first_appearance_s",
            "operator": ">=",
            "value": 15.0,
        }
        conflicting["scope"] = {"video_type": "口播种草", "client": None, "style_profile": None}
        conflicting["status"] = "candidate"
        write_json(candidate_path(self.root, "cand-conflict-fixture"), conflicting)
        result = approve_rule(
            self.root,
            "cand-conflict-fixture",
            reviewer="user",
            reason="试试冲突",
            video_type="口播种草",
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("rule conflicts" in item for item in result["reasons"]))
        self.assertTrue(result["rule_conflicts"])

    def test_deprecate_keeps_rule(self) -> None:
        approve_rule(self.root, self.candidate_id, reviewer="user", reason="批准")
        rule_id = list_rules(self.root)["rules"][0]["rule_id"]
        result = deprecate_rule(
            self.root,
            rule_id,
            reviewer="user",
            reason="该规则不再适用于新版内容结构",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "deprecated")
        rules = list_rules(self.root)["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["status"], "deprecated")

    def test_human_activation_is_bound_to_exact_rule_revision(self) -> None:
        approved = approve_rule(
            self.root, self.candidate_id, reviewer="approver", reason="evidence accepted"
        )
        result = activate_rule(
            self.root,
            approved["rule_id"],
            reviewer="activator",
            reason="enable as planner advice",
            application_mode="advisory",
        )
        self.assertEqual(result["status"], "active")
        rule = json.loads(Path(approved["rule_file"]).read_text(encoding="utf-8"))
        self.assertTrue(rule["active"])
        self.assertEqual(rule["activation"]["reviewer"], "activator")
        self.assertEqual(rule["activation"]["rule_id"], approved["rule_id"])
        self.assertEqual(rule["activation"]["rule_revision"], 1)
        self.assertEqual(rule["activation"]["application_mode"], "advisory")
        self.assertEqual(rule["activation"]["review_id"], result["review_id"])
        self.assertEqual(validate_rule_integrity(self.root, rule), [])

        forged = deepcopy(rule)
        forged["activation"]["reviewer"] = "forged-activator"
        errors = validate_rule_integrity(self.root, forged)
        self.assertTrue(any("activation reviewer" in item for item in errors))

    def test_activation_rejects_hard_mode_and_direct_active_flag(self) -> None:
        approved = approve_rule(
            self.root, self.candidate_id, reviewer="approver", reason="evidence accepted"
        )
        with self.assertRaises(ApprovalError):
            activate_rule(
                self.root,
                approved["rule_id"],
                reviewer="activator",
                reason="invalid mandatory request",
                application_mode="hard",
            )
        rule_path = Path(approved["rule_file"])
        forged = json.loads(rule_path.read_text(encoding="utf-8"))
        forged["status"] = "active"
        forged["active"] = True
        errors = validate_rule_integrity(self.root, forged)
        self.assertTrue(any("lifecycle" in item or "activation" in item for item in errors))

    def test_deactivation_preserves_audited_history(self) -> None:
        approved = approve_rule(
            self.root, self.candidate_id, reviewer="approver", reason="evidence accepted"
        )
        activate_rule(
            self.root,
            approved["rule_id"],
            reviewer="activator",
            reason="enable advisory",
        )
        result = deactivate_rule(
            self.root,
            approved["rule_id"],
            reviewer="activator",
            reason="pause rule",
        )
        self.assertEqual(result["status"], "inactive")
        rule = json.loads(Path(approved["rule_file"]).read_text(encoding="utf-8"))
        self.assertFalse(rule["active"])
        self.assertNotIn("activation", rule)
        self.assertEqual(
            [item["event"] for item in rule["lifecycle"]["history"]],
            ["approve", "activate", "deactivate"],
        )
        self.assertEqual(validate_rule_integrity(self.root, rule), [])

    def test_dry_run_zero_writes(self) -> None:
        result = approve_rule(
            self.root,
            self.candidate_id,
            reviewer="user",
            reason="批准",
            dry_run=True,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(list((self.root / "editing_rules").glob("*.json"))), 0)
        self.assertEqual(len(list((self.root / "reviews").glob("*.json"))), 0)
        self.assertEqual(self._load_candidate()["status"], "candidate")
        manifest = load_manifest(self.root)
        self.assertEqual(manifest["counts"]["editing_rules"], 0)
        self.assertEqual(manifest["counts"]["reviews"], 0)

    def test_review_records_immutable(self) -> None:
        approve_rule(self.root, self.candidate_id, reviewer="user", reason="批准")
        review_path = list((self.root / "reviews").glob("*.json"))[0]
        original = review_path.read_bytes()
        # Write would fail on a second identical review id; verify the file is untouched.
        from video_os_core.rule_approval import ReviewRecordExistsError, write_review_record

        payload = json.loads(review_path.read_text(encoding="utf-8"))
        with self.assertRaises(ReviewRecordExistsError):
            write_review_record(self.root, payload)
        self.assertEqual(review_path.read_bytes(), original)

    def test_manifest_counts_correct(self) -> None:
        approve_rule(self.root, self.candidate_id, reviewer="user", reason="批准")
        manifest = load_manifest(self.root)
        self.assertEqual(manifest["counts"]["rule_candidates"], 1)
        self.assertEqual(manifest["counts"]["editing_rules"], 1)
        self.assertEqual(manifest["counts"]["reviews"], 1)

    def test_candidate_schema_valid_after_extraction(self) -> None:
        candidate = self._load_candidate()
        self.assertEqual(validate_rule_candidate(candidate), [])

    def test_review_candidate_shows_full_context(self) -> None:
        context = review_candidate(self.root, self.candidate_id)
        self.assertTrue(context["ok"])
        self.assertEqual(context["candidate_id"], self.candidate_id)
        self.assertEqual(context["evidence_count"], 2)
        self.assertEqual(context["weighted_evidence"], 2.0)
        self.assertTrue(context["source_valid"])
        self.assertTrue(context["recompute_matches"])
        self.assertTrue(context["confidence_recomputable"])
        self.assertIn("approve", context["available_decisions"])
        self.assertIn("reject", context["available_decisions"])
        self.assertIn("defer", context["available_decisions"])


if __name__ == "__main__":
    unittest.main()
