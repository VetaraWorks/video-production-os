from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import init_knowledge  # noqa: E402
from video_os_core.planner_memory import build_planner_memory  # noqa: E402
from video_os_core.decision_log import list_governance_history, record_decision  # noqa: E402
from video_os_core.memory_suggestions import generate_memory_suggestions  # noqa: E402
from video_os_core.production_evidence import (  # noqa: E402
    EvidenceValidationError,
    TIER_HUMAN_VERIFIED,
    TIER_OBSERVED,
    TIER_PRODUCTION_VERIFIED,
    capture_observed_repair,
    finalize_repair_evidence,
    record_manual_evidence,
    sync_verified_evidence,
    write_evidence_record,
)
from video_os_core.rule_approval import approve_rule  # noqa: E402
from video_os_core.rule_candidates import build_candidates, extract_rule_candidates  # noqa: E402
from video_pipeline.perception import source_signature  # noqa: E402
from video_pipeline.config import load_config  # noqa: E402
from governance_fixtures import install_formal_rule  # noqa: E402


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class ProductionEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.ffprobe = shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("real ffmpeg/ffprobe are required")
        cls._media_tmp = tempfile.TemporaryDirectory(prefix="production-evidence-media-")
        media_root = Path(cls._media_tmp.name)
        cls.before_media = media_root / "before.mp4"
        cls.after_media = media_root / "after.mp4"
        cls._render_media(cls.before_media, "red", 440)
        cls._render_media(cls.after_media, "blue", 660)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._media_tmp.cleanup()

    @classmethod
    def _render_media(cls, path: Path, color: str, frequency: int) -> None:
        completed = subprocess.run(
            [
                str(cls.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=160x240:r=25:d=1",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise unittest.SkipTest(f"could not create real media: {completed.stderr}")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="production-evidence-test-")
        self.base = Path(self._tmp.name)
        self.knowledge_root = self.base / "knowledge"
        init_knowledge(self.knowledge_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan(self, digest: str, *, segment_duration: float = 1.0) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "duration_seconds": 1.0,
            "canvas": {"width": 160, "height": 240},
            "perception": {
                "input_signature_digest": digest,
                "selected_segment_ids": ["visual-1"],
            },
            "segments": [
                {
                    "id": "segment-1",
                    "timeline_start": 0.0,
                    "timeline_end": 1.0,
                    "duration": segment_duration,
                    "source": "raw_video/clip.mp4",
                    "selection": {
                        "mode": "perception",
                        "perception_segment_id": "visual-1",
                    },
                }
            ],
        }

    def _perception(self, digest: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider": "real-test-provider",
            "input_signature": {"digest_sha256": digest},
            "sources": [{"segments": [{"id": "visual-1"}]}],
        }

    def _review(self, project: Path, verdict: str) -> dict[str, Any]:
        issues = []
        if verdict == "fix":
            issues = [
                {
                    "id": "issue-1",
                    "category": "continuity",
                    "severity": "medium",
                    "start": 0.0,
                    "end": 0.5,
                    "segment_id": "segment-1",
                    "description": "trim this segment",
                    "suggestion": "end at 0.5 seconds",
                }
            ]
        return {
            "schema_version": 1,
            "task_id": f"review-{verdict}-{project.name}",
            "status": "done",
            "verdict": verdict,
            "target": {
                "path": "output/final.mp4",
                "signature": source_signature(project / "output" / "final.mp4"),
                "duration": 1.0,
            },
            "categories": ["continuity"],
            "issues": issues,
        }

    def _write_durable_review(self, project: Path, review: dict[str, Any]) -> None:
        task_id = str(review["task_id"])
        result_path = project / "review" / "results" / f"{task_id}.json"
        task = {
            "schema_version": 1,
            "task_type": "review",
            "task_id": task_id,
            "status": "done",
            "target": "output/final.mp4",
            "target_signature": review["target"]["signature"],
            "result_path": str(result_path),
        }
        write_json(project / "review" / "tasks" / "done" / f"{task_id}.json", task)
        write_json(result_path, review)

    def _prepare_observed(
        self,
        name: str,
        *,
        project_id: str | None = None,
    ) -> tuple[Path, str]:
        project = self.base / name
        (project / "output").mkdir(parents=True)
        (project / "perception").mkdir(parents=True)
        (project / "review").mkdir(parents=True)
        (project / "repair").mkdir(parents=True)
        shutil.copy2(self.before_media, project / "output" / "final.mp4")
        state = {
            "project": name,
            "created_at": f"2026-08-09T00:00:{len(name):02d}+00:00",
        }
        if project_id:
            state["project_id"] = project_id
        write_json(project / "project_state.json", state)
        digest = f"perception-{name}"
        perception = self._perception(digest)
        plan = self._plan(digest)
        review = self._review(project, "fix")
        qa = {"ok": True, "errors": []}
        repair_plan = {
            "schema_version": 1,
            "project": name,
            "source_reports": ["review.json", "qa_report.json"],
            "actions": [
                {
                    "id": "repair-001",
                    "type": "adjust_trim",
                    "segment_id": "segment-1",
                    "after": {"duration": 0.5},
                    "reason": "trim this segment",
                }
            ],
            "needs_human": [],
        }
        repair_diff = {
            "schema_version": 1,
            "project": name,
            "changes": [
                {
                    "action_id": "repair-001",
                    "type": "adjust_trim",
                    "segment_id": "segment-1",
                    "before": {"duration": 1.0, "source": "raw_video/clip.mp4"},
                    "after": {"duration": 0.5, "source": "raw_video/clip.mp4"},
                    "reason": "trim this segment",
                }
            ],
            "script_changed": False,
            "timeline_changed": False,
            "plan_changed": True,
        }
        write_json(project / "perception" / "perception.json", perception)
        write_json(project / "output" / "edit_plan.json", plan)
        write_json(project / "output" / "qa_report.json", qa)
        write_json(project / "review" / "review.json", review)
        self._write_durable_review(project, review)
        write_json(project / "repair" / "repair_plan.json", repair_plan)
        write_json(project / "repair" / "repair_diff.json", repair_diff)
        captured = capture_observed_repair(
            project,
            review_before=review,
            qa_before=qa,
            perception_before=perception,
            plan_before=plan,
            repair_plan=repair_plan,
            repair_diff=repair_diff,
        )
        self.assertEqual(captured["record"]["evidence_tier"], TIER_OBSERVED)
        return project, str(captured["record"]["evidence_id"])

    def _complete_after(self, project: Path, *, same_video: bool = False) -> None:
        shutil.copy2(
            self.before_media if same_video else self.after_media,
            project / "output" / "final.mp4",
        )
        digest = f"perception-{project.name}"
        write_json(project / "perception" / "perception.json", self._perception(digest))
        write_json(
            project / "output" / "edit_plan.json",
            self._plan(digest, segment_duration=0.5),
        )
        write_json(project / "output" / "qa_report.json", {"ok": True, "errors": []})
        review = self._review(project, "pass")
        write_json(project / "review" / "review.json", review)
        self._write_durable_review(project, review)

    def _finalize(self, project: Path, evidence_id: str) -> dict[str, Any]:
        return finalize_repair_evidence(
            project,
            evidence_id,
            ffmpeg=str(self.ffmpeg),
            ffprobe=str(self.ffprobe),
        )

    def _decision_project(self, name: str) -> Path:
        project = self.base / name
        (project / "script").mkdir(parents=True)
        (project / "raw_video").mkdir(parents=True)
        (project / "config").mkdir(parents=True)
        (project / "output").mkdir(parents=True)
        (project / "script" / "script.txt").write_text("真实治理验收脚本\n", encoding="utf-8")
        (project / "raw_video" / "source.mp4").write_bytes(b"governance-input")
        write_json(
            project / "config" / "config.json",
            {"duration_seconds": 1.0, "canvas": {"width": 160, "height": 240, "fps": 25}},
        )
        write_json(
            project / "config" / "project_context.json",
            {
                "video_type": None,
                "client": None,
                "style_profile": None,
                "platform": "抖音",
                "available_metrics": {},
            },
        )
        write_json(
            project / "output" / "edit_plan.json",
            {
                "duration_seconds": 1.0,
                "segments": [
                    {
                        "id": "segment-1",
                        "duration": 1.0,
                        "timeline_start": 0.0,
                        "timeline_end": 1.0,
                    }
                ],
            },
        )
        write_json(
            project / "project_state.json",
            {"project_id": f"project-{name}", "project": name, "stage": "PLAN"},
        )
        return project

    def test_real_repair_chain_promotes_and_is_idempotent(self) -> None:
        project, evidence_id = self._prepare_observed("project-a")
        review = json.loads((project / "review" / "review.json").read_text(encoding="utf-8"))
        qa = json.loads((project / "output" / "qa_report.json").read_text(encoding="utf-8"))
        perception = json.loads((project / "perception" / "perception.json").read_text(encoding="utf-8"))
        plan = json.loads((project / "output" / "edit_plan.json").read_text(encoding="utf-8"))
        repair_plan = json.loads((project / "repair" / "repair_plan.json").read_text(encoding="utf-8"))
        repair_diff = json.loads((project / "repair" / "repair_diff.json").read_text(encoding="utf-8"))
        repeated = capture_observed_repair(
            project,
            review_before=review,
            qa_before=qa,
            perception_before=perception,
            plan_before=plan,
            repair_plan=repair_plan,
            repair_diff=repair_diff,
        )
        self.assertTrue(repeated["reused"])

        self._complete_after(project)
        final = self._finalize(project, evidence_id)
        self.assertTrue(final["ok"], final.get("error"))
        record = final["record"]
        self.assertEqual(record["evidence_tier"], TIER_PRODUCTION_VERIFIED)
        self.assertEqual([item["to"] for item in record["tier_history"]], [TIER_OBSERVED, TIER_PRODUCTION_VERIFIED])
        self.assertNotEqual(record["video"]["before"]["sha256"], record["video"]["after"]["sha256"])
        self.assertEqual(record["verification"]["status"], "passed")

        first_sync = sync_verified_evidence(project, knowledge_root=self.knowledge_root)
        second_sync = sync_verified_evidence(project, knowledge_root=self.knowledge_root)
        self.assertTrue(first_sync["ok"])
        self.assertTrue(second_sync["ok"])
        self.assertEqual(len(list((self.knowledge_root / "repair_log").glob("*.json"))), 1)

    def test_production_evidence_seals_applied_planner_memory_provenance(self) -> None:
        project, evidence_id = self._prepare_observed("planner-memory-evidence")
        self._complete_after(project)
        (project / "script").mkdir(parents=True, exist_ok=True)
        (project / "config").mkdir(parents=True, exist_ok=True)
        (project / "script" / "script.txt").write_text("planner memory evidence", encoding="utf-8")
        write_json(
            project / "config" / "config.json",
            {"video_os": {"planner_memory": {"mode": "advisory"}}},
        )
        install_formal_rule(
            self.knowledge_root,
            rule_key="production-memory-shot-duration",
            expression={"metric": "shot_duration_s", "operator": "<=", "value": 0.5},
            status="active",
        )
        digest = f"perception-{project.name}"
        perception = {
            "schema_version": 1,
            "provider": "real-test-provider",
            "input_signature": {"digest_sha256": digest},
            "sources": [
                {
                    "source": "raw_video/clip.mp4",
                    "duration": 2.0,
                    "segments": [
                        {
                            "id": "visual-1",
                            "safe_start": 0.0,
                            "safe_end": 1.0,
                            "summary": "primary",
                            "semantic_tags": ["product"],
                            "subjects": [],
                            "objects": ["product"],
                            "actions": [],
                            "quality": {"usable": True, "score": 0.95},
                            "confidence": 0.95,
                            "visual_fingerprint": "fp-1",
                        },
                        {
                            "id": "visual-2",
                            "safe_start": 1.0,
                            "safe_end": 2.0,
                            "summary": "secondary",
                            "semantic_tags": ["product"],
                            "subjects": [],
                            "objects": ["product"],
                            "actions": [],
                            "quality": {"usable": True, "score": 0.95},
                            "confidence": 0.95,
                            "visual_fingerprint": "fp-2",
                        },
                    ],
                }
            ],
        }
        base_plan = self._plan(digest, segment_duration=1.0)
        base_plan["segments"][0]["matched_tags"] = ["product"]
        base_plan["segments"][0]["source_start"] = 0.0
        base_plan["segments"][0]["source_duration"] = 2.0
        base_plan["segments"][0]["selection"].update(
            {
                "summary": "primary",
                "semantic_tags": ["product"],
                "subjects": [],
                "objects": ["product"],
                "actions": [],
                "safe_start": 0.0,
                "safe_end": 1.0,
                "visual_fingerprint": "fp-1",
            }
        )
        config = load_config(project)
        final_plan, context, application, _shadow = build_planner_memory(
            project,
            config,
            base_plan,
            perception,
            knowledge_root=self.knowledge_root,
        )
        self.assertTrue(final_plan["memory"]["memory_applied"])
        write_json(project / "perception" / "perception.json", perception)
        write_json(project / "output" / "edit_plan.base.json", base_plan)
        write_json(project / "output" / "memory_context.json", context)
        write_json(project / "output" / "memory_application.json", application)
        write_json(project / "output" / "edit_plan.json", final_plan)

        finalized = self._finalize(project, evidence_id)
        self.assertTrue(finalized["ok"], finalized.get("error"))
        record = finalized["record"]
        self.assertTrue(record["planner_memory"]["memory_applied"])
        self.assertEqual(
            record["planner_memory"]["applied_rules"],
            final_plan["memory"]["applied_rules"],
        )
        references = record["provenance"]["references"]
        for label in (
            "plan_after",
            "base_plan_after",
            "memory_context_after",
            "memory_application_after",
        ):
            self.assertIn(label, references)

    def test_two_real_independent_projects_produce_one_candidate(self) -> None:
        evidence_ids: list[str] = []
        for name in ("project-a", "project-b"):
            project, evidence_id = self._prepare_observed(name)
            self._complete_after(project)
            result = self._finalize(project, evidence_id)
            self.assertTrue(result["ok"], result.get("error"))
            self.assertTrue(sync_verified_evidence(project, knowledge_root=self.knowledge_root)["ok"])
            evidence_ids.append(evidence_id)
        candidates, stats = build_candidates(self.knowledge_root)
        self.assertEqual(stats["production_evidence_collected"], 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["expression"]["metric"], "shot_duration_s")
        self.assertEqual(candidates[0]["project_count"], 2)
        self.assertEqual(candidates[0]["evidence_count"], 2)
        self.assertEqual(candidates[0]["status"], "candidate")
        self.assertNotIn("approved", candidates[0]["status"])
        self.assertEqual(len(set(evidence_ids)), 2)

    def test_real_evidence_drives_inactive_rule_and_two_project_decisions(self) -> None:
        for name in ("governance-evidence-a", "governance-evidence-b"):
            project, evidence_id = self._prepare_observed(name)
            self._complete_after(project)
            finalized = self._finalize(project, evidence_id)
            self.assertTrue(finalized["ok"], finalized.get("error"))
            self.assertTrue(sync_verified_evidence(project, knowledge_root=self.knowledge_root)["ok"])

        extracted = extract_rule_candidates(self.knowledge_root)
        self.assertEqual(extracted["candidate_count"], 1)
        candidate_id = next((self.knowledge_root / "rule_candidates").glob("*.json")).stem
        approved = approve_rule(
            self.knowledge_root,
            candidate_id,
            reviewer="production-governance-reviewer",
            reason="two independent production gates support human review",
        )
        self.assertTrue(approved["ok"], approved)
        rule_path = Path(approved["rule_file"])
        rule_before = rule_path.read_bytes()
        rule = json.loads(rule_before)
        self.assertEqual(rule["status"], "inactive")
        self.assertFalse(rule["active"])

        accept_project = self._decision_project("governance-accept")
        reject_project = self._decision_project("governance-reject")
        accept_plan = (accept_project / "output" / "edit_plan.json").read_bytes()
        reject_plan = (reject_project / "output" / "edit_plan.json").read_bytes()
        accept_suggestion = generate_memory_suggestions(accept_project, self.knowledge_root)["suggestions"][0]
        reject_suggestion = generate_memory_suggestions(reject_project, self.knowledge_root)["suggestions"][0]
        record_decision(
            accept_project,
            self.knowledge_root,
            suggestion_id=accept_suggestion["suggestion_id"],
            decision="accept",
            reviewer="production-accept-reviewer",
            reason="accepted for the first independent project",
        )
        record_decision(
            reject_project,
            self.knowledge_root,
            suggestion_id=reject_suggestion["suggestion_id"],
            decision="reject",
            reviewer="production-reject-reviewer",
            reason="rejected for the second independent project",
        )

        history = list_governance_history(self.knowledge_root, rule_id=rule["rule_id"])
        self.assertEqual(history["decision_count"], 2)
        self.assertEqual(history["rules"][0]["accept"], 1)
        self.assertEqual(history["rules"][0]["reject"], 1)
        self.assertEqual(rule_path.read_bytes(), rule_before)
        self.assertEqual((accept_project / "output" / "edit_plan.json").read_bytes(), accept_plan)
        self.assertEqual((reject_project / "output" / "edit_plan.json").read_bytes(), reject_plan)

    def test_same_project_identity_cannot_fake_two_sources(self) -> None:
        for name in ("repeat-a", "repeat-b"):
            project, evidence_id = self._prepare_observed(name, project_id="project-shared")
            self._complete_after(project)
            result = self._finalize(project, evidence_id)
            self.assertTrue(result["ok"], result.get("error"))
            self.assertTrue(sync_verified_evidence(project, knowledge_root=self.knowledge_root)["ok"])
        candidates, stats = build_candidates(self.knowledge_root)
        self.assertEqual(stats["production_evidence_collected"], 2)
        self.assertEqual(candidates, [])
        self.assertTrue(any("independent project" in reason for reason in stats["reasons"]))

    def test_attack_forged_review_reference_fails_closed(self) -> None:
        project, evidence_id = self._prepare_observed("forged-review")
        self._complete_after(project)
        path = project / "repair" / "evidence" / evidence_id / "evidence.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["provenance"]["references"]["review_before"]["path"] = "review/forged.json"
        write_json(path, record)
        result = self._finalize(project, evidence_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["record"]["evidence_tier"], TIER_OBSERVED)

    def test_attack_wrong_video_signature_fails_closed(self) -> None:
        project, evidence_id = self._prepare_observed("wrong-signature")
        self._complete_after(project)
        path = project / "repair" / "evidence" / evidence_id / "evidence.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["video"]["before"]["signature"]["sample_sha256"] = "0" * 64
        write_json(path, record)
        result = self._finalize(project, evidence_id)
        self.assertFalse(result["ok"])
        self.assertIn("identity", result["error"])

    def test_attack_missing_qa_fails_closed(self) -> None:
        project, evidence_id = self._prepare_observed("missing-qa")
        self._complete_after(project)
        (project / "output" / "qa_report.json").unlink()
        result = self._finalize(project, evidence_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["record"]["evidence_tier"], TIER_OBSERVED)

    def test_attack_same_before_after_media_fails_closed(self) -> None:
        project, evidence_id = self._prepare_observed("same-media")
        self._complete_after(project, same_video=True)
        result = self._finalize(project, evidence_id)
        self.assertFalse(result["ok"])
        self.assertIn("identical content", result["error"])

    def test_attack_demo_tier_cannot_upgrade(self) -> None:
        project, evidence_id = self._prepare_observed("demo-tier")
        self._complete_after(project)
        path = project / "repair" / "evidence" / evidence_id / "evidence.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["evidence_tier"] = "demo"
        write_json(path, record)
        result = self._finalize(project, evidence_id)
        self.assertFalse(result["ok"])
        self.assertIn("never be upgraded", result["error"])

    def test_unavailable_knowledge_keeps_verified_local_record(self) -> None:
        project, evidence_id = self._prepare_observed("knowledge-down")
        self._complete_after(project)
        result = self._finalize(project, evidence_id)
        self.assertTrue(result["ok"], result.get("error"))
        unavailable = sync_verified_evidence(
            project,
            knowledge_root=self.base / "not-initialized",
        )
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertTrue(unavailable["warning"])
        local = json.loads(
            (project / "repair" / "evidence" / evidence_id / "evidence.json").read_text(encoding="utf-8")
        )
        self.assertEqual(local["evidence_tier"], TIER_PRODUCTION_VERIFIED)
        self.assertEqual(local["knowledge_sync"]["status"], "unavailable")

    def test_tampered_knowledge_record_is_not_candidate_evidence(self) -> None:
        project, evidence_id = self._prepare_observed("tampered-knowledge")
        self._complete_after(project)
        result = self._finalize(project, evidence_id)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(sync_verified_evidence(project, knowledge_root=self.knowledge_root)["ok"])
        path = self.knowledge_root / "repair_log" / f"{evidence_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["actions"][0]["value"] = 0.1
        write_json(path, record)
        candidates, stats = build_candidates(self.knowledge_root)
        self.assertEqual(candidates, [])
        self.assertEqual(stats["production_evidence_excluded_invalid"], 1)

    def test_manual_structured_evidence_is_human_verified_not_production(self) -> None:
        payload = {
            "project_id": "project-manual",
            "project": "manual-project",
            "run_id": "manual-run-001",
            "timestamp": "2026-08-09T00:00:00+00:00",
            "actions": [
                {
                    "action_id": "manual-001",
                    "type": "manual_edit",
                    "issue_refs": [],
                    "target": {
                        "segment_id": "segment-1",
                        "time_range": {"start": 0.0, "end": 1.0},
                    },
                    "scope": {"kind": "segment"},
                    "field": "segment.duration",
                    "metric": "shot_duration_s",
                    "operator": "<=",
                    "before": 1.0,
                    "after": 0.5,
                    "value": 0.5,
                    "reason": "human shortened the shot",
                }
            ],
            "provenance": {"references": {}, "chain_digest": None},
        }
        written = record_manual_evidence(
            self.knowledge_root,
            payload,
            reviewer="editor@example.test",
            verification_reason="editor confirmed the structured values",
        )
        record = json.loads(Path(written["path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["evidence_tier"], TIER_HUMAN_VERIFIED)
        self.assertEqual([item["to"] for item in record["tier_history"]], [TIER_OBSERVED, TIER_HUMAN_VERIFIED])
        candidates, stats = build_candidates(self.knowledge_root)
        self.assertEqual(candidates, [])
        self.assertEqual(stats["production_evidence_collected"], 0)

        forged = dict(record)
        forged["evidence_tier"] = TIER_PRODUCTION_VERIFIED
        with self.assertRaises(EvidenceValidationError):
            write_evidence_record(self.knowledge_root, forged)

    def test_manual_evidence_cli_accepts_structured_fields(self) -> None:
        payload = {
            "project_id": "project-cli",
            "project": "cli-project",
            "run_id": "manual-run-cli",
            "actions": [
                {
                    "action_id": "manual-cli-001",
                    "type": "manual_edit",
                    "issue_refs": [],
                    "target": {
                        "segment_id": "segment-2",
                        "time_range": {"start": 1.0, "end": 2.0},
                    },
                    "scope": {"kind": "segment"},
                    "field": "segment.duration",
                    "metric": "shot_duration_s",
                    "operator": "<=",
                    "before": 1.0,
                    "after": 0.75,
                    "value": 0.75,
                    "reason": "manual timing correction",
                }
            ],
            "provenance": {"references": {}, "chain_digest": None},
        }
        input_path = self.base / "manual-evidence.json"
        write_json(input_path, payload)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "knowledge_tools.py"),
                "--root",
                str(self.knowledge_root),
                "record-evidence",
                "--input",
                str(input_path),
                "--reviewer",
                "editor@example.test",
                "--reason",
                "confirmed in the editing session",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["evidence_tier"], TIER_HUMAN_VERIFIED)


if __name__ == "__main__":
    unittest.main()
