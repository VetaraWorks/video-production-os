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

from video_os_core.knowledge import init_knowledge  # noqa: E402
from video_os_core.memory_suggestions import (  # noqa: E402
    generate_memory_suggestions,
    write_suggestion_report,
)
from governance_fixtures import install_formal_rule  # noqa: E402


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_project(base: Path, *, with_plan: bool = True) -> Path:
    project = base / "demo"
    (project / "script").mkdir(parents=True, exist_ok=True)
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "output").mkdir(parents=True, exist_ok=True)
    (project / "script" / "script.txt").write_text("hello\nworld\n", encoding="utf-8")
    write_json(
        project / "config" / "config.json",
        {
            "canvas": {"width": 360, "height": 640, "fps": 24},
            "duration_seconds": 6.0,
        },
    )
    write_json(
        project / "config" / "project_context.json",
        {
            "video_type": "口播种草",
            "client": None,
            "style_profile": None,
            "platform": "抖音",
        },
    )
    if with_plan:
        write_json(
            project / "output" / "edit_plan.json",
            {
                "duration_seconds": 6.0,
                "segments": [
                    {"id": "hook", "duration": 1.0},
                    {"id": "cta", "duration": 2.0},
                ],
            },
        )
    write_json(project / "project_state.json", {"stage": "PLAN", "history": []})
    return project


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
        "description": "产品首次出现不晚于 8 秒",
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
            },
            {
                "kind": "feedback",
                "ref": "fb-b",
                "snapshot_ref": "projects/demo2/snapshots/v003",
                "project": "demo2",
                "version": "v003",
                "source_file": "fb-b.json",
            },
        ],
        "approval": {
            "review_id": "review-1",
            "reviewer": "user",
            "reason": "两个独立项目支持：产品展示过晚会降低开场吸引力",
        },
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
        "evidence_status": "valid",
    }
    rule.update(overrides)
    return rule


def context_with_metrics(project: Path, metrics: dict[str, float]) -> dict[str, Any]:
    context = {
        "schema_version": 1,
        "project": project.name,
        "version": "unreleased",
        "video_type": None,
        "client": None,
        "style_profile": None,
        "platform": None,
        "duration_target_s": 6.0,
        "available_metrics": metrics,
    }
    return context


class MemorySuggestionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-suggest-test-")
        self.base = Path(self._tmp.name)
        self.knowledge_root = self.base / "knowledge"
        init_knowledge(self.knowledge_root)
        self.rules_dir = self.knowledge_root / "editing_rules"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.project = make_project(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_rule(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return install_formal_rule(
            self.knowledge_root,
            rule_key=name,
            expression=payload["expression"],
            scope=payload.get("scope"),
            status=payload.get("status", "inactive"),
            evidence_status=payload.get("evidence_status", "valid"),
            confidence=float(payload.get("confidence_at_approval", 0.76)),
        )

    def test_empty_formal_knowledge_zero_suggestions(self) -> None:
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.assertEqual(suggestion["summary"]["rules_scanned"], 0)
        self.assertEqual(suggestion["summary"]["suggestions"], 0)
        self.assertEqual(suggestion["suggestions"], [])

    def test_inactive_rule_generates_advisory_suggestion(self) -> None:
        rule = self._add_rule("r-inactive", build_rule("r-inactive"))
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.assertEqual(suggestion["summary"]["suggestions"], 1)
        entry = suggestion["suggestions"][0]
        self.assertEqual(entry["rule_id"], rule["rule_id"])
        self.assertEqual(entry["execution_status"], "would_match_but_inactive")
        # product_first_appearance_s is not derivable from plan artifacts yet,
        # so compliance stays unknown (None), never guessed.
        self.assertIsNone(entry["compliance"])

    def test_suggestion_contains_reason_source_confidence(self) -> None:
        self._add_rule("r-inactive", build_rule("r-inactive"))
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        entry = suggestion["suggestions"][0]
        self.assertTrue(entry["reason"])
        self.assertEqual(len(entry["source_cases"]), 2)
        self.assertEqual(entry["source_cases"][0]["kind"], "production_evidence")
        self.assertTrue(entry["source_cases"][0]["evidence_id"])
        self.assertEqual(entry["confidence"], 0.76)
        self.assertIn("仅提示", entry["suggestion"])

    def test_not_matched_rule_not_suggested(self) -> None:
        self._add_rule(
            "r-other",
            build_rule("r-other", scope={"video_type": "国风宣传", "client": None, "style_profile": None}),
        )
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.assertEqual(suggestion["summary"]["not_matched"], 1)
        self.assertEqual(suggestion["summary"]["suggestions"], 0)

    def test_unknown_scope_not_suggested(self) -> None:
        self._add_rule("r-unknown", build_rule("r-unknown"))
        # Remove the explicit context declaration so video_type is unknown.
        (self.project / "config" / "project_context.json").unlink()
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.assertEqual(suggestion["summary"]["unknown"], 1)
        self.assertEqual(suggestion["summary"]["suggestions"], 0)

    def test_conflicted_rules_not_suggested(self) -> None:
        self._add_rule("r-ca", build_rule("r-ca"))
        self._add_rule(
            "r-cb",
            build_rule("r-cb", expression={"metric": "product_first_appearance_s", "operator": "<=", "value": 12}),
        )
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.assertEqual(suggestion["summary"]["conflicted"], 2)
        self.assertEqual(suggestion["summary"]["suggestions"], 0)

    def test_metric_compliance_observed_from_plan(self) -> None:
        # Build a plan-derived metric via edit_plan: average_clip_duration_s = 1.5
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
        self._add_rule(
            "r-duration",
            build_rule(
                "r-duration",
                expression={"metric": "average_clip_duration_s", "operator": "<=", "value": 1.5},
            ),
        )
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.assertEqual(suggestion["summary"]["suggestions"], 1)
        entry = suggestion["suggestions"][0]
        self.assertEqual(entry["observed_value"], 1.5)
        self.assertTrue(entry["compliance"])
        self.assertIn("已满足", entry["suggestion"])

    def test_compliance_false_suggests_adjustment(self) -> None:
        # Force observed value via direct context not needed: plan average is 1.5,
        # so craft a rule that fails: average_clip_duration_s >= 2.0
        self._add_rule(
            "r-duration2",
            build_rule(
                "r-duration2",
                expression={"metric": "average_clip_duration_s", "operator": ">=", "value": 2.0},
            ),
        )
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        entry = suggestion["suggestions"][0]
        self.assertFalse(entry["compliance"])
        self.assertIn("建议调整", entry["suggestion"])

    def test_declared_metric_read_from_context(self) -> None:
        write_json(
            self.project / "config" / "project_context.json",
            {
                "video_type": "口播种草",
                "client": None,
                "style_profile": None,
                "platform": "抖音",
                "available_metrics": {"product_first_appearance_s": 10.5},
            },
        )
        self._add_rule("r-inactive", build_rule("r-inactive"))
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        entry = suggestion["suggestions"][0]
        self.assertEqual(entry["observed_value"], 10.5)
        self.assertFalse(entry["compliance"])
        self.assertIn("建议调整", entry["suggestion"])

    def test_edit_plan_and_state_unchanged(self) -> None:
        plan_before = (self.project / "output" / "edit_plan.json").read_bytes()
        state_before = (self.project / "project_state.json").read_bytes()
        self._add_rule("r-inactive", build_rule("r-inactive"))
        generate_memory_suggestions(self.project, self.knowledge_root)
        self.assertEqual(
            (self.project / "output" / "edit_plan.json").read_bytes(), plan_before
        )
        self.assertEqual(
            (self.project / "project_state.json").read_bytes(), state_before
        )

    def test_write_report_idempotent(self) -> None:
        self._add_rule("r-inactive", build_rule("r-inactive"))
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        path = self.base / "suggestions.json"
        write_suggestion_report(suggestion, path)
        first = path.read_bytes()
        write_suggestion_report(suggestion, path)
        self.assertEqual(path.read_bytes(), first)

    def test_constraint_rule_suggestion(self) -> None:
        self._add_rule(
            "r-constraint",
            build_rule(
                "r-constraint",
                rule_type="shot_selection",
                expression={"constraint": "avoid_duplicate_visual_fingerprint"},
            ),
        )
        suggestion = generate_memory_suggestions(
            self.project, self.knowledge_root
        )
        self.assertEqual(suggestion["summary"]["suggestions"], 1)
        entry = suggestion["suggestions"][0]
        self.assertIsNone(entry["compliance"])
        self.assertIn("avoid_duplicate_visual_fingerprint", entry["suggestion"])
        self.assertIn("不自动执行", entry["suggestion"])


class MemorySuggestCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-suggest-cli-")
        self.base = Path(self._tmp.name)
        self.knowledge_root = self.base / "knowledge"
        init_knowledge(self.knowledge_root)
        self.project = make_project(self.base)
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

    def test_cli_empty_knowledge(self) -> None:
        result = self._run(
            "memory-suggest",
            str(self.project),
            "--knowledge-root",
            str(self.knowledge_root),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["rules_scanned"], 0)
        self.assertEqual(result["summary"]["suggestions"], 0)
        report = json.loads(
            (self.project / "memory_preview" / "memory_suggestions.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report["dry_run"])

    def test_cli_dry_run_zero_writes(self) -> None:
        result = self._run(
            "memory-suggest",
            str(self.project),
            "--knowledge-root",
            str(self.knowledge_root),
            "--dry-run",
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse(
            (self.project / "memory_preview" / "memory_suggestions.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
