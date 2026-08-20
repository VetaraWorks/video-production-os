from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repair.repair_planner import plan_repair  # noqa: E402


DEFAULT_CONFIG = ROOT / "assets" / "default-config.json"


def make_plan_project(base: Path) -> Path:
    project = base / "demo"
    (project / "script").mkdir(parents=True, exist_ok=True)
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "output").mkdir(parents=True, exist_ok=True)
    (project / "perception").mkdir(parents=True, exist_ok=True)
    (project / "script" / "script.txt").write_text(
        "测试钩子\n产品展示\n", encoding="utf-8"
    )
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    config["jianying_export"]["enabled"] = False
    config["perception"]["required"] = False
    (project / "config" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    plan = {
        "schema_version": 2,
        "duration_seconds": 6.0,
        "segments": [
            {
                "id": "hook",
                "timeline_start": 0.0,
                "timeline_end": 1.0,
                "duration": 1.0,
                "source": "raw_video/a.mp4",
                "source_start": 0.0,
                "source_duration": 2.0,
                "has_audio": True,
                "loop": False,
                "selection": {"mode": "perception", "visual_fingerprint": "shot-a"},
            },
            {
                "id": "product",
                "timeline_start": 2.0,
                "timeline_end": 3.5,
                "duration": 1.5,
                "source": "material/prod.mp4",
                "source_start": 0.0,
                "source_duration": 2.0,
                "has_audio": False,
                "loop": False,
                "selection": {"mode": "perception", "visual_fingerprint": "shot-b"},
            },
        ],
    }
    (project / "output" / "edit_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    (project / "output" / "analysis.json").write_text(
        json.dumps(
            {
                "media": [
                    {"path": "raw_video/a.mp4", "has_video": True, "has_audio": True, "duration": 2.0},
                    {"path": "material/prod.mp4", "has_video": True, "has_audio": False, "duration": 2.0},
                    {"path": "material/b.mp4", "has_video": True, "has_audio": False, "duration": 3.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    perception = {
        "schema_version": 1,
        "status": "done",
        "provider": {"name": "test", "model": "test"},
        "sources": [
            {
                "source": "material/b.mp4",
                "duration": 3.0,
                "segments": [
                    {
                        "id": "b-001",
                        "start": 0.0,
                        "end": 2.0,
                        "safe_start": 0.1,
                        "safe_end": 1.9,
                        "summary": "产品细节",
                        "semantic_tags": ["product", "detail"],
                        "quality": {"usable": True, "score": 0.9},
                        "confidence": 0.9,
                        "visual_fingerprint": "shot-c",
                    }
                ],
            }
        ],
    }
    (project / "perception" / "perception.json").write_text(
        json.dumps(perception, ensure_ascii=False), encoding="utf-8"
    )
    return project


class RepairPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="repair-planner-")
        self.project = make_plan_project(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan_before_state(self) -> tuple[str, str]:
        plan = (self.project / "output" / "edit_plan.json").read_text(encoding="utf-8")
        perception = (self.project / "perception" / "perception.json").read_text(encoding="utf-8")
        return plan, perception

    def test_subtitle_error_generates_fix_subtitle(self) -> None:
        review = {
            "verdict": "fix",
            "issues": [
                {
                    "id": "r1",
                    "category": "subtitle_error",
                    "segment_id": "product",
                    "subtitle": {"text_from": "产品展示", "text_to": "产品细节展示"},
                }
            ],
        }
        plan = plan_repair(self.project, review, None)
        self.assertEqual(len(plan["actions"]), 1)
        action = plan["actions"][0]
        self.assertEqual(action["type"], "fix_subtitle")
        self.assertEqual(action["kind"], "text")
        self.assertEqual(action["text_from"], "产品展示")
        self.assertEqual(action["segment_id"], "product")

    def test_duplicate_clip_generates_replace_clip(self) -> None:
        review = {
            "verdict": "fix",
            "issues": [
                {
                    "id": "r2",
                    "category": "duplicate_clip",
                    "segment_id": "product",
                    "description": "重复镜头",
                }
            ],
        }
        plan = plan_repair(self.project, review, None)
        self.assertEqual(len(plan["actions"]), 1)
        action = plan["actions"][0]
        self.assertEqual(action["type"], "replace_clip")
        self.assertEqual(action["after"]["source"], "material/b.mp4")
        self.assertEqual(action["after"]["source_start"], 0.1)
        self.assertEqual(action["candidate"]["visual_fingerprint"], "shot-c")

    def test_wrong_clip_generates_replace_clip(self) -> None:
        review = {
            "verdict": "fix",
            "issues": [
                {
                    "id": "r3",
                    "category": "wrong_clip",
                    "segment_id": "product",
                }
            ],
        }
        plan = plan_repair(self.project, review, None)
        self.assertEqual(plan["actions"][0]["type"], "replace_clip")

    def test_picture_issue_is_covered_by_same_segment_replacement(self) -> None:
        review = {
            "verdict": "fix",
            "issues": [
                {
                    "id": "picture-placeholder",
                    "category": "picture",
                    "segment_id": "product",
                    "description": "placeholder graphic",
                },
                {
                    "id": "duplicate-product",
                    "category": "duplicate_shot",
                    "segment_id": "product",
                    "description": "repeated visual",
                },
            ],
        }
        plan = plan_repair(self.project, review, None)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(plan["actions"][0]["type"], "replace_clip")
        self.assertEqual(plan["actions"][0]["segment_id"], "product")
        self.assertEqual(plan["needs_human"], [])

    def test_picture_issue_without_replacement_still_needs_human(self) -> None:
        review = {
            "verdict": "fix",
            "issues": [
                {
                    "id": "picture-only",
                    "category": "picture",
                    "segment_id": "product",
                }
            ],
        }
        plan = plan_repair(self.project, review, None)
        self.assertEqual(plan["actions"], [])
        self.assertTrue(plan["needs_human"])

    def test_unfixable_category_goes_to_needs_human(self) -> None:
        review = {
            "verdict": "fix",
            "issues": [
                {
                    "id": "r4",
                    "category": "freeze_frame",
                    "segment_id": "hook",
                    "description": "卡帧",
                }
            ],
        }
        plan = plan_repair(self.project, review, None)
        self.assertEqual(plan["actions"], [])
        self.assertTrue(plan["needs_human"])

    def test_planner_never_mutates_files(self) -> None:
        before_plan, before_perception = self._plan_before_state()
        review = {
            "verdict": "fix",
            "issues": [
                {
                    "id": "r5",
                    "category": "wrong_clip",
                    "segment_id": "product",
                }
            ],
        }
        plan_repair(self.project, review, None)
        self.assertEqual(
            (self.project / "output" / "edit_plan.json").read_text(encoding="utf-8"),
            before_plan,
        )
        self.assertEqual(
            (self.project / "perception" / "perception.json").read_text(encoding="utf-8"),
            before_perception,
        )

    def test_fullscreen_plan_marked_needs_human(self) -> None:
        plan = {
            "schema_version": 2,
            "base_video": "raw_video/a.mp4",
            "broll_video": "material/b.mp4",
            "fullscreen_events": [],
        }
        (self.project / "output" / "edit_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8"
        )
        review = {
            "verdict": "fix",
            "issues": [{"id": "r6", "category": "wrong_clip", "segment_id": "product"}],
        }
        result = plan_repair(self.project, review, None)
        self.assertEqual(result["actions"], [])
        self.assertTrue(any("fullscreen" in item for item in result["needs_human"]))


if __name__ == "__main__":
    unittest.main()
