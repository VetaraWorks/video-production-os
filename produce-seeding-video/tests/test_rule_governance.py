from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from governance_fixtures import write_production_evidence  # noqa: E402
from video_os_core.decision_log import (  # noqa: E402
    DecisionError,
    list_governance_history,
    record_decision,
)
from video_os_core.knowledge import init_knowledge  # noqa: E402
from video_os_core.memory_reader import load_rules  # noqa: E402
from video_os_core.memory_suggestions import (  # noqa: E402
    generate_memory_suggestions,
    validate_suggestion_snapshot,
)
from video_os_core.rule_approval import (  # noqa: E402
    approve_rule,
    deprecate_rule,
    reject_candidate,
    revoke_rule,
)
from video_os_core.rule_candidates import extract_rule_candidates  # noqa: E402


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_project(base: Path, name: str, project_id: str) -> Path:
    project = base / name
    (project / "script").mkdir(parents=True)
    (project / "raw_video").mkdir(parents=True)
    (project / "config").mkdir(parents=True)
    (project / "output").mkdir(parents=True)
    (project / "script" / "script.txt").write_text("真实项目脚本\n", encoding="utf-8")
    (project / "raw_video" / "source.mp4").write_bytes(b"video-input-v1")
    write_json(
        project / "config" / "config.json",
        {"duration_seconds": 4.0, "canvas": {"width": 360, "height": 640, "fps": 24}},
    )
    write_json(
        project / "config" / "project_context.json",
        {
            "video_type": None,
            "client": None,
            "style_profile": None,
            "platform": "抖音",
            "available_metrics": {"product_first_appearance_s": 10.5},
        },
    )
    write_json(
        project / "output" / "edit_plan.json",
        {
            "duration_seconds": 4.0,
            "segments": [
                {"id": "hook", "duration": 2.0, "timeline_start": 0.0, "timeline_end": 2.0},
                {"id": "product", "duration": 2.0, "timeline_start": 2.0, "timeline_end": 4.0},
            ],
        },
    )
    write_json(
        project / "project_state.json",
        {"project_id": project_id, "project": name, "created_at": "2026-08-09T00:00:00Z", "stage": "PLAN"},
    )
    return project


class RuleGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rule-governance-")
        self.base = Path(self._tmp.name)
        self.knowledge = self.base / "knowledge"
        init_knowledge(self.knowledge)
        self._write_evidence()
        result = extract_rule_candidates(self.knowledge)
        self.assertEqual(result["candidate_count"], 1)
        candidates = list((self.knowledge / "rule_candidates").glob("*.json"))
        self.candidate_path = candidates[0]
        self.candidate_id = candidates[0].stem

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_evidence(self, value: float = 8.0) -> None:
        write_production_evidence(
            self.knowledge,
            project_id="project-evidence-a",
            project="evidence-a",
            run_id="run-a",
            evidence_id="evidence-a",
            value=value,
        )
        write_production_evidence(
            self.knowledge,
            project_id="project-evidence-b",
            project="evidence-b",
            run_id="run-b",
            evidence_id="evidence-b",
            value=value,
        )

    def _approve(self) -> dict:
        result = approve_rule(
            self.knowledge,
            self.candidate_id,
            reviewer="human-reviewer",
            reason="two independent production-verified projects support this rule",
        )
        self.assertTrue(result["ok"], result)
        return result

    def test_two_project_accept_reject_governance_loop_is_l0_only(self) -> None:
        approved = self._approve()
        rule_path = Path(approved["rule_file"])
        rule_before = rule_path.read_bytes()
        rule = json.loads(rule_before)
        self.assertEqual(rule["status"], "inactive")
        self.assertFalse(rule["active"])

        project_a = make_project(self.base, "decision-a", "project-decision-a")
        project_b = make_project(self.base, "decision-b", "project-decision-b")
        plan_a = (project_a / "output" / "edit_plan.json").read_bytes()
        plan_b = (project_b / "output" / "edit_plan.json").read_bytes()

        suggestion_a = generate_memory_suggestions(project_a, self.knowledge)["suggestions"][0]
        self.assertEqual(suggestion_a["rule_revision"], 1)
        self.assertTrue(suggestion_a["match_positions"])
        self.assertTrue(suggestion_a["evidence_summary"]["evidence_ids"])
        accepted = record_decision(
            project_a,
            self.knowledge,
            suggestion_id=suggestion_a["suggestion_id"],
            decision="accept",
            reviewer="human-a",
            reason="fits this project",
        )
        self.assertFalse(accepted["idempotent"])
        duplicate = record_decision(
            project_a,
            self.knowledge,
            suggestion_id=suggestion_a["suggestion_id"],
            decision="accept",
            reviewer="human-a",
            reason="fits this project",
        )
        self.assertTrue(duplicate["idempotent"])

        suggestion_b = generate_memory_suggestions(project_b, self.knowledge)["suggestions"][0]
        record_decision(
            project_b,
            self.knowledge,
            suggestion_id=suggestion_b["suggestion_id"],
            decision="reject",
            reviewer="human-b",
            reason="not suitable for this project",
        )
        history = list_governance_history(self.knowledge, rule_id=rule["rule_id"])
        self.assertTrue(history["ok"], history)
        self.assertEqual(history["decision_count"], 2)
        self.assertEqual(history["rules"][0]["accept"], 1)
        self.assertEqual(history["rules"][0]["reject"], 1)
        manifest = json.loads((self.knowledge / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["governance_history"], 2)
        self.assertEqual(rule_path.read_bytes(), rule_before)
        self.assertEqual((project_a / "output" / "edit_plan.json").read_bytes(), plan_a)
        self.assertEqual((project_b / "output" / "edit_plan.json").read_bytes(), plan_b)

    def test_candidate_change_invalidates_old_review_and_rule(self) -> None:
        self._approve()
        candidate = json.loads(self.candidate_path.read_text(encoding="utf-8"))
        candidate["expression"]["value"] = 7.0
        write_json(self.candidate_path, candidate)
        rules, invalid = load_rules(self.knowledge)
        self.assertEqual(rules, [])
        self.assertTrue(any("candidate content hash" in " ".join(item["errors"]) for item in invalid))

    def test_evidence_or_rule_tamper_fails_closed(self) -> None:
        approved = self._approve()
        evidence_path = self.knowledge / "repair_log" / "evidence-a.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["actions"][0]["value"] = 6.0
        write_json(evidence_path, evidence)
        self.assertEqual(load_rules(self.knowledge)[0], [])

        self._write_evidence()
        rule_path = Path(approved["rule_file"])
        rule = json.loads(rule_path.read_text(encoding="utf-8"))
        rule["expression"]["value"] = 6.0
        write_json(rule_path, rule)
        self.assertEqual(load_rules(self.knowledge)[0], [])

    def test_forged_approved_or_rejected_candidate_cannot_generate_rule(self) -> None:
        candidate = json.loads(self.candidate_path.read_text(encoding="utf-8"))
        candidate["status"] = "approved"
        write_json(self.candidate_path, candidate)
        forged = approve_rule(
            self.knowledge,
            self.candidate_id,
            reviewer="human",
            reason="must not trust forged status",
        )
        self.assertFalse(forged["ok"])

        candidate["status"] = "candidate"
        write_json(self.candidate_path, candidate)
        reject_candidate(
            self.knowledge,
            self.candidate_id,
            reviewer="human",
            reason="reject candidate",
        )
        review = json.loads(
            next((self.knowledge / "reviews").glob("*.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(review["candidate_revision"], 1)
        self.assertEqual(len(review["candidate_content_hash"]), 64)
        self.assertEqual(len(review["evidence_snapshot"]), 2)
        self.assertEqual(len(review["review_hash"]["sha256"]), 64)
        rejected = approve_rule(
            self.knowledge,
            self.candidate_id,
            reviewer="human",
            reason="must not approve rejected candidate",
        )
        self.assertFalse(rejected["ok"])

    def test_new_candidate_revision_creates_stable_rule_revision_and_stales_old_suggestion(self) -> None:
        first = self._approve()
        project = make_project(self.base, "revision", "project-revision")
        old_suggestion = generate_memory_suggestions(project, self.knowledge)["suggestions"][0]

        self._write_evidence(value=6.0)
        extracted = extract_rule_candidates(self.knowledge)
        self.assertEqual(extracted["revisions_created"], 1)
        revision_path = next(
            path
            for path in (self.knowledge / "rule_candidates").glob("*.json")
            if path != self.candidate_path
        )
        second = approve_rule(
            self.knowledge,
            revision_path.stem,
            reviewer="human-reviewer-2",
            reason="new production evidence supports a stricter threshold",
            conflict_resolution="new revision explicitly supersedes the prior threshold",
        )
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["rule_id"], first["rule_id"])
        self.assertEqual(second["rule_revision"], 2)
        second_rule = json.loads(Path(second["rule_file"]).read_text(encoding="utf-8"))
        self.assertEqual(second_rule["supersedes"], {"rule_id": first["rule_id"], "revision": 1})
        current = generate_memory_suggestions(project, self.knowledge)["suggestions"][0]
        self.assertEqual(current["rule_revision"], 2)
        self.assertNotEqual(current["suggestion_id"], old_suggestion["suggestion_id"])
        with self.assertRaises(DecisionError):
            record_decision(
                project,
                self.knowledge,
                suggestion_id=old_suggestion["suggestion_id"],
                decision="accept",
                reviewer="human",
                reason="old rule revision must be stale",
            )

    def test_legacy_or_migrated_evidence_cannot_create_formal_rule(self) -> None:
        legacy_root = self.base / "legacy" / "knowledge"
        init_knowledge(legacy_root)
        for suffix, project in (("a", "legacy-a"), ("b", "legacy-b")):
            write_json(
                legacy_root / "edits" / f"feedback-{suffix}.json",
                {
                    "schema_version": 2,
                    "evidence_tier": "production_verified",
                    "feedback_id": f"feedback-{suffix}",
                    "project": project,
                    "from_version": "v1",
                    "to_version": "v2",
                    "collector": "migrated",
                    "collected_at": "2026-08-09T00:00:00Z",
                    "source_docs": ["legacy.md"],
                    "snapshot_refs": [f"legacy/{project}"],
                    "changes": [
                        {
                            "change_id": f"change-{suffix}",
                            "category": "timing",
                            "rule_class": "editing",
                            "target": {"kind": "whole_video"},
                            "before": {"metric": {"name": "legacy_metric", "value": 10}},
                            "after": {"metric": {"name": "legacy_metric", "value": 8}},
                            "reason": "legacy migration",
                            "status": "pending",
                        }
                    ],
                },
            )
        extract_rule_candidates(legacy_root)
        legacy_candidate = next((legacy_root / "rule_candidates").glob("*.json")).stem
        denied = approve_rule(
            legacy_root,
            legacy_candidate,
            reviewer="human",
            reason="must not formalize migrated data",
        )
        self.assertFalse(denied["ok"])
        self.assertIn("formal rule evidence", " ".join(denied["reasons"]))
        self.assertEqual(list((legacy_root / "editing_rules").glob("*.json")), [])

    def test_project_or_plan_change_makes_suggestion_stale(self) -> None:
        self._approve()
        project = make_project(self.base, "stale", "project-stale")
        suggestion = generate_memory_suggestions(project, self.knowledge)["suggestions"][0]
        plan = json.loads((project / "output" / "edit_plan.json").read_text(encoding="utf-8"))
        plan["segments"][0]["duration"] = 1.5
        write_json(project / "output" / "edit_plan.json", plan)
        self.assertTrue(validate_suggestion_snapshot(project, self.knowledge, suggestion))
        with self.assertRaises(DecisionError):
            record_decision(
                project,
                self.knowledge,
                suggestion_id=suggestion["suggestion_id"],
                decision="accept",
                reviewer="human",
                reason="stale plan",
            )

        current = generate_memory_suggestions(project, self.knowledge)["suggestions"][0]
        (project / "script" / "script.txt").write_text("输入已变化\n", encoding="utf-8")
        self.assertTrue(validate_suggestion_snapshot(project, self.knowledge, current))

    def test_deprecated_and_revoked_rules_emit_no_new_suggestions(self) -> None:
        approved = self._approve()
        project = make_project(self.base, "lifecycle", "project-lifecycle")
        self.assertEqual(len(generate_memory_suggestions(project, self.knowledge)["suggestions"]), 1)
        deprecate_rule(
            self.knowledge,
            approved["rule_id"],
            reviewer="human",
            reason="superseded policy",
        )
        self.assertEqual(generate_memory_suggestions(project, self.knowledge)["suggestions"], [])

        # A fresh fixture proves revoke independently from deprecate.
        other_base = self.base / "revoke-fixture"
        other_knowledge = other_base / "knowledge"
        init_knowledge(other_knowledge)
        write_production_evidence(other_knowledge, project_id="pa", project="pa", run_id="ra", evidence_id="ea")
        write_production_evidence(other_knowledge, project_id="pb", project="pb", run_id="rb", evidence_id="eb")
        extract_rule_candidates(other_knowledge)
        candidate_id = next((other_knowledge / "rule_candidates").glob("*.json")).stem
        other_rule = approve_rule(other_knowledge, candidate_id, reviewer="human", reason="approve")
        revoke_rule(other_knowledge, other_rule["rule_id"], reviewer="human", reason="unsafe rule")
        self.assertEqual(generate_memory_suggestions(project, other_knowledge)["suggestions"], [])


if __name__ == "__main__":
    unittest.main()
