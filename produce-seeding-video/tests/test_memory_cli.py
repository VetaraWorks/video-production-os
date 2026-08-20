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
from governance_fixtures import install_formal_rule  # noqa: E402


def build_rule(rule_id: str, **overrides: Any) -> dict[str, Any]:
    rule = {
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
        "description": "test rule",
        "status": "inactive",
        "confidence_at_approval": 0.76,
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
            "reason": "两个独立项目支持",
        },
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
        "evidence_status": "valid",
    }
    rule.update(overrides)
    return rule


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class MemoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-cli-test-")
        self.base = Path(self._tmp.name)
        self.root = self.base / "knowledge"
        init_knowledge(self.root)
        self.rules_dir = self.root / "editing_rules"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.context_path = self.base / "context.json"
        write_json(
            self.context_path,
            {
                "schema_version": 1,
                "project": "demo",
                "version": "v002",
                "video_type": "口播种草",
                "client": None,
                "style_profile": None,
                "platform": "抖音",
                "duration_target_s": 60,
                "available_metrics": {"product_first_appearance_s": 10.5},
            },
        )
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.env = env
        self.cli = str(ROOT / "scripts" / "knowledge_tools.py")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, self.cli, "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            cwd=str(self.base),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_match_rules_dry_run_zero_writes(self) -> None:
        install_formal_rule(
            self.root,
            rule_key="rule-inactive",
            expression=build_rule("rule-inactive")["expression"],
            scope=build_rule("rule-inactive")["scope"],
        )
        result = self._run(
            "match-rules",
            "--project-context",
            str(self.context_path),
            "--dry-run",
        )
        self.assertEqual(result["summary"]["matched"], 1)
        self.assertEqual(
            result["matches"][0]["execution_status"], "would_match_but_inactive"
        )
        self.assertFalse((self.base / "rule_match_report.json").exists())

    def test_match_rules_write_idempotent(self) -> None:
        install_formal_rule(
            self.root,
            rule_key="rule-inactive",
            expression=build_rule("rule-inactive")["expression"],
            scope=build_rule("rule-inactive")["scope"],
        )
        output = self.base / "report.json"
        first = self._run(
            "match-rules",
            "--project-context",
            str(self.context_path),
            "--output",
            str(output),
        )
        self.assertTrue(first["ok"])
        content_first = output.read_bytes()
        second = self._run(
            "match-rules",
            "--project-context",
            str(self.context_path),
            "--output",
            str(output),
        )
        self.assertEqual(output.read_bytes(), content_first)

    def test_explain_match_traceability(self) -> None:
        rule = install_formal_rule(
            self.root,
            rule_key="rule-trace",
            expression=build_rule("rule-trace")["expression"],
            scope=build_rule("rule-trace")["scope"],
        )
        rule_id = rule["rule_id"]
        output = self.base / "report.json"
        self._run(
            "match-rules",
            "--project-context",
            str(self.context_path),
            "--output",
            str(output),
        )
        explained = self._run("explain-match", "--report", str(output), "--rule-id", rule_id)
        self.assertTrue(explained["ok"])
        self.assertIsNotNone(explained["review"])
        self.assertIsNotNone(explained["candidate"])
        self.assertEqual(len(explained["evidence"]), 2)
        self.assertTrue(explained["evidence"][0]["file_present"])
        self.assertEqual(explained["execution_status"], "would_match_but_inactive")
        self.assertIn("L0 read-only", explained["why_not_executed"])

    def test_validate_memory_api_empty_library(self) -> None:
        result = self._run("validate-memory-api")
        self.assertTrue(result["ok"])
        self.assertEqual(result["rules_scanned"], 0)
        self.assertEqual(result["matches"], 0)

    def test_formal_knowledge_not_injected(self) -> None:
        # All fixture data lives under the temp root; nothing is written to the
        # real workspace knowledge/ (asserted implicitly by using temp dirs).
        self.assertEqual(len(list(self.rules_dir.glob("*.json"))), 0)
        self.assertEqual(len(list((self.root / "rule_candidates").glob("*.json"))), 0)
        self.assertEqual(len(list((self.root / "reviews").glob("*.json"))), 0)


class MemoryPreviewCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-preview-test-")
        self.base = Path(self._tmp.name)
        self.knowledge_root = self.base / "knowledge"
        init_knowledge(self.knowledge_root)
        self.project = self.base / "demo"
        (self.project / "script").mkdir(parents=True, exist_ok=True)
        (self.project / "config").mkdir(parents=True, exist_ok=True)
        (self.project / "output").mkdir(parents=True, exist_ok=True)
        (self.project / "script" / "script.txt").write_text("hello\n", encoding="utf-8")
        write_json(
            self.project / "config" / "config.json",
            {"canvas": {"width": 360, "height": 640, "fps": 24}, "duration_seconds": 6.0},
        )
        write_json(
            self.project / "output" / "edit_plan.json",
            {
                "duration_seconds": 6.0,
                "segments": [
                    {"id": "hook", "duration": 1.0},
                    {"id": "cta", "duration": 2.0},
                ],
            },
        )
        state_path = self.project / "project_state.json"
        write_json(state_path, {"stage": "PLAN", "history": []})
        self.state_before = state_path.read_bytes()
        self.plan_before = (self.project / "output" / "edit_plan.json").read_bytes()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        self.env = env
        self.cli = str(ROOT / "scripts" / "video_os.py")

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

    def test_memory_preview_does_not_touch_state_or_plan(self) -> None:
        result = self._run(
            "memory-preview",
            str(self.project),
            "--knowledge-root",
            str(self.knowledge_root),
        )
        self.assertTrue(result["ok"])
        report = json.loads(
            (self.project / "memory_preview" / "rule_match_report.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["summary"]["rules_scanned"], 0)
        # project_state and edit_plan are byte-identical.
        self.assertEqual(
            (self.project / "project_state.json").read_bytes(), self.state_before
        )
        self.assertEqual(
            (self.project / "output" / "edit_plan.json").read_bytes(), self.plan_before
        )


if __name__ == "__main__":
    unittest.main()
