from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import init_knowledge  # noqa: E402
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


class RuleApprovalCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rule-approval-cli-")
        self.root = Path(self._tmp.name) / "knowledge"
        init_knowledge(self.root)
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
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.env = env
        self.cli = str(ROOT / "scripts" / "knowledge_tools.py")
        result = self._run("extract-rules")
        self.assertEqual(result["candidate_count"], 1)
        self.candidate_id = list((self.root / "rule_candidates").glob("*.json"))[0].stem

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> dict[str, Any]:
        result = subprocess.run(
            [
                sys.executable,
                self.cli,
                "--root",
                str(self.root),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_review_and_approve_via_cli(self) -> None:
        review = self._run("review-candidate", self.candidate_id)
        self.assertTrue(review["ok"])
        self.assertEqual(review["candidate_id"], self.candidate_id)
        self.assertEqual(review["evidence_count"], 2)
        self.assertIn("approve", review["available_decisions"])

        approved = self._run(
            "approve-rule",
            self.candidate_id,
            "--reviewer",
            "user",
            "--reason",
            "两个独立项目支持",
            "--video-type",
            "口播种草",
        )
        self.assertTrue(approved["ok"])
        self.assertFalse(approved["idempotent"])

        rules = self._run("list-rules")
        self.assertEqual(rules["rule_count"], 1)
        self.assertEqual(rules["rules"][0]["status"], "inactive")
        self.assertEqual(rules["rules"][0]["scope"]["video_type"], "口播种草")

        explained = self._run("explain-rule", rules["rules"][0]["rule_id"])
        self.assertTrue(explained["ok"])
        self.assertEqual(explained["rule"]["source_candidate_id"], self.candidate_id)
        self.assertIsNotNone(explained["review"])

    def test_dry_run_zero_writes_via_cli(self) -> None:
        dry = self._run(
            "approve-rule",
            self.candidate_id,
            "--reviewer",
            "user",
            "--reason",
            "批准",
            "--dry-run",
        )
        self.assertTrue(dry["ok"])
        self.assertTrue(dry["dry_run"])
        self.assertEqual(len(list((self.root / "editing_rules").glob("*.json"))), 0)
        self.assertEqual(len(list((self.root / "reviews").glob("*.json"))), 0)

    def test_reject_via_cli(self) -> None:
        result = self._run(
            "reject-candidate",
            self.candidate_id,
            "--reviewer",
            "user",
            "--reason",
            "证据来自同一项目",
            "--rejection-category",
            "over_generalization",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(
            (self.root / "rule_candidates" / f"{self.candidate_id}.json").is_file()
        )
        self.assertEqual(len(list((self.root / "reviews").glob("*.json"))), 1)

    def test_deprecate_via_cli(self) -> None:
        self._run(
            "approve-rule",
            self.candidate_id,
            "--reviewer",
            "user",
            "--reason",
            "批准",
        )
        rule_id = self._run("list-rules")["rules"][0]["rule_id"]
        result = self._run(
            "deprecate-rule",
            rule_id,
            "--reviewer",
            "user",
            "--reason",
            "不再适用",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "deprecated")
        rules = self._run("list-rules")
        self.assertEqual(rules["rules"][0]["status"], "deprecated")

    def test_activate_and_deactivate_via_cli(self) -> None:
        self._run(
            "approve-rule",
            self.candidate_id,
            "--reviewer",
            "approver",
            "--reason",
            "approve evidence",
        )
        rule_id = self._run("list-rules")["rules"][0]["rule_id"]
        activated = self._run(
            "activate-rule",
            rule_id,
            "--reviewer",
            "activator",
            "--reason",
            "enable planner advisory",
            "--application-mode",
            "advisory",
        )
        self.assertEqual(activated["status"], "active")
        explained = self._run("explain-rule", rule_id)
        self.assertTrue(explained["integrity_valid"])
        self.assertEqual(explained["rule"]["activation"]["application_mode"], "advisory")

        deactivated = self._run(
            "deactivate-rule",
            rule_id,
            "--reviewer",
            "activator",
            "--reason",
            "pause advisory",
        )
        self.assertEqual(deactivated["status"], "inactive")

    def test_revoke_via_cli(self) -> None:
        self._run(
            "approve-rule",
            self.candidate_id,
            "--reviewer",
            "user",
            "--reason",
            "批准",
        )
        rule_id = self._run("list-rules")["rules"][0]["rule_id"]
        result = self._run(
            "revoke-rule",
            rule_id,
            "--reviewer",
            "user",
            "--reason",
            "规则存在风险",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "revoked")


if __name__ == "__main__":
    unittest.main()
