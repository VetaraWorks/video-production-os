from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import init_knowledge  # noqa: E402
from video_os_core.rule_approval import (  # noqa: E402
    approve_rule,
    defer_candidate,
    reject_candidate,
    reopen_candidate,
)
from video_os_core.rule_candidates import extract_rule_candidates  # noqa: E402
from governance_fixtures import write_production_evidence  # noqa: E402


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_feedback(project: str, version: str, feedback_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2, "evidence_tier": "production_verified",
        "feedback_id": feedback_id, "project": project,
        "from_version": "v001", "to_version": version, "collector": "manual",
        "collected_at": "2026-08-05T00:00:00+00:00",
        "source_docs": ["review.json"],
        "snapshot_refs": [f"projects/{project}/snapshots/{version}"],
        "changes": [{
            "change_id": "c-1", "category": "timing", "rule_class": "editing",
            "target": {"kind": "whole_video"},
            "before": {"description": "x", "metric": {"name": "product_first_appearance_s", "value": 22.0}},
            "after": {"description": "y", "metric": {"name": "product_first_appearance_s", "value": 8.0}},
            "reason": "test", "status": "pending", "source_docs": ["review.json"],
        }],
    }


class ReopenCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="reopen-test-")
        self.root = Path(self._tmp.name) / "knowledge"
        init_knowledge(self.root)
        write_production_evidence(
            self.root, project_id="project-demo", project="demo", run_id="v002", evidence_id="evidence-a"
        )
        write_production_evidence(
            self.root, project_id="project-demo2", project="demo2", run_id="v003", evidence_id="evidence-b"
        )
        extract_rule_candidates(self.root)
        files = list((self.root / "rule_candidates").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.candidate_id = files[0].stem

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejected_direct_approve_fails(self) -> None:
        reject_candidate(self.root, self.candidate_id, reviewer="user", reason="证据不足")
        result = approve_rule(
            self.root, self.candidate_id, reviewer="user", reason="再试试"
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("reopen-candidate" in r for r in result["reasons"]))

    def test_deferred_direct_approve_fails(self) -> None:
        defer_candidate(self.root, self.candidate_id, reviewer="user", reason="等待更多项目")
        result = approve_rule(
            self.root, self.candidate_id, reviewer="user", reason="再试试"
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("reopen-candidate" in r for r in result["reasons"]))

    def test_reopen_then_approve_succeeds(self) -> None:
        reject_candidate(self.root, self.candidate_id, reviewer="user", reason="证据不足")
        reopened = reopen_candidate(
            self.root, self.candidate_id, reviewer="user", reason="新证据表明适用"
        )
        self.assertTrue(reopened["ok"])
        self.assertEqual(reopened["new_status"], "reviewing")
        approved = approve_rule(
            self.root, self.candidate_id, reviewer="user", reason="新证据验证通过"
        )
        self.assertTrue(approved["ok"])
        self.assertIn("rule_id", approved)

    def test_review_chain_complete(self) -> None:
        reject_candidate(self.root, self.candidate_id, reviewer="user", reason="证据不足")
        reopen_candidate(self.root, self.candidate_id, reviewer="user", reason="重新评估")
        approve_rule(self.root, self.candidate_id, reviewer="user", reason="新证据验证通过")
        reviews = sorted((self.root / "reviews").glob("*.json"))
        self.assertEqual(len(reviews), 3)
        decisions = []
        for path in reviews:
            decisions.append(json.loads(path.read_text(encoding="utf-8"))["decision"])
        self.assertEqual(decisions, ["reject", "reopen", "approve"])

    def test_reopen_preserves_history(self) -> None:
        reject_candidate(self.root, self.candidate_id, reviewer="user", reason="原始拒绝理由")
        reopen_candidate(self.root, self.candidate_id, reviewer="user", reason="重新评估")
        reviews = sorted((self.root / "reviews").glob("*.json"))
        self.assertEqual(len(reviews), 2)
        first = json.loads(reviews[0].read_text(encoding="utf-8"))
        self.assertEqual(first["decision"], "reject")
        self.assertEqual(first["reason"], "原始拒绝理由")

    def test_reopen_on_active_candidate_fails(self) -> None:
        with self.assertRaises(Exception):
            reopen_candidate(
                self.root, self.candidate_id, reviewer="user", reason="不该重开"
            )


if __name__ == "__main__":
    unittest.main()
