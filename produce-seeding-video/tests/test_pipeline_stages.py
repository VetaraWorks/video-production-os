from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_pipeline import pipeline  # noqa: E402
from video_pipeline.perception import perception_input_signature, source_signature  # noqa: E402


class PipelineStageTests(unittest.TestCase):
    def test_plan_stage_accepts_missing_perception_when_explicitly_disabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pipeline-no-perception-") as temporary:
            project = Path(temporary)
            for relative in ("script", "material", "config", "output"):
                (project / relative).mkdir(parents=True, exist_ok=True)
            (project / "script" / "script.txt").write_text(
                "show the bottle then buy now", encoding="utf-8"
            )
            (project / "material" / "clip.mp4").write_bytes(
                b"metadata-only-planner-input" * 1024
            )
            (project / "output" / "analysis.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "script": {"sentences": ["show the bottle", "buy now"]},
                        "media": [
                            {
                                "path": "material/clip.mp4",
                                "group": "material",
                                "has_video": True,
                                "has_audio": False,
                                "duration": 4.0,
                                "tags": ["product"],
                            }
                        ],
                        "references": [],
                        "perception": {"available": False},
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )
            config = json.loads(
                (ROOT / "assets" / "default-config.json").read_text(encoding="utf-8")
            )
            config["duration_seconds"] = 4.0
            config["template_segments"] = [
                {
                    "id": "hook",
                    "start": 0.0,
                    "end": 2.0,
                    "intent": "show product",
                    "preferred_tags": ["product"],
                },
                {
                    "id": "cta",
                    "start": 2.0,
                    "end": 4.0,
                    "intent": "call to action",
                    "preferred_tags": ["product"],
                },
            ]
            config["subtitles"]["enabled"] = False
            config["bgm"]["enabled"] = False
            config["sound_effects"]["enabled"] = False
            config["perception"]["enabled"] = False
            (project / "config" / "config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )

            result = pipeline.run_plan_stage(project)

            self.assertTrue(result["ok"])
            self.assertEqual(result["stage"], "PLAN")
            self.assertFalse(result["payload"]["memory"]["memory_applied"])
            self.assertEqual(
                result["payload"]["memory"]["fallback_reason"],
                "knowledge_root_unavailable",
            )
            self.assertTrue((project / "output" / "edit_plan.base.json").is_file())
            self.assertTrue((project / "output" / "memory_context.json").is_file())
            self.assertTrue((project / "output" / "memory_application.json").is_file())

    def test_plan_stage_loads_post_analyze_perception_and_records_consumption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pipeline-perception-") as temporary:
            project = Path(temporary)
            (project / "script").mkdir(parents=True)
            (project / "material").mkdir()
            (project / "config").mkdir()
            (project / "output").mkdir()
            (project / "perception").mkdir()
            (project / "script" / "script.txt").write_text(
                "show the bottle then buy now", encoding="utf-8"
            )
            video = project / "material" / "clip.mp4"
            video.write_bytes(b"real-planner-input" * 1024)
            media = [
                {
                    "path": "material/clip.mp4",
                    "group": "material",
                    "has_video": True,
                    "has_audio": False,
                    "duration": 8.0,
                    "tags": ["product"],
                }
            ]
            analysis = {
                "schema_version": 1,
                "script": {
                    "sentences": ["show the bottle", "buy now"],
                },
                "media": media,
                "references": [],
                "perception": {"available": False},
                "warnings": [],
            }
            (project / "output" / "analysis.json").write_text(
                json.dumps(analysis), encoding="utf-8"
            )
            config = json.loads(
                (ROOT / "assets" / "default-config.json").read_text(encoding="utf-8")
            )
            config["duration_seconds"] = 4.0
            config["template_segments"] = [
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
            config["subtitles"]["enabled"] = False
            config["bgm"]["enabled"] = False
            config["sound_effects"]["enabled"] = False
            (project / "config" / "config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )

            input_signature = perception_input_signature(project, media)
            source_sig = source_signature(video)
            perception = {
                "schema_version": 1,
                "status": "done",
                "input_signature": input_signature,
                "provider": {"name": "integration", "model": "fixture-contract"},
                "sources": [
                    {
                        "source": "material/clip.mp4",
                        "duration": 8.0,
                        "signature": source_sig,
                        "segments": [
                            self._perception_segment(
                                "product-shot", 0.0, 2.5, ["hook", "product"], ["bottle"], ["reveal"]
                            ),
                            self._perception_segment(
                                "cta-shot", 3.0, 5.5, ["cta", "product"], ["bottle"], ["point_to_cart"]
                            ),
                        ],
                    }
                ],
            }
            (project / "perception" / "perception.json").write_text(
                json.dumps(perception), encoding="utf-8"
            )

            result = pipeline.run_plan_stage(project)
            plan = result["payload"]
            selections = [segment["selection"] for segment in plan["segments"]]
            self.assertTrue(all(item["mode"] == "perception" for item in selections))
            self.assertEqual(selections[0]["objects"], ["bottle"])
            self.assertEqual(selections[1]["actions"], ["point_to_cart"])
            self.assertEqual(
                plan["perception"]["input_signature_digest"],
                input_signature["digest_sha256"],
            )
            self.assertEqual(
                plan["perception"]["selected_segment_ids"],
                ["product-shot", "cta-shot"],
            )

    @staticmethod
    def _perception_segment(
        segment_id: str,
        start: float,
        end: float,
        tags: list[str],
        objects: list[str],
        actions: list[str],
    ) -> dict:
        return {
            "id": segment_id,
            "start": start,
            "end": end,
            "safe_start": start + 0.1,
            "safe_end": end - 0.1,
            "summary": f"observed {segment_id}",
            "semantic_tags": tags,
            "subjects": ["person"],
            "objects": objects,
            "actions": actions,
            "script_alignment": [],
            "quality": {"usable": True, "score": 0.95, "issues": []},
            "confidence": 0.95,
            "visual_fingerprint": f"fp-{segment_id}",
        }

    def test_legacy_run_project_invokes_each_stage_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pipeline-stage-") as temporary:
            project = Path(temporary)
            analysis_result = {
                "analysis": str(project / "output" / "analysis.json"),
            }
            plan_result = {
                "edit_plan": str(project / "output" / "edit_plan.json"),
                "subtitles": None,
                "warnings": [],
                "payload": {"warnings": []},
            }
            render_result = {"final": str(project / "output" / "final.mp4")}
            qa_result = {
                "qa_report": str(project / "output" / "qa_report.json"),
                "payload": {"ok": True, "errors": []},
            }
            with (
                mock.patch.object(
                    pipeline,
                    "load_config",
                    return_value={"jianying_export": {"enabled": False}},
                ),
                mock.patch.object(
                    pipeline,
                    "run_analysis_stage",
                    return_value=analysis_result,
                ) as analyze,
                mock.patch.object(
                    pipeline,
                    "run_plan_stage",
                    return_value=plan_result,
                ) as plan,
                mock.patch.object(
                    pipeline,
                    "run_render_stage",
                    return_value=render_result,
                ) as render,
                mock.patch.object(
                    pipeline,
                    "run_qa_stage",
                    return_value=qa_result,
                ) as qa,
            ):
                result = pipeline.run_project(project)

            self.assertTrue(result["ok"])
            analyze.assert_called_once()
            plan.assert_called_once()
            render.assert_called_once()
            qa.assert_called_once()

    def test_run_stage_dispatches_only_requested_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pipeline-dispatch-") as temporary:
            project = Path(temporary)
            with (
                mock.patch.object(
                    pipeline,
                    "run_analysis_stage",
                    return_value={"ok": True, "stage": "ANALYZE"},
                ) as analyze,
                mock.patch.object(pipeline, "run_plan_stage") as plan,
                mock.patch.object(pipeline, "run_render_stage") as render,
                mock.patch.object(pipeline, "run_qa_stage") as qa,
            ):
                result = pipeline.run_stage(project, "ANALYZE")

            self.assertEqual(result["stage"], "ANALYZE")
            analyze.assert_called_once()
            plan.assert_not_called()
            render.assert_not_called()
            qa.assert_not_called()


if __name__ == "__main__":
    unittest.main()
