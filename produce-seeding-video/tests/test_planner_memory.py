from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from governance_fixtures import install_formal_rule  # noqa: E402
from video_os_core.knowledge import init_knowledge  # noqa: E402
from video_os_core import project_manager  # noqa: E402
from video_os_core.planner_memory import (  # noqa: E402
    APPLICATION_HASH_ALGORITHM,
    _seal,
    validate_planner_memory_artifacts,
)
from video_os_core.rule_approval import _seal_rule  # noqa: E402
from video_pipeline import pipeline  # noqa: E402
from video_pipeline.perception import perception_input_signature, source_signature  # noqa: E402


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class PlannerMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="planner-memory-")
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.knowledge = self.root / "knowledge"
        for relative in ("script", "material", "config", "output", "perception"):
            (self.project / relative).mkdir(parents=True, exist_ok=True)
        init_knowledge(self.knowledge)
        self.video = self.project / "material" / "clip.mp4"
        self.video.write_bytes(b"planner-memory-source" * 4096)
        (self.project / "script" / "script.txt").write_text(
            "show the product clearly and ask the viewer to buy", encoding="utf-8"
        )
        self.config = json.loads(
            (ROOT / "assets" / "default-config.json").read_text(encoding="utf-8")
        )
        self.config["duration_seconds"] = 4.0
        self.config["template_segments"] = [
            {
                "id": "hook",
                "start": 0.0,
                "end": 2.0,
                "intent": "show product",
                "preferred_tags": ["hook", "product"],
            },
            {
                "id": "cta",
                "start": 2.0,
                "end": 4.0,
                "intent": "call to action",
                "preferred_tags": ["cta", "product"],
            },
        ]
        self.config["subtitles"]["enabled"] = False
        self.config["bgm"]["enabled"] = False
        self.config["sound_effects"]["enabled"] = False
        self.media = [
            {
                "path": "material/clip.mp4",
                "group": "material",
                "has_video": True,
                "has_audio": False,
                "duration": 16.0,
                "tags": ["product"],
            }
        ]
        write_json(
            self.project / "output" / "analysis.json",
            {
                "schema_version": 1,
                "script": {"sentences": ["show the product", "buy now"]},
                "media": self.media,
                "references": [],
                "perception": {"available": False},
                "warnings": [],
            },
        )
        self.perception = {
            "schema_version": 1,
            "status": "done",
            "input_signature": perception_input_signature(self.project, self.media),
            "provider": {"name": "fixture-provider", "model": "real-contract"},
            "sources": [
                {
                    "source": "material/clip.mp4",
                    "duration": 16.0,
                    "signature": source_signature(self.video),
                    "segments": [
                        self._segment("hook-a", 0.0, ["hook", "product"]),
                        self._segment("cta-a", 2.5, ["cta", "product"]),
                        self._segment("hook-b", 5.0, ["hook", "product"]),
                        self._segment("cta-b", 7.5, ["cta", "product"]),
                        self._segment("hook-c", 10.0, ["hook", "product"]),
                        self._segment("cta-c", 12.5, ["cta", "product"]),
                    ],
                }
            ],
        }
        write_json(self.project / "perception" / "perception.json", self.perception)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _segment(segment_id: str, start: float, tags: list[str]) -> dict:
        return {
            "id": segment_id,
            "start": start,
            "end": start + 2.4,
            "safe_start": start + 0.1,
            "safe_end": start + 2.3,
            "summary": f"observed {segment_id}",
            "semantic_tags": tags,
            "subjects": ["person"],
            "objects": ["product"],
            "actions": ["present"],
            "script_alignment": [],
            "quality": {"usable": True, "score": 0.95, "issues": []},
            "confidence": 0.95,
            "visual_fingerprint": f"fp-{segment_id}",
        }

    def _set_mode(self, mode: str) -> None:
        self.config["video_os"]["planner_memory"]["mode"] = mode
        write_json(self.project / "config" / "config.json", self.config)

    def _install_rule(
        self,
        key: str,
        *,
        operator: str = "<=",
        value: float = 1.0,
        status: str = "active",
    ) -> dict:
        return install_formal_rule(
            self.knowledge,
            rule_key=key,
            expression={
                "metric": "shot_duration_s",
                "operator": operator,
                "value": value,
            },
            status=status,
        )

    def _run(self, mode: str, *, knowledge: Path | None = None) -> dict:
        self._set_mode(mode)
        return pipeline.run_plan_stage(
            self.project,
            knowledge_root=self.knowledge if knowledge is None else knowledge,
        )

    def _artifact(self, name: str) -> dict:
        return json.loads((self.project / "output" / name).read_text(encoding="utf-8"))

    @staticmethod
    def _executable(plan: dict) -> dict:
        clean = deepcopy(plan)
        clean.pop("memory", None)
        return clean

    def test_real_off_shadow_advisory_ab(self) -> None:
        self._install_rule("shot-max-one-second")
        off = self._run("off")["payload"]
        base_a = self._executable(off)
        self.assertFalse(off["memory"]["memory_applied"])

        shadow = self._run("shadow")["payload"]
        shadow_application = self._artifact("memory_application.json")
        self.assertEqual(self._executable(shadow), base_a)
        self.assertIn("would_apply", [item["result"] for item in shadow_application["decisions"]])
        self.assertTrue(self._artifact("memory_shadow_report.json")["final_plan_semantically_equal"])

        advisory = self._run("advisory")["payload"]
        application = self._artifact("memory_application.json")
        self.assertTrue(advisory["memory"]["memory_applied"])
        self.assertNotEqual(self._executable(advisory), base_a)
        self.assertIn("applied", [item["result"] for item in application["decisions"]])
        self.assertTrue(application["changes"])
        self.assertTrue(all(float(item["duration"]) <= 1.0 for item in advisory["segments"]))
        self.assertEqual(
            validate_planner_memory_artifacts(
                self.project, self.config, knowledge_root=self.knowledge
            ),
            [],
        )

    def test_valid_rule_that_cannot_be_applied_is_explicitly_unsafe(self) -> None:
        self._install_rule("unsafe-short-shot", value=0.4)
        result = self._run("advisory")["payload"]
        application = self._artifact("memory_application.json")
        self.assertFalse(result["memory"]["memory_applied"])
        self.assertEqual(self._executable(result), self._artifact("edit_plan.base.json"))
        self.assertIn("unsafe", [item["result"] for item in application["decisions"]])

    def test_inactive_deprecated_and_revoked_rules_never_enter_context(self) -> None:
        for status in ("inactive", "deprecated", "revoked"):
            with self.subTest(status=status):
                root = self.root / f"knowledge-{status}"
                init_knowledge(root)
                install_formal_rule(
                    root,
                    rule_key=f"excluded-{status}",
                    expression={"metric": "shot_duration_s", "operator": "<=", "value": 1.0},
                    status=status,
                )
                result = self._run("advisory", knowledge=root)["payload"]
                self.assertFalse(result["memory"]["memory_applied"])
                self.assertEqual(self._artifact("memory_context.json")["rules"], [])

    def test_rule_conflict_falls_back_to_base_without_failing_plan(self) -> None:
        self._install_rule("upper-bound", operator="<=", value=1.0)
        self._install_rule("lower-bound", operator=">=", value=3.0)
        result = self._run("advisory")["payload"]
        application = self._artifact("memory_application.json")
        self.assertFalse(result["memory"]["memory_applied"])
        self.assertEqual(self._executable(result), self._artifact("edit_plan.base.json"))
        self.assertEqual(
            [item["result"] for item in application["decisions"]],
            ["conflict", "conflict"],
        )
        self.assertTrue(application["warnings"])

    def test_missing_knowledge_root_falls_back_with_warning(self) -> None:
        result = self._run("advisory", knowledge=self.root / "missing")["payload"]
        self.assertFalse(result["memory"]["memory_applied"])
        self.assertEqual(result["memory"]["fallback_reason"], "knowledge_root_unavailable")
        self.assertTrue(result["memory"]["warning"])

    def test_memory_application_validation_failure_uses_sealed_base_fallback(self) -> None:
        self._install_rule("validation-fallback")
        with patch.object(
            pipeline,
            "validate_planner_memory_artifacts",
            side_effect=[["synthetic invalid Memory diff"], []],
        ):
            result = self._run("advisory")["payload"]
        self.assertFalse(result["memory"]["memory_applied"])
        self.assertEqual(
            result["memory"]["fallback_reason"],
            "memory_application_validation_failed",
        )
        self.assertEqual(self._executable(result), self._artifact("edit_plan.base.json"))
        self.assertTrue(result["memory"]["warning"])
        self.assertEqual(
            validate_planner_memory_artifacts(
                self.project,
                self.config,
                knowledge_root=self.knowledge,
            ),
            [],
        )

    def test_project_status_forwards_the_explicit_knowledge_root(self) -> None:
        project_manager.ensure_project_state(self.project)
        with patch.object(
            project_manager,
            "refresh_state_validity",
            return_value=False,
        ) as refresh:
            project_manager.project_status(
                self.project,
                knowledge_root=self.knowledge,
            )
        self.assertEqual(refresh.call_args.kwargs["knowledge_root"], self.knowledge)

    def test_new_revision_or_content_cannot_reuse_old_activation(self) -> None:
        for mutation in ("revision", "content"):
            with self.subTest(mutation=mutation):
                root = self.root / f"knowledge-{mutation}"
                init_knowledge(root)
                rule = install_formal_rule(
                    root,
                    rule_key=f"activated-{mutation}",
                    expression={"metric": "shot_duration_s", "operator": "<=", "value": 1.0},
                    status="active",
                )
                rule_path = next((root / "editing_rules").glob("*.json"))
                if mutation == "revision":
                    rule["revision"] = 2
                    rule["version"] = "v2"
                else:
                    rule["expression"]["value"] = 0.8
                    rule["value"] = 0.8
                write_json(rule_path, _seal_rule(rule))
                result = self._run("advisory", knowledge=root)["payload"]
                self.assertFalse(result["memory"]["memory_applied"])
                self.assertEqual(result["memory"]["fallback_reason"], "rule_integrity_invalid")

    def test_broken_production_evidence_seal_falls_back_to_base(self) -> None:
        self._install_rule("sealed-evidence")
        evidence_path = next((self.knowledge / "repair_log").glob("*.json"))
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["actions"][0]["after"] = 99.0
        write_json(evidence_path, evidence)
        result = self._run("advisory")["payload"]
        self.assertFalse(result["memory"]["memory_applied"])
        self.assertEqual(result["memory"]["fallback_reason"], "rule_integrity_invalid")

    def test_project_perception_and_base_changes_stale_context(self) -> None:
        self._install_rule("stale-bindings")
        self._run("advisory")
        original_script = (self.project / "script" / "script.txt").read_text(encoding="utf-8")
        (self.project / "script" / "script.txt").write_text(
            original_script + " changed", encoding="utf-8"
        )
        errors = validate_planner_memory_artifacts(
            self.project, self.config, knowledge_root=self.knowledge
        )
        self.assertTrue(any("stale" in item or "mismatched" in item for item in errors))
        (self.project / "script" / "script.txt").write_text(original_script, encoding="utf-8")

        self._run("advisory")
        original_perception = self._artifact_from(
            self.project / "perception" / "perception.json"
        )
        changed_perception = deepcopy(original_perception)
        changed_perception["provider"]["model"] = "changed-model"
        write_json(self.project / "perception" / "perception.json", changed_perception)
        errors = validate_planner_memory_artifacts(
            self.project, self.config, knowledge_root=self.knowledge
        )
        self.assertTrue(any("stale" in item or "mismatched" in item for item in errors))
        write_json(self.project / "perception" / "perception.json", original_perception)

        self._run("advisory")
        payload = self._artifact("edit_plan.base.json")
        payload.setdefault("warnings", []).append("tampered base")
        write_json(self.project / "output" / "edit_plan.base.json", payload)
        errors = validate_planner_memory_artifacts(
            self.project, self.config, knowledge_root=self.knowledge
        )
        self.assertTrue(any("stale" in item or "mismatched" in item for item in errors))

    def test_video_content_change_stales_memory_context_independently(self) -> None:
        self._install_rule("stale-video-input")
        self._run("advisory")
        self.video.write_bytes(self.video.read_bytes() + b"changed")
        errors = validate_planner_memory_artifacts(
            self.project,
            self.config,
            knowledge_root=self.knowledge,
        )
        self.assertTrue(
            any("project_input_signature" in item for item in errors),
            errors,
        )

    @staticmethod
    def _artifact_from(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_tampered_application_unrelated_diff_and_false_applied_claim_fail_closed(self) -> None:
        self._install_rule("tamper-protection")
        self._run("advisory")
        application_path = self.project / "output" / "memory_application.json"
        application = self._artifact("memory_application.json")
        application["decisions"][0]["reason"] = "tampered"
        write_json(application_path, application)
        self.assertTrue(
            any(
                "content hash" in item
                for item in validate_planner_memory_artifacts(
                    self.project, self.config, knowledge_root=self.knowledge
                )
            )
        )

        self._run("advisory")
        application = self._artifact("memory_application.json")
        application["changes"] = [
            {
                "op": "replace",
                "affected_slot": "canvas",
                "affected_field": "width",
                "before": 1080,
                "after": 999,
            }
        ]
        write_json(
            application_path,
            _seal(application, "application_signature", APPLICATION_HASH_ALGORITHM),
        )
        self.assertTrue(
            any(
                "unrelated" in item or "unsupported" in item
                for item in validate_planner_memory_artifacts(
                    self.project, self.config, knowledge_root=self.knowledge
                )
            )
        )

        self._run("advisory")
        final = self._artifact("edit_plan.json")
        base = self._artifact("edit_plan.base.json")
        memory = final["memory"]
        write_json(self.project / "output" / "edit_plan.json", {**base, "memory": memory})
        self.assertTrue(
            any(
                "no corresponding diff" in item or "unexplained" in item
                for item in validate_planner_memory_artifacts(
                    self.project, self.config, knowledge_root=self.knowledge
                )
            )
        )

    def test_shadow_cannot_secretly_modify_final_plan(self) -> None:
        self._install_rule("shadow-tamper")
        self._run("shadow")
        final = self._artifact("edit_plan.json")
        final["canvas"]["width"] = 999
        write_json(self.project / "output" / "edit_plan.json", final)
        errors = validate_planner_memory_artifacts(
            self.project, self.config, knowledge_root=self.knowledge
        )
        self.assertTrue(any("shadow mode modified" in item for item in errors))

    def test_resealed_application_cannot_escape_perception_safe_range(self) -> None:
        self._install_rule("unsafe-resealed-diff")
        self._run("advisory")
        application_path = self.project / "output" / "memory_application.json"
        application = self._artifact("memory_application.json")
        segment_change = next(
            item
            for item in application["changes"]
            if item.get("op") == "replace_segment_with_slots"
        )
        segment_change["after"][0]["source_start"] = 99.0
        write_json(
            application_path,
            _seal(application, "application_signature", APPLICATION_HASH_ALGORITHM),
        )
        errors = validate_planner_memory_artifacts(
            self.project, self.config, knowledge_root=self.knowledge
        )
        self.assertTrue(any("safe Perception range" in item for item in errors))

    def test_final_plan_cannot_forge_applied_rule_metadata(self) -> None:
        self._install_rule("forged-applied-metadata")
        self._run("advisory")
        final_path = self.project / "output" / "edit_plan.json"
        final = self._artifact("edit_plan.json")
        final["memory"]["applied_rules"] = [
            {"rule_id": "rule-forged", "revision": 999}
        ]
        write_json(final_path, final)
        errors = validate_planner_memory_artifacts(
            self.project, self.config, knowledge_root=self.knowledge
        )
        self.assertTrue(any("applied Rule metadata" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
