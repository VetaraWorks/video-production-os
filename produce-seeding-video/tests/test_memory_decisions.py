from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.decision_log import (  # noqa: E402
    DecisionError,
    list_decisions,
    record_decision,
)
from video_os_core.knowledge import init_knowledge  # noqa: E402
from video_os_core.memory_suggestions import (  # noqa: E402
    generate_memory_suggestions,
    write_suggestion_report,
)
from governance_fixtures import install_formal_rule  # noqa: E402


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_project(base: Path) -> Path:
    project = base / "demo"
    (project / "script").mkdir(parents=True, exist_ok=True)
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "output").mkdir(parents=True, exist_ok=True)
    (project / "script" / "script.txt").write_text("hello\n", encoding="utf-8")
    write_json(
        project / "config" / "config.json",
        {"canvas": {"width": 360, "height": 640, "fps": 24}, "duration_seconds": 60.0},
    )
    write_json(
        project / "config" / "project_context.json",
        {
            "video_type": "口播种草",
            "client": None,
            "style_profile": None,
            "platform": "抖音",
            "available_metrics": {"product_first_appearance_s": 10.5},
        },
    )
    write_json(
        project / "output" / "edit_plan.json",
        {"duration_seconds": 60.0, "segments": [{"id": "hook", "duration": 3.0}]},
    )
    write_json(project / "project_state.json", {"stage": "PLAN"})
    return project


def build_rule(rule_id: str = "r-inactive") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "rule_id": rule_id,
        "source_candidate_id": f"cand-{rule_id}",
        "rule_class": "editing",
        "category": "timing",
        "rule_type": "timing",
        "scope": {"video_type": "口播种草", "client": None, "style_profile": None},
        "expression": {
            "metric": "product_first_appearance_s",
            "operator": "<=",
            "value": 8,
        },
        "description": "产品首次出现不晚于 8 秒",
        "status": "inactive",
        "confidence_at_approval": 0.82,
        "evidence_snapshot": [
            {
                "kind": "feedback",
                "ref": "fb-a",
                "snapshot_ref": "projects/demo/snapshots/v002",
                "project": "demo",
                "version": "v002",
                "source_file": "fb-a.json",
            }
        ],
        "approval": {
            "review_id": "review-1",
            "reviewer": "user",
            "reason": "产品展示过晚会降低开场吸引力",
        },
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
        "evidence_status": "valid",
    }


class MemoryDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-decision-test-")
        self.base = Path(self._tmp.name)
        self.knowledge_root = self.base / "knowledge"
        init_knowledge(self.knowledge_root)
        self.rules_dir = self.knowledge_root / "editing_rules"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.project = make_project(self.base)
        self.rule = install_formal_rule(
            self.knowledge_root,
            rule_key="r-inactive",
            expression={
                "metric": "product_first_appearance_s",
                "operator": "<=",
                "value": 8,
            },
            scope={"video_type": "口播种草", "client": None, "style_profile": None},
        )
        self.suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.suggestion_id = self.suggestion["suggestions"][0]["suggestion_id"]
        self.plan_before = (self.project / "output" / "edit_plan.json").read_bytes()
        self.state_before = (self.project / "project_state.json").read_bytes()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_accepted_recorded_and_traceable(self) -> None:
        result = record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="accepted",
            reviewer="user",
            reason="符合种草节奏",
        )
        self.assertTrue(result["ok"])
        record = result["decision"]
        self.assertEqual(record["decision"], "accepted")
        self.assertEqual(record["suggestion_id"], self.suggestion_id)
        self.assertEqual(record["reviewer"], {"type": "human", "name": "user"})
        self.assertEqual(record["reason"], "符合种草节奏")
        self.assertEqual(record["original_suggestion"]["rule_id"], self.rule["rule_id"])
        self.assertIsNotNone(record["recorded_at"])

    def test_all_decision_types_recordable(self) -> None:
        for decision, modified in (
            ("accepted", None),
            ("rejected", None),
            ("modified", {"product_first_appearance_s": 6.0}),
            ("deferred", None),
        ):
            result = record_decision(
                self.project,
                self.knowledge_root,
                suggestion_id=self.suggestion_id,
                decision=decision,
                reviewer="user",
                reason=f"decision {decision}",
                modified_value=modified,
            )
            self.assertTrue(result["ok"], decision)
            self.assertEqual(result["decision"]["decision"], decision)

    def test_original_suggestion_unchanged(self) -> None:
        suggestion_before = json.dumps(
            self.suggestion, sort_keys=True, ensure_ascii=False
        )
        record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="rejected",
            reviewer="user",
            reason="品牌片需要铺垫",
        )
        suggestion_after = json.dumps(
            generate_memory_suggestions(self.project, self.knowledge_root),
            sort_keys=True,
            ensure_ascii=False,
        )
        self.assertEqual(suggestion_after, suggestion_before)

    def test_decision_log_queryable(self) -> None:
        record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="accepted",
            reviewer="user",
            reason="符合节奏",
        )
        listing = list_decisions(self.project)
        self.assertEqual(listing["decision_count"], 1)
        self.assertEqual(listing["decisions"][0]["decision"], "accepted")

    def test_edit_plan_and_state_unchanged(self) -> None:
        record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="accepted",
            reviewer="user",
            reason="测试",
        )
        self.assertEqual(
            (self.project / "output" / "edit_plan.json").read_bytes(),
            self.plan_before,
        )
        self.assertEqual(
            (self.project / "project_state.json").read_bytes(),
            self.state_before,
        )

    def test_missing_reviewer_or_reason_rejected(self) -> None:
        with self.assertRaises(DecisionError):
            record_decision(
                self.project,
                self.knowledge_root,
                suggestion_id=self.suggestion_id,
                decision="accepted",
                reviewer="",
                reason="测试",
            )
        with self.assertRaises(DecisionError):
            record_decision(
                self.project,
                self.knowledge_root,
                suggestion_id=self.suggestion_id,
                decision="accepted",
                reviewer="user",
                reason="",
            )

    def test_modified_requires_value(self) -> None:
        with self.assertRaises(DecisionError):
            record_decision(
                self.project,
                self.knowledge_root,
                suggestion_id=self.suggestion_id,
                decision="modified",
                reviewer="user",
                reason="调整",
            )

    def test_dry_run_zero_writes(self) -> None:
        result = record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="accepted",
            reviewer="user",
            reason="测试",
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        listing = list_decisions(self.project)
        self.assertEqual(listing["decision_count"], 0)

    def test_unknown_suggestion_rejected(self) -> None:
        with self.assertRaises(DecisionError):
            record_decision(
                self.project,
                self.knowledge_root,
                suggestion_id="sugg-nonexistent",
                decision="accepted",
                reviewer="user",
                reason="测试",
            )

    def test_append_only_multiple_decisions(self) -> None:
        record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="accepted",
            reviewer="user",
            reason="第一次",
        )
        record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="rejected",
            reviewer="user",
            reason="第二次重新评估",
        )
        listing = list_decisions(self.project)
        self.assertEqual(listing["decision_count"], 2)
        self.assertEqual(
            [item["decision"] for item in listing["decisions"]],
            ["accepted", "rejected"],
        )

    def test_decision_referencing_rule_and_project(self) -> None:
        result = record_decision(
            self.project,
            self.knowledge_root,
            suggestion_id=self.suggestion_id,
            decision="accepted",
            reviewer="user",
            reason="测试",
        )
        record = result["decision"]
        self.assertEqual(record["rule_id"], self.rule["rule_id"])
        self.assertEqual(record["project"], "demo")


class MemoryDecisionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-decision-cli-")
        self.base = Path(self._tmp.name)
        self.knowledge_root = self.base / "knowledge"
        init_knowledge(self.knowledge_root)
        self.rules_dir = self.knowledge_root / "editing_rules"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.project = make_project(self.base)
        install_formal_rule(
            self.knowledge_root,
            rule_key="r-inactive",
            expression={
                "metric": "product_first_appearance_s",
                "operator": "<=",
                "value": 8,
            },
            scope={"video_type": "口播种草", "client": None, "style_profile": None},
        )
        self.suggestion_id = generate_memory_suggestions(
            self.project, self.knowledge_root
        )["suggestions"][0]["suggestion_id"]
        self.cli = str(ROOT / "scripts" / "video_os.py")
        import os

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.env = env

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, self.cli, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_cli_decide_and_list(self) -> None:
        result = self._run(
            "memory-decide",
            str(self.project),
            "--knowledge-root",
            str(self.knowledge_root),
            "--suggestion-id",
            self.suggestion_id,
            "--decision",
            "accepted",
            "--reviewer",
            "user",
            "--reason",
            "符合种草节奏",
        )
        self.assertTrue(result["ok"])
        listing = self._run("memory-decisions", str(self.project))
        self.assertEqual(listing["decision_count"], 1)
        self.assertEqual(listing["decisions"][0]["decision"], "accepted")

    def test_cli_modified_with_value(self) -> None:
        result = self._run(
            "memory-decide",
            str(self.project),
            "--knowledge-root",
            str(self.knowledge_root),
            "--suggestion-id",
            self.suggestion_id,
            "--decision",
            "modified",
            "--reviewer",
            "user",
            "--reason",
            "调整阈值",
            "--modified-value",
            '{"product_first_appearance_s": 6.0}',
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["decision"]["modified_value"], {"product_first_appearance_s": 6.0}
        )

    def test_cli_dry_run_zero_writes(self) -> None:
        self._run(
            "memory-decide",
            str(self.project),
            "--knowledge-root",
            str(self.knowledge_root),
            "--suggestion-id",
            self.suggestion_id,
            "--decision",
            "accepted",
            "--reviewer",
            "user",
            "--reason",
            "测试",
            "--dry-run",
        )
        listing = self._run("memory-decisions", str(self.project))
        self.assertEqual(listing["decision_count"], 0)


if __name__ == "__main__":
    unittest.main()
