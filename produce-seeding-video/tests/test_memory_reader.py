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
from video_os_core.memory_reader import (  # noqa: E402
    load_project_context,
    load_rules,
    validate_editing_rule,
)
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
        "evidence_snapshot": [],
        "approval": {"review_id": "review-1", "reviewer": "user", "reason": "批准"},
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
        "evidence_status": "valid",
    }
    rule.update(overrides)
    return rule


class MemoryReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="memory-reader-test-")
        self.root = Path(self._tmp.name) / "knowledge"
        init_knowledge(self.root)
        self.rules_dir = self.root / "editing_rules"
        self.rules_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_rule(self, name: str, payload: dict[str, Any]) -> None:
        (self.rules_dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_empty_library_returns_zero(self) -> None:
        rules, invalid = load_rules(self.root)
        self.assertEqual(rules, [])
        self.assertEqual(invalid, [])

    def test_default_excludes_deprecated_and_superseded(self) -> None:
        deprecated = install_formal_rule(
            self.root,
            rule_key="deprecated",
            expression={"metric": "metric_deprecated", "operator": "<=", "value": 8},
            status="deprecated",
        )
        revoked = install_formal_rule(
            self.root,
            rule_key="revoked",
            expression={"metric": "metric_revoked", "operator": "<=", "value": 8},
            status="revoked",
        )
        inactive = install_formal_rule(
            self.root,
            rule_key="inactive",
            expression={"metric": "metric_inactive", "operator": "<=", "value": 8},
        )
        rules, _ = load_rules(self.root)
        self.assertEqual([r["rule_id"] for r in rules], [inactive["rule_id"]])
        rules_hist, _ = load_rules(self.root, include_historical=True)
        self.assertEqual(
            {r["rule_id"] for r in rules_hist},
            {deprecated["rule_id"], revoked["rule_id"], inactive["rule_id"]},
        )

    def test_invalid_rule_reported(self) -> None:
        self._write_rule("bad", {"rule_id": "rule-bad"})
        rules, invalid = load_rules(self.root)
        self.assertEqual(rules, [])
        self.assertEqual(len(invalid), 1)
        self.assertIn("missing field", invalid[0]["errors"][0])

    def test_validate_editing_rule(self) -> None:
        rule = install_formal_rule(
            self.root,
            rule_key="validate",
            expression={"metric": "metric_validate", "operator": "<=", "value": 8},
        )
        self.assertEqual(validate_editing_rule(rule), [])
        bad = dict(rule)
        bad["status"] = "bogus"
        self.assertTrue(any("status" in error for error in validate_editing_rule(bad)))

    def test_load_project_context_normalizes_missing_fields(self) -> None:
        path = self.root.parent / "context.json"
        path.write_text(json.dumps({"schema_version": 1, "project": "demo"}), encoding="utf-8")
        context = load_project_context(path)
        self.assertEqual(context["project"], "demo")
        self.assertIsNone(context["video_type"])
        self.assertEqual(context["available_metrics"], {})

    def test_rule_class_filter(self) -> None:
        rule = install_formal_rule(
            self.root,
            rule_key="style",
            expression={"metric": "metric_style", "operator": "<=", "value": 8},
        )
        rule["rule_class"] = "style"
        self._write_rule("style-copy", rule)
        for path in self.rules_dir.glob(f"{rule['rule_id']}*.json"):
            if path.name != "style-copy.json":
                path.unlink()
        rules, _ = load_rules(self.root)
        self.assertEqual(rules, [])


if __name__ == "__main__":
    unittest.main()
