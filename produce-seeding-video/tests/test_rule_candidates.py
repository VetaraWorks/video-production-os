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
from video_os_core.rule_candidates import (  # noqa: E402
    build_candidates,
    candidate_id_for,
    extract_rule_candidates,
    list_candidates,
    validate_rule_candidates,
)


def make_feedback(
    project: str,
    version: str,
    feedback_id: str,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
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
        "changes": changes,
    }


def timing_change(metric: str, before: float, after: float, category: str = "rhythm") -> dict[str, Any]:
    return {
        "change_id": "c-1",
        "category": category,
        "rule_class": "editing",
        "target": {"kind": "whole_video"},
        "before": {"description": "x", "metric": {"name": metric, "value": before}},
        "after": {"description": "y", "metric": {"name": metric, "value": after}},
        "reason": "test",
        "status": "pending",
        "source_docs": ["review.json"],
    }


def shot_selection_change(constraint: str = "avoid_duplicate_visual_fingerprint") -> dict[str, Any]:
    return {
        "change_id": "c-2",
        "category": "shot_selection",
        "rule_class": "editing",
        "target": {"kind": "segment", "id": "proof"},
        "before": {"description": "clip A"},
        "after": {"description": "clip B"},
        "reason": "duplicate clip replaced",
        "rule_candidate_structured": {"constraint": constraint},
        "status": "pending",
        "source_docs": ["review.json"],
    }


def make_repair_log(
    project: str,
    version: str,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evidence_tier": "production_verified",
        "project": project,
        "version": version,
        "source": f"projects/{project}/snapshots/{version}",
        "source_reports": ["review.json"],
        "actions": actions,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class RuleCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rule-candidate-test-")
        self.root = Path(self._tmp.name) / "knowledge"
        init_knowledge(self.root)
        self.edits = self.root / "edits"
        self.repair_log = self.root / "repair_log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_feedback(self, name: str, payload: dict[str, Any]) -> None:
        write_json(self.edits / f"{name}.json", payload)

    def _add_repair(self, name: str, payload: dict[str, Any]) -> None:
        write_json(self.repair_log / f"{name}.json", payload)

    def test_single_feedback_not_enough(self) -> None:
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        candidates, stats = build_candidates(self.root)
        self.assertEqual(candidates, [])
        self.assertEqual(stats["feedback_scanned"], 1)
        self.assertTrue(any("weighted evidence" in reason or "unique record" in reason for reason in stats["reasons"]))

    def test_demo_migrated_and_untiered_evidence_are_excluded(self) -> None:
        demo = make_feedback(
            "demo",
            "v002",
            "fb-demo",
            [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
        )
        demo["evidence_tier"] = "demo"
        migrated = make_feedback(
            "migrated",
            "v003",
            "fb-migrated",
            [timing_change("product_first_appearance_s", 15.0, 6.0, "timing")],
        )
        migrated["evidence_tier"] = "migrated_unverified"
        untiered = make_feedback(
            "legacy",
            "v004",
            "fb-legacy",
            [timing_change("product_first_appearance_s", 12.0, 5.0, "timing")],
        )
        untiered.pop("evidence_tier")
        self._add_feedback("demo", demo)
        self._add_feedback("migrated", migrated)
        self._add_feedback("legacy", untiered)
        repair = make_repair_log(
            "repair-demo",
            "v002",
            [{"type": "replace_clip", "reason": "duplicate clip reused"}],
        )
        repair["evidence_tier"] = "migrated_unverified"
        self._add_repair("repair-demo", repair)

        candidates, stats = build_candidates(self.root)
        self.assertEqual(candidates, [])
        self.assertEqual(stats["feedback_scanned"], 3)
        self.assertEqual(stats["feedback_excluded_unverified"], 3)
        self.assertEqual(stats["repair_log_scanned"], 1)
        self.assertEqual(stats["repair_log_excluded_unverified"], 1)

    def test_public_snapshot_does_not_ship_repository_knowledge(self) -> None:
        repository_knowledge = ROOT.parent / "knowledge"
        self.assertFalse(repository_knowledge.exists())

    def test_two_same_direction_feedback_reach_threshold(self) -> None:
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        self._add_feedback(
            "fb-b",
            make_feedback(
                "demo2",
                "v003",
                "fb-b",
                [timing_change("product_first_appearance_s", 15.0, 6.0, "timing")],
            ),
        )
        candidates, stats = build_candidates(self.root)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["rule_type"], "timing")
        self.assertEqual(candidate["expression"]["metric"], "product_first_appearance_s")
        self.assertEqual(candidate["expression"]["operator"], "<=")
        self.assertEqual(candidate["expression"]["value"], 8.0)
        self.assertEqual(candidate["weighted_evidence"], 2.0)
        self.assertEqual(candidate["evidence_count"], 2)
        self.assertEqual(candidate["project_count"], 2)
        self.assertEqual(candidate["human_feedback_count"], 2)
        self.assertEqual(candidate["repair_evidence_count"], 0)

    def test_repair_evidence_counts_half(self) -> None:
        # Feedback uses shot_duration_s so it aggregates with adjust_trim repairs.
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("shot_duration_s", 22.0, 8.0, "rhythm")],
            ),
        )
        self._add_repair(
            "repair-a",
            make_repair_log(
                "demo2",
                "v003",
                [
                    {
                        "type": "adjust_trim",
                        "segment_id": "hook",
                        "before": {"duration": 6.0},
                        "after": {"duration": 3.0},
                    }
                ],
            ),
        )
        candidates, _ = build_candidates(self.root)
        # 1.0 (feedback) + 0.5 (repair) = 1.5 < 2.0 -> no candidate.
        self.assertEqual(candidates, [])

        # Add a second repair in a different version: 1.0 + 0.5 + 0.5 = 2.0.
        self._add_repair(
            "repair-b",
            make_repair_log(
                "demo2",
                "v004",
                [
                    {
                        "type": "adjust_trim",
                        "segment_id": "hook",
                        "before": {"duration": 5.0},
                        "after": {"duration": 2.5},
                    }
                ],
            ),
        )
        candidates, _ = build_candidates(self.root)
        rhythm = [c for c in candidates if c["rule_type"] == "rhythm"]
        self.assertEqual(len(rhythm), 1)
        self.assertEqual(rhythm[0]["expression"]["metric"], "shot_duration_s")
        self.assertEqual(rhythm[0]["weighted_evidence"], 2.0)

    def test_draft_does_not_participate(self) -> None:
        # A .draft.json under edits/ must be skipped.
        draft = make_feedback(
            "demo",
            "v002",
            "draft-1",
            [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
        )
        write_json(self.edits / "draft-1.draft.json", draft)
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        candidates, stats = build_candidates(self.root)
        self.assertEqual(candidates, [])
        self.assertEqual(stats["feedback_scanned"], 1)  # draft not scanned as feedback

    def test_style_and_audit_not_participating(self) -> None:
        style_change = timing_change("product_first_appearance_s", 22.0, 8.0, "timing")
        style_change["rule_class"] = "style"
        audit_change = timing_change("product_first_appearance_s", 22.0, 8.0, "timing")
        audit_change["rule_class"] = "audit"
        self._add_feedback(
            "fb-a",
            make_feedback("demo", "v002", "fb-a", [style_change]),
        )
        self._add_feedback(
            "fb-b",
            make_feedback("demo2", "v003", "fb-b", [audit_change]),
        )
        candidates, stats = build_candidates(self.root)
        self.assertEqual(candidates, [])
        self.assertGreaterEqual(stats["feedback_excluded_style_or_audit"], 2)

    def test_same_project_diversity_not_inflated(self) -> None:
        for name in ("fb-a", "fb-b"):
            self._add_feedback(
                name,
                make_feedback(
                    "demo",
                    "v002",
                    name,
                    [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
                ),
            )
        candidates, _ = build_candidates(self.root)
        # Two records from one project are not two independent production sources.
        self.assertEqual(candidates, [])

    def test_multi_project_raises_diversity(self) -> None:
        projects = [("demo", "v002"), ("demo2", "v003"), ("demo3", "v004")]
        for index, (project, version) in enumerate(projects):
            self._add_feedback(
                f"fb-{index}",
                make_feedback(
                    project,
                    version,
                    f"fb-{index}",
                    [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
                ),
            )
        candidates, _ = build_candidates(self.root)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["project_count"], 3)
        self.assertGreater(candidates[0]["confidence_factors"]["diversity"], 0.5)

    def test_contradicting_evidence_lowers_confidence(self) -> None:
        for name, project, version, before, after in (
            ("fb-a", "demo", "v002", 22.0, 8.0),
            ("fb-b", "demo2", "v003", 15.0, 6.0),
            ("fb-c", "demo3", "v004", 18.0, 9.0),
        ):
            self._add_feedback(
                name,
                make_feedback(
                    project,
                    version,
                    name,
                    [timing_change("product_first_appearance_s", before, after, "timing")],
                ),
            )
        # One contradicting (increase) evidence: 3.0 support vs 1.0 conflict.
        self._add_feedback(
            "fb-d",
            make_feedback(
                "demo4",
                "v005",
                "fb-d",
                [timing_change("product_first_appearance_s", 8.0, 15.0, "timing")],
            ),
        )
        candidates, _ = build_candidates(self.root)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0]["contradicting_evidence"]), 1)
        self.assertLess(candidates[0]["confidence"], 0.9)

    def test_equal_strength_conflict_not_emitted(self) -> None:
        # 1.0 support vs 1.0 conflict: equal strength -> not emitted.
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        self._add_feedback(
            "fb-b",
            make_feedback(
                "demo2",
                "v003",
                "fb-b",
                [timing_change("product_first_appearance_s", 8.0, 15.0, "timing")],
            ),
        )
        candidates, stats = build_candidates(self.root)
        self.assertEqual(candidates, [])
        # Equal strength conflict is also below the weighted threshold (1.0 < 2.0).
        self.assertTrue(
            any(
                "weighted evidence" in reason or "consistency" in reason
                for reason in stats["reasons"]
            )
        )

    def test_fuzzy_natural_language_not_converted(self) -> None:
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [
                    {
                        "change_id": "c-1",
                        "category": "rhythm",
                        "rule_class": "editing",
                        "target": {"kind": "whole_video"},
                        "before": {"description": "产品出现太晚"},
                        "after": {"description": "产品更早出现"},
                        "reason": "客户反馈",
                        "status": "pending",
                    }
                ],
            ),
        )
        candidates, stats = build_candidates(self.root)
        self.assertEqual(candidates, [])
        self.assertGreaterEqual(stats["feedback_excluded_fuzzy"], 1)

    def test_candidate_id_deterministic(self) -> None:
        expression = {"metric": "product_first_appearance_s", "operator": "<=", "value": 8.0}
        first = candidate_id_for("editing", "timing", "timing", expression, {"video_type": None, "client": None, "style_profile": None})
        second = candidate_id_for("editing", "timing", "timing", expression, {"video_type": None, "client": None, "style_profile": None})
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("cand-"))

    def test_extract_writes_once_and_is_idempotent(self) -> None:
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        self._add_feedback(
            "fb-b",
            make_feedback(
                "demo2",
                "v003",
                "fb-b",
                [timing_change("product_first_appearance_s", 15.0, 6.0, "timing")],
            ),
        )
        result = extract_rule_candidates(self.root)
        self.assertEqual(result["written"], 1)
        target = self.root / "rule_candidates"
        files_after_first = sorted(target.glob("*.json"))
        self.assertEqual(len(files_after_first), 1)
        content_first = files_after_first[0].read_text(encoding="utf-8")
        result_again = extract_rule_candidates(self.root)
        self.assertEqual(result_again["written"], 0)
        self.assertEqual(result_again["unchanged"], 1)
        self.assertEqual(len(sorted(target.glob("*.json"))), 1)
        self.assertEqual(files_after_first[0].read_text(encoding="utf-8"), content_first)

    def test_original_files_not_modified(self) -> None:
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        before = (self.edits / "fb-a.json").read_bytes()
        self._add_repair(
            "repair-a",
            make_repair_log(
                "demo2",
                "v003",
                [
                    {
                        "type": "replace_clip",
                        "segment_id": "proof",
                        "reason": "duplicate clip reused",
                    }
                ],
            ),
        )
        repair_before = (self.repair_log / "repair-a.json").read_bytes()
        extract_rule_candidates(self.root)
        self.assertEqual((self.edits / "fb-a.json").read_bytes(), before)
        self.assertEqual((self.repair_log / "repair-a.json").read_bytes(), repair_before)

    def test_schema_and_manifest_validation(self) -> None:
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        self._add_feedback(
            "fb-b",
            make_feedback(
                "demo2",
                "v003",
                "fb-b",
                [timing_change("product_first_appearance_s", 15.0, 6.0, "timing")],
            ),
        )
        extract_rule_candidates(self.root)
        result = validate_rule_candidates(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(result["manifest"]["counts"]["rule_candidates"], 1)

    def test_stale_marked_when_evidence_removed(self) -> None:
        self._add_feedback(
            "fb-a",
            make_feedback(
                "demo",
                "v002",
                "fb-a",
                [timing_change("product_first_appearance_s", 22.0, 8.0, "timing")],
            ),
        )
        self._add_feedback(
            "fb-b",
            make_feedback(
                "demo2",
                "v003",
                "fb-b",
                [timing_change("product_first_appearance_s", 15.0, 6.0, "timing")],
            ),
        )
        extract_rule_candidates(self.root)
        (self.edits / "fb-a.json").unlink()
        result = extract_rule_candidates(self.root)
        self.assertEqual(result["stale_marked"], 1)
        # Candidate is no longer regenerated; the existing file must be marked stale.
        candidates = list_candidates(self.root)
        self.assertEqual(candidates["candidate_count"], 1)
        self.assertEqual(candidates["candidates"][0]["status"], "stale")

    def test_shot_selection_from_repair_duplicate_reason(self) -> None:
        # 0.5 weight each: need 4 distinct repairs to reach 2.0.
        for index in range(4):
            self._add_repair(
                f"repair-{index}",
                make_repair_log(
                    f"demo{index}",
                    f"v00{index + 2}",
                    [
                        {
                            "type": "replace_clip",
                            "segment_id": "proof",
                            "reason": "duplicate clip reused" if index % 2 == 0 else "重复使用同一镜头",
                        }
                    ],
                ),
            )
        candidates, _ = build_candidates(self.root)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["rule_type"], "shot_selection")
        self.assertEqual(candidates[0]["expression"]["constraint"], "avoid_duplicate_visual_fingerprint")
        self.assertEqual(candidates[0]["weighted_evidence"], 2.0)


if __name__ == "__main__":
    unittest.main()
