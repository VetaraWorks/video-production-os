from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import (  # noqa: E402
    KNOWLEDGE_DIRS,
    init_knowledge,
    load_manifest,
    migrate_feedback_file,
    migrate_feedback_v1_to_v2,
    validate_feedback_v2,
    write_feedback_v2,
)


V1_SAMPLE = {
    "schema_version": 1,
    "project": "赛逸77",
    "snapshot": "v6-process",
    "from_version": "首版",
    "to_version": "V5（真人反馈修正版）",
    "source_docs": ["修改说明.md", "全屏切镜精修版说明.md", "output/qa_report-v5.json"],
    "changes": [
        {
            "category": "structure",
            "what": "画中画插片结构",
            "before": "单层口播底片",
            "after": "画中画叠加 + 片尾封面",
            "reason": "增强产品画面表现（画中画版）",
            "source_doc": "修改说明.md",
        },
        {
            "category": "shot_count",
            "what": "全屏切镜数量",
            "before": "10 处",
            "after": "20 处",
            "reason": "补充具体语义镜头",
            "source_doc": "全屏切镜精修版说明.md",
        },
        {
            "category": "semantic_split",
            "what": "口播段落拆镜头",
            "before": "一段口播对应一个画面",
            "after": "按语义拆成多个画面",
            "reason": "避免语义混淆",
            "source_doc": "全屏切镜精修版说明.md",
        },
        {
            "category": "subtitle_style",
            "what": "重点大字样式",
            "before": "普通字幕",
            "after": "黑体、放大、变色、弹入动画，固定顶部安全区",
            "reason": "突出关键词，避开人脸遮挡",
            "source_doc": "全屏切镜精修版说明.md",
        },
        {
            "category": "rhythm",
            "what": "口播画面丰富度",
            "before": "开头/种草/资质背书段落画面单一",
            "after": "增加 3 处缓慢推近",
            "reason": "丰富口播画面",
            "source_doc": "全屏切镜精修版说明.md",
        },
        {
            "category": "sound_effect",
            "what": "音效数量与音量",
            "before": "少量音效",
            "after": "13 个语义音效节点，音量克制",
            "reason": "强调关键动作但不压口播",
            "source_doc": "全屏切镜精修版说明.md",
        },
        {
            "category": "repair",
            "what": "自动修复",
            "before": "无",
            "after": "1 次自动 repair（auto_repair_executed=true）",
            "reason": "QA 发现后自动修复（V5 记录）",
            "source_doc": "output/qa_report-v5.json",
        },
        {
            "category": "human_review",
            "what": "真人反馈修正",
            "before": "V4 人工复核版",
            "after": "V5 真人反馈修正版",
            "reason": "真人反馈后的修正迭代",
            "source_doc": "output/qa_report-v5.json",
        },
    ],
}


class KnowledgeLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="knowledge-test-")
        self.root = Path(self._tmp.name) / "knowledge"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_init_creates_tree_and_manifest(self) -> None:
        result = init_knowledge(self.root)
        self.assertTrue(result["ok"])
        for name in KNOWLEDGE_DIRS:
            self.assertTrue((self.root / name).is_dir(), name)
        self.assertTrue((self.root / "README.md").is_file())
        manifest = load_manifest(self.root)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["counts"]["edits"], 0)

    def test_validate_feedback_v2_ok(self) -> None:
        feedback, _ = migrate_feedback_v1_to_v2(V1_SAMPLE, "snap-ref")
        self.assertEqual(validate_feedback_v2(feedback), [])

    def test_validate_feedback_v2_rejects_bad_record(self) -> None:
        feedback, _ = migrate_feedback_v1_to_v2(V1_SAMPLE, "snap-ref")
        feedback["changes"][0]["rule_class"] = "bogus"
        feedback["changes"][1]["target"] = {"kind": "invalid"}
        errors = validate_feedback_v2(feedback)
        self.assertTrue(any("rule_class" in error for error in errors))
        self.assertTrue(any("target" in error for error in errors))

    def test_migrate_preserves_all_8_changes(self) -> None:
        feedback, repair_log = migrate_feedback_v1_to_v2(V1_SAMPLE, "snap-ref")
        self.assertEqual(len(feedback["changes"]), 8)
        self.assertEqual(feedback["project"], "赛逸77")
        self.assertEqual(feedback["schema_version"], 2)
        self.assertIn("snap-ref", feedback["snapshot_refs"])
        self.assertIn("修改说明.md", feedback["source_docs"])
        for change in feedback["changes"]:
            self.assertTrue(change["change_id"])
            self.assertTrue(change["reason"])
            self.assertEqual(change["status"], "pending")
            self.assertEqual(change["target"], {"kind": "whole_video"})
            self.assertIn("description", change["before"])
            self.assertIn("description", change["after"])

    def test_migrate_rule_class_classification(self) -> None:
        feedback, _ = migrate_feedback_v1_to_v2(V1_SAMPLE, "snap-ref")
        by_category = {
            change["category"]: change["rule_class"] for change in feedback["changes"]
        }
        self.assertEqual(by_category["structure"], "editing")
        self.assertEqual(by_category["shot_count"], "editing")
        self.assertEqual(by_category["semantic_split"], "editing")
        self.assertEqual(by_category["subtitle_style"], "style")
        self.assertEqual(by_category["rhythm"], "editing")
        self.assertEqual(by_category["sound_effect"], "editing")
        self.assertEqual(by_category["repair"], "audit")
        self.assertEqual(by_category["human_review"], "audit")

    def test_migrate_creates_repair_log_entry(self) -> None:
        feedback, repair_log = migrate_feedback_v1_to_v2(V1_SAMPLE, "snap-ref")
        self.assertIsNotNone(repair_log)
        self.assertEqual(repair_log["project"], "赛逸77")
        self.assertEqual(repair_log["version"], "V5（真人反馈修正版）")
        self.assertEqual(len(repair_log["actions"]), 2)  # repair + human_review
        self.assertTrue(
            any(action["type"] == "repair" for action in repair_log["actions"])
        )

    def test_migrate_file_writes_and_updates_manifest(self) -> None:
        init_knowledge(self.root)
        source = self.root.parent / "feedback-v1.json"
        source.write_text(json.dumps(V1_SAMPLE, ensure_ascii=False), encoding="utf-8")
        result = migrate_feedback_file(
            source, self.root, "projects/赛逸77/snapshots/v6-process"
        )
        self.assertTrue(result["feedback"]["written"])
        self.assertTrue(result["repair_log"]["written"])
        manifest = load_manifest(self.root)
        self.assertEqual(manifest["counts"]["edits"], 1)
        self.assertEqual(manifest["counts"]["repair_log"], 1)

    def test_migrate_idempotent(self) -> None:
        init_knowledge(self.root)
        source = self.root.parent / "feedback-v1.json"
        source.write_text(json.dumps(V1_SAMPLE, ensure_ascii=False), encoding="utf-8")
        migrate_feedback_file(source, self.root, "snap-ref")
        result = migrate_feedback_file(source, self.root, "snap-ref")
        self.assertFalse(result["feedback"]["written"])
        self.assertFalse(result["repair_log"]["written"])
        manifest = load_manifest(self.root)
        self.assertEqual(manifest["counts"]["edits"], 1)

    def test_write_feedback_rejects_invalid(self) -> None:
        init_knowledge(self.root)
        feedback, _ = migrate_feedback_v1_to_v2(V1_SAMPLE, "snap-ref")
        feedback["changes"][0]["category"] = "bogus"
        with self.assertRaises(ValueError):
            write_feedback_v2(self.root, feedback)

    def test_feedback_writer_cannot_assign_production_verified_directly(self) -> None:
        init_knowledge(self.root)
        feedback, _ = migrate_feedback_v1_to_v2(V1_SAMPLE, "snap-ref")
        feedback["evidence_tier"] = "production_verified"
        with self.assertRaisesRegex(ValueError, "Production Evidence Gate"):
            write_feedback_v2(self.root, feedback)


if __name__ == "__main__":
    unittest.main()
