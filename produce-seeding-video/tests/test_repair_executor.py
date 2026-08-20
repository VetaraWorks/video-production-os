from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from repair.repair_executor import (  # noqa: E402
    RepairExecutionError,
    apply_repair_plan,
    next_repair_version,
)
from video_pipeline.config import load_config  # noqa: E402


DEFAULT_CONFIG = ROOT / "assets" / "default-config.json"


def make_project(base: Path) -> Path:
    project = base / "demo"
    (project / "script").mkdir(parents=True, exist_ok=True)
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "output").mkdir(parents=True, exist_ok=True)
    (project / "raw_video").mkdir(parents=True, exist_ok=True)
    (project / "material").mkdir(parents=True, exist_ok=True)
    (project / "script" / "script.txt").write_text("hello\nworld\n", encoding="utf-8")
    (project / "raw_video" / "a.mp4").write_bytes(b"media-a" * 100)
    (project / "material" / "b.mp4").write_bytes(b"media-b" * 100)
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    config["jianying_export"]["enabled"] = False
    (project / "config" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    plan = {
        "schema_version": 2,
        "duration_seconds": 2.0,
        "canvas": {"width": 360, "height": 640, "fps": 24},
        "segments": [
            {
                "id": "hook",
                "timeline_start": 0.0,
                "timeline_end": 1.0,
                "duration": 1.0,
                "source": "raw_video/a.mp4",
                "source_start": 0.0,
                "source_duration": 1.5,
                "has_audio": True,
                "loop": False,
            }
        ],
        "subtitles": {"enabled": True, "filename": "subtitles.ass"},
        "render": {"output_filename": "final.mp4"},
    }
    (project / "output" / "edit_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    return project


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, project_dir: Path, ffmpeg, ffprobe) -> None:
        self.calls.append(Path(project_dir))
        (project_dir / "output" / "qa_report.json").write_text(
            json.dumps({"ok": True, "errors": []}), encoding="utf-8"
        )


class RepairExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="repair-exec-")
        self.base = Path(self._tmp.name)
        self.project = make_project(self.base)
        self.projects_root = self.base / "projects"
        self.config = load_config(self.project)
        self.fake = FakeRunner()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan(self, actions: list[dict]) -> dict:
        return {
            "schema_version": 1,
            "project": "demo",
            "source_reports": ["review.json"],
            "actions": actions,
            "needs_human": [],
        }

    def test_replace_clip_creates_new_version_and_preserves_original(self) -> None:
        original = (self.project / "output" / "edit_plan.json").read_text(encoding="utf-8")
        repair_plan = self._plan(
            [
                {
                    "id": "repair-001",
                    "type": "replace_clip",
                    "segment_id": "hook",
                    "after": {
                        "source": "material/b.mp4",
                        "source_start": 0.2,
                        "source_duration": 1.5,
                        "duration": 1.0,
                        "has_audio": False,
                        "loop": False,
                    },
                }
            ]
        )
        result = apply_repair_plan(
            self.project,
            repair_plan,
            self.config,
            self.projects_root,
            run_pipeline=self.fake,
            version="v001",
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["version"], "v001")
        self.assertEqual(self.fake.calls, [self.project])

        # Original output/edit_plan.json untouched; override holds the repaired copy.
        self.assertEqual(
            (self.project / "output" / "edit_plan.json").read_text(encoding="utf-8"),
            original,
        )
        override = json.loads(
            (self.project / "config" / "edit_plan.json").read_text(encoding="utf-8")
        )
        segment = override["segments"][0]
        self.assertEqual(segment["source"], "material/b.mp4")
        self.assertEqual(segment["source_start"], 0.2)
        self.assertEqual(segment["selection"]["mode"], "repair")

        diff = json.loads(
            (self.project / "repair" / "repair_diff.json").read_text(encoding="utf-8")
        )
        self.assertTrue(any(item.get("type") == "replace_clip" for item in diff["changes"]))

        snapshot = self.projects_root / "demo" / "snapshots" / "v001"
        self.assertTrue((snapshot / "manifest.json").is_file())
        self.assertTrue((snapshot / "VERSION.md").is_file())
        self.assertTrue((snapshot / "repair_plan.json").is_file())
        self.assertTrue((snapshot / "repair_diff.json").is_file())

    def test_adjust_trim_rejects_source_change(self) -> None:
        repair_plan = self._plan(
            [
                {
                    "id": "repair-002",
                    "type": "adjust_trim",
                    "segment_id": "hook",
                    "after": {
                        "source": "material/b.mp4",
                        "source_start": 0.1,
                        "source_duration": 1.5,
                        "duration": 1.0,
                    },
                }
            ]
        )
        with self.assertRaises(RepairExecutionError):
            apply_repair_plan(
                self.project,
                repair_plan,
                self.config,
                self.projects_root,
                run_pipeline=self.fake,
            )
        self.assertEqual(self.fake.calls, [])

    def test_trim_exceeding_source_rejected(self) -> None:
        repair_plan = self._plan(
            [
                {
                    "id": "repair-003",
                    "type": "adjust_trim",
                    "segment_id": "hook",
                    "after": {
                        "source": "raw_video/a.mp4",
                        "source_start": 1.4,
                        "source_duration": 1.5,
                        "duration": 1.0,
                    },
                }
            ]
        )
        with self.assertRaises(RepairExecutionError):
            apply_repair_plan(
                self.project,
                repair_plan,
                self.config,
                self.projects_root,
                run_pipeline=self.fake,
            )
        self.assertEqual(self.fake.calls, [])

    def test_fix_subtitle_text_patches_script(self) -> None:
        (self.project / "script" / "script.txt").write_text(
            "你好世界\n再见\n", encoding="utf-8"
        )
        repair_plan = self._plan(
            [
                {
                    "id": "repair-004",
                    "type": "fix_subtitle",
                    "kind": "text",
                    "segment_id": "hook",
                    "text_from": "你好世界",
                    "text_to": "你好，世界",
                }
            ]
        )
        result = apply_repair_plan(
            self.project,
            repair_plan,
            self.config,
            self.projects_root,
            run_pipeline=self.fake,
            version="v001",
        )
        self.assertTrue(result["applied"])
        self.assertTrue(result["script_changed"])
        self.assertIn(
            "你好，世界",
            (self.project / "script" / "script.txt").read_text(encoding="utf-8"),
        )

    def test_fix_subtitle_timing_patches_timeline(self) -> None:
        (self.project / "speech_timeline.json").write_text(
            json.dumps(
                {"timing_mode": "speech-timeline", "cues": [{"start": 1.0, "end": 2.0, "text": "x"}]}
            ),
            encoding="utf-8",
        )
        repair_plan = self._plan(
            [
                {
                    "id": "repair-005",
                    "type": "fix_subtitle",
                    "kind": "timing",
                    "segment_id": "hook",
                    "cue_index": 0,
                    "new_start": 1.2,
                    "new_end": 2.4,
                }
            ]
        )
        result = apply_repair_plan(
            self.project,
            repair_plan,
            self.config,
            self.projects_root,
            run_pipeline=self.fake,
            version="v001",
        )
        self.assertTrue(result["applied"])
        self.assertTrue(result["timeline_changed"])
        timeline = json.loads(
            (self.project / "speech_timeline.json").read_text(encoding="utf-8")
        )
        self.assertEqual(timeline["cues"][0]["start"], 1.2)
        self.assertEqual(timeline["cues"][0]["end"], 2.4)

    def test_qa_failure_raises(self) -> None:
        def bad_runner(project_dir, ffmpeg, ffprobe) -> None:
            (project_dir / "output" / "qa_report.json").write_text(
                json.dumps({"ok": False, "errors": ["boom"]}), encoding="utf-8"
            )

        repair_plan = self._plan(
            [
                {
                    "id": "repair-006",
                    "type": "replace_clip",
                    "segment_id": "hook",
                    "after": {
                        "source": "material/b.mp4",
                        "source_start": 0.0,
                        "source_duration": 1.5,
                        "duration": 1.0,
                        "has_audio": False,
                        "loop": False,
                    },
                }
            ]
        )
        with self.assertRaises(Exception) as ctx:
            apply_repair_plan(
                self.project,
                repair_plan,
                self.config,
                self.projects_root,
                run_pipeline=bad_runner,
                version="v001",
            )
        self.assertIn("QA failed", str(ctx.exception))

    def test_deferred_repair_leaves_render_and_qa_to_director(self) -> None:
        repair_plan = self._plan(
            [
                {
                    "id": "repair-deferred",
                    "type": "replace_clip",
                    "segment_id": "hook",
                    "after": {
                        "source": "material/b.mp4",
                        "source_start": 0.0,
                        "source_duration": 1.5,
                        "duration": 1.0,
                        "has_audio": False,
                        "loop": False,
                    },
                }
            ]
        )
        result = apply_repair_plan(
            self.project,
            repair_plan,
            self.config,
            None,
            run_pipeline=self.fake,
            defer_render=True,
        )
        self.assertTrue(result["applied"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["rerun_from"], "RENDER")
        self.assertEqual(self.fake.calls, [])
        rendered_plan = json.loads(
            (self.project / "output" / "edit_plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(rendered_plan["segments"][0]["source"], "material/b.mp4")

    def test_independent_output_verification_runs_before_archive(self) -> None:
        repair_plan = self._plan(
            [
                {
                    "id": "repair-verify",
                    "type": "replace_clip",
                    "segment_id": "hook",
                    "after": {
                        "source": "material/b.mp4",
                        "source_start": 0.0,
                        "source_duration": 1.5,
                        "duration": 1.0,
                        "has_audio": False,
                        "loop": False,
                    },
                }
            ]
        )

        def reject_outputs(project_dir, ffmpeg, ffprobe) -> None:
            raise RuntimeError("independent media verification failed")

        with self.assertRaisesRegex(RuntimeError, "independent media verification failed"):
            apply_repair_plan(
                self.project,
                repair_plan,
                self.config,
                self.projects_root,
                run_pipeline=self.fake,
                verify_outputs=reject_outputs,
                version="v001",
            )
        self.assertFalse(
            (self.projects_root / "demo" / "snapshots" / "v001").exists()
        )

    def test_next_repair_version_numbering(self) -> None:
        self.assertEqual(next_repair_version(self.projects_root, "demo"), "v001")
        snap = self.projects_root / "demo" / "snapshots" / "v007"
        snap.mkdir(parents=True, exist_ok=True)
        self.assertEqual(next_repair_version(self.projects_root, "demo"), "v008")


if __name__ == "__main__":
    unittest.main()
