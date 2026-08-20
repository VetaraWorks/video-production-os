from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import (  # noqa: E402
    build_feedback_draft,
    build_feedback_draft_from_repair,
    init_knowledge,
    load_manifest,
    migrate_feedback_file,
    validate_feedback_v2,
    write_feedback_draft,
    write_feedback_v2,
)


def make_project(base: Path, name: str = "demo") -> Path:
    project = base / name
    (project / "script").mkdir(parents=True, exist_ok=True)
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "script" / "script.txt").write_text("hello\nworld\n", encoding="utf-8")
    (project / "config" / "config.json").write_text(
        json.dumps({"canvas": {"width": 360, "height": 640, "fps": 24}}),
        encoding="utf-8",
    )
    return project


class FeedbackCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="feedback-test-")
        self.base = Path(self._tmp.name)
        self.root = self.base / "knowledge"
        self.project = make_project(self.base)
        init_knowledge(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _draft(self, category: str, rule_class: str) -> dict:
        return build_feedback_draft(
            project="demo",
            from_version="v001",
            to_version="v002",
            changes=[
                {
                    "category": category,
                    "rule_class": rule_class,
                    "target": {"kind": "segment", "id": "hook"},
                    "before": {"description": "old"},
                    "after": {"description": "new"},
                    "reason": "test reason",
                }
            ],
            source_docs=["review.json"],
            snapshot_refs=["projects/demo/snapshots/v002"],
        )

    def test_build_editing_feedback(self) -> None:
        draft = self._draft("rhythm", "editing")
        self.assertEqual(validate_feedback_v2(draft), [])
        self.assertEqual(draft["evidence_tier"], "human_verified")
        change = draft["changes"][0]
        self.assertEqual(change["rule_class"], "editing")
        self.assertEqual(change["category"], "rhythm")
        self.assertEqual(change["status"], "pending")

    def test_build_style_feedback(self) -> None:
        draft = self._draft("subtitle_style", "style")
        self.assertEqual(validate_feedback_v2(draft), [])
        self.assertEqual(draft["changes"][0]["rule_class"], "style")

    def test_build_audit_feedback(self) -> None:
        draft = self._draft("repair", "audit")
        self.assertEqual(validate_feedback_v2(draft), [])
        self.assertEqual(draft["changes"][0]["rule_class"], "audit")

    def test_source_traceability(self) -> None:
        draft = self._draft("rhythm", "editing")
        self.assertEqual(draft["project"], "demo")
        self.assertEqual(draft["from_version"], "v001")
        self.assertEqual(draft["to_version"], "v002")
        self.assertIn("review.json", draft["source_docs"])
        self.assertIn("projects/demo/snapshots/v002", draft["snapshot_refs"])
        self.assertIn("review.json", draft["changes"][0]["source_docs"])

    def test_save_is_idempotent(self) -> None:
        draft = self._draft("rhythm", "editing")
        first = write_feedback_v2(self.root, draft)
        self.assertTrue(first["written"])
        second = write_feedback_v2(self.root, draft)
        self.assertFalse(second["written"])
        self.assertEqual(load_manifest(self.root)["counts"]["edits"], 1)

    def test_build_rejects_missing_source_info(self) -> None:
        with self.assertRaises(ValueError):
            build_feedback_draft(
                project="demo",
                from_version="",
                to_version="",
                changes=[
                    {
                        "category": "rhythm",
                        "rule_class": "editing",
                        "target": {"kind": "segment", "id": "hook"},
                        "before": {"description": "x"},
                        "after": {"description": "y"},
                        "reason": "",
                    }
                ],
            )

    def test_repair_draft_generation(self) -> None:
        repair_dir = self.project / "repair"
        repair_dir.mkdir(parents=True, exist_ok=True)
        (repair_dir / "repair_plan.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project": "demo",
                    "source_reports": ["review.json", "qa_report.json"],
                    "actions": [],
                    "needs_human": [],
                }
            ),
            encoding="utf-8",
        )
        (repair_dir / "repair_diff.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project": "demo",
                    "changes": [
                        {
                            "action_id": "repair-001",
                            "type": "replace_clip",
                            "segment_id": "proof",
                            "before": {"source": "material/proof_cta.mp4", "source_start": 0.0, "duration": 1.5},
                            "after": {"source": "material/alt.mp4", "source_start": 0.05, "duration": 1.5},
                            "reason": "duplicate clip",
                        },
                        {"action_id": "system", "type": "write_override", "detail": "override"},
                    ],
                    "script_changed": False,
                    "timeline_changed": False,
                    "plan_changed": True,
                }
            ),
            encoding="utf-8",
        )
        plan = json.loads((repair_dir / "repair_plan.json").read_text(encoding="utf-8"))
        diff = json.loads((repair_dir / "repair_diff.json").read_text(encoding="utf-8"))
        draft = build_feedback_draft_from_repair(
            project="demo",
            from_version="v001",
            to_version="v002",
            repair_plan=plan,
            repair_diff=diff,
            snapshot_refs=["projects/demo/snapshots/v002"],
        )
        self.assertEqual(len(draft["changes"]), 1)  # system action excluded
        change = draft["changes"][0]
        self.assertEqual(change["category"], "shot_selection")
        self.assertEqual(change["rule_class"], "editing")
        self.assertEqual(change["target"], {"kind": "segment", "id": "proof"})
        self.assertIn("material/proof_cta.mp4", change["before"]["description"])
        self.assertIn("material/alt.mp4", change["after"]["description"])
        self.assertEqual(validate_feedback_v2(draft), [])

    def test_repair_draft_never_auto_saves_to_edits(self) -> None:
        repair_dir = self.project / "repair"
        repair_dir.mkdir(parents=True, exist_ok=True)
        (repair_dir / "repair_diff.json").write_text(
            json.dumps(
                {
                    "changes": [
                        {
                            "type": "fix_subtitle",
                            "segment_id": "hook",
                            "before": {},
                            "after": {},
                            "reason": "subtitle fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        diff = json.loads((repair_dir / "repair_diff.json").read_text(encoding="utf-8"))
        draft = build_feedback_draft_from_repair(
            project="demo",
            from_version="v001",
            to_version="v002",
            repair_plan=None,
            repair_diff=diff,
        )
        saved = write_feedback_draft(self.project, draft)
        self.assertTrue(saved["written"])
        self.assertTrue(saved["path"].endswith(".draft.json"))
        self.assertEqual(load_manifest(self.root)["counts"]["edits"], 0)

    def test_historical_8_entries_unaffected(self) -> None:
        v1 = {
            "schema_version": 1,
            "project": "赛逸77",
            "from_version": "首版",
            "to_version": "V5",
            "source_docs": ["修改说明.md"],
            "changes": [
                {"category": "rhythm", "what": "x", "before": "a", "after": "b", "reason": "r"},
                {"category": "repair", "what": "y", "before": "c", "after": "d", "reason": "t"},
            ],
        }
        source = self.base / "feedback-v1.json"
        source.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
        migrate_feedback_file(source, self.root, "snap-ref")
        self.assertEqual(load_manifest(self.root)["counts"]["edits"], 1)
        new_draft = self._draft("rhythm", "editing")
        write_feedback_v2(self.root, new_draft)
        self.assertEqual(load_manifest(self.root)["counts"]["edits"], 2)
        edits = sorted((self.root / "edits").glob("*.json"))
        self.assertEqual(len(edits), 2)

    def test_cli_smoke_create_and_save(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "video_os.py"),
                "feedback",
                str(self.project),
                "--knowledge-root",
                str(self.root),
                "--from-version",
                "v001",
                "--to-version",
                "v002",
                "--category",
                "rhythm",
                "--rule-class",
                "editing",
                "--target-kind",
                "segment",
                "--segment-id",
                "hook",
                "--before",
                "产品22秒出现",
                "--after",
                "产品8秒出现",
                "--reason",
                "展示太晚",
                "--source-doc",
                "修改说明.md",
                "--snapshot-ref",
                "projects/demo/snapshots/v002",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "saved")
        self.assertTrue(payload["written"])
        self.assertEqual(load_manifest(self.root)["counts"]["edits"], 1)


if __name__ == "__main__":
    unittest.main()
