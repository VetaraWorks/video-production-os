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
from video_os_core.memory_reader import load_rules  # noqa: E402
from video_os_core.rule_matcher import match_rules  # noqa: E402
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


def demo_context(**overrides: Any) -> dict[str, Any]:
    context = {
        "schema_version": 1,
        "project": "demo",
        "version": "v002",
        "video_type": "口播种草",
        "client": None,
        "style_profile": None,
        "platform": "抖音",
        "duration_target_s": 60,
        "available_metrics": {
            "product_first_appearance_s": 10.5,
            "average_clip_duration_s": 4.2,
        },
    }
    context.update(overrides)
    return context


class RuleMatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rule-matcher-test-")
        self.root = Path(self._tmp.name) / "knowledge"
        init_knowledge(self.root)
        self.rules_dir = self.root / "editing_rules"
        self.rules_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return install_formal_rule(
            self.root,
            rule_key=name,
            expression=payload["expression"],
            scope=payload.get("scope"),
            status=payload.get("status", "inactive"),
            evidence_status=payload.get("evidence_status", "valid"),
            confidence=float(payload.get("confidence_at_approval", 0.76)),
        )

    def _match(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        rules, invalid = load_rules(self.root)
        return match_rules(context or demo_context(), rules, invalid)

    def test_empty_library_zero_matches(self) -> None:
        report = self._match()
        self.assertEqual(report["summary"]["rules_scanned"], 0)
        self.assertEqual(report["matches"], [])
        self.assertTrue(report["dry_run"])

    def test_inactive_rule_preview_only(self) -> None:
        self._add("inactive", build_rule("rule-inactive"))
        report = self._match()
        entry = report["matches"][0]
        self.assertEqual(entry["match_status"], "matched")
        self.assertEqual(entry["execution_status"], "would_match_but_inactive")
        self.assertEqual(entry["observed_value"], 10.5)
        self.assertFalse(entry["compliance"])
        self.assertIn("不会改变剪辑", entry["explanation"])

    def test_active_rule_preview_only(self) -> None:
        self._add("active", build_rule("rule-active", status="active"))
        report = self._match()
        self.assertEqual(report["matches"][0]["execution_status"], "would_apply_in_future")
        self.assertIn("不会执行", report["matches"][0]["explanation"])

    def test_scope_matched(self) -> None:
        self._add("matched", build_rule("rule-matched"))
        report = self._match()
        self.assertEqual(report["summary"]["matched"], 1)
        self.assertEqual(report["summary"]["not_matched"], 0)

    def test_scope_not_matched(self) -> None:
        self._add(
            "other",
            build_rule("rule-other", scope={"video_type": "国风宣传", "client": None, "style_profile": None}),
        )
        report = self._match()
        self.assertEqual(report["summary"]["not_matched"], 1)
        self.assertEqual(report["matches"][0]["execution_status"], "not_applicable")

    def test_scope_unknown_when_field_missing(self) -> None:
        self._add("unknown", build_rule("rule-unknown"))
        context = demo_context(video_type=None)
        report = match_rules(context, *load_rules(self.root))
        self.assertEqual(report["summary"]["unknown"], 1)
        self.assertEqual(report["matches"][0]["missing_scope_fields"], ["video_type"])
        self.assertNotEqual(report["matches"][0]["match_status"], "matched")

    def test_stale_rule_warning_only(self) -> None:
        self._add("stale", build_rule("rule-stale", evidence_status="source_missing"))
        report = self._match()
        self.assertEqual(report["matches"], [])
        self.assertTrue(any("stale" in warning for warning in report["warnings"]))

    def test_deprecated_excluded_by_default(self) -> None:
        self._add("deprecated", build_rule("rule-deprecated", status="deprecated"))
        report = self._match()
        self.assertEqual(report["matches"], [])

    def test_compliance_computed(self) -> None:
        self._add("c1", build_rule("rule-c1", expression={"metric": "average_clip_duration_s", "operator": "<=", "value": 5.0}))
        report = self._match()
        self.assertTrue(report["matches"][0]["compliance"])
        c2_rule = self._add("c2", build_rule("rule-c2", expression={"metric": "average_clip_duration_s", "operator": ">", "value": 5.0}))
        report = self._match()
        c2 = next(item for item in report["matches"] if item["rule_id"] == c2_rule["rule_id"])
        self.assertFalse(c2["compliance"])

    def test_constraint_display_only(self) -> None:
        self._add(
            "constraint",
            build_rule(
                "rule-constraint",
                rule_type="shot_selection",
                expression={"constraint": "avoid_duplicate_visual_fingerprint"},
            ),
        )
        report = self._match()
        entry = report["matches"][0]
        self.assertEqual(entry["expression_status"], "constraint")
        self.assertIsNone(entry["compliance"])
        self.assertIn("绝不执行", entry["explanation"])

    def test_unsupported_operator_rejected(self) -> None:
        self._add(
            "badop",
            build_rule("rule-badop", expression={"metric": "x", "operator": "~~", "value": 1}),
        )
        report = self._match()
        self.assertEqual(report["matches"][0]["expression_status"], "unsupported")
        self.assertIsNone(report["matches"][0]["compliance"])

    def test_no_eval_usage(self) -> None:
        source = (ROOT / "scripts" / "video_os_core" / "rule_matcher.py").read_text(encoding="utf-8")
        self.assertNotIn("eval(", source)

    def test_conflicting_rules_marked_conflicted(self) -> None:
        self._add("ca", build_rule("rule-ca"))
        self._add(
            "cb",
            build_rule("rule-cb", expression={"metric": "product_first_appearance_s", "operator": "<=", "value": 12}),
        )
        report = self._match()
        self.assertEqual(report["summary"]["conflicted"], 2)
        for entry in report["matches"]:
            self.assertEqual(entry["match_status"], "conflicted")
            self.assertEqual(entry["execution_status"], "not_applicable")
            self.assertEqual(len(entry["conflicts_with"]), 1)

    def test_explanation_traceable(self) -> None:
        trace_rule = self._add("trace", build_rule("rule-trace"))
        report = self._match()
        entry = report["matches"][0]
        self.assertEqual(entry["source_candidate_id"], trace_rule["source_candidate_id"])
        self.assertEqual(entry["evidence_snapshot"][0]["kind"], "production_evidence")
        self.assertEqual(entry["approval"]["review_id"], trace_rule["review_id"])
        self.assertEqual(entry["approval"]["reviewer"], "fixture-human")
        self.assertIn("口播种草", entry["explanation"])
        self.assertIn("10.5", entry["explanation"])
        self.assertIn("8", entry["explanation"])

    def test_deterministic_output(self) -> None:
        self._add("d1", build_rule("rule-d1"))
        first = self._match()
        second = self._match()
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=False),
            json.dumps(second, sort_keys=True, ensure_ascii=False),
        )

    def test_invalid_file_counts(self) -> None:
        (self.rules_dir / "bad.json").write_text('{"rule_id": "x"}', encoding="utf-8")
        report = self._match()
        self.assertEqual(report["summary"]["invalid"], 1)
        self.assertTrue(any("invalid" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
