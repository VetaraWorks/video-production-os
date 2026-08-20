from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import video_os_core.project_manager as pm  # noqa: E402
from video_os_core.locks import ProjectLock, ProjectLockError, lock_status  # noqa: E402
from video_os_core.planner_memory import build_planner_memory  # noqa: E402


DEFAULT_CONFIG = ROOT / "assets" / "default-config.json"


def write_plan_layers(project: Path, config: dict, base_plan: dict) -> None:
    pm.ensure_project_state(project)
    perception_path = project / "perception" / "perception.json"
    perception = (
        json.loads(perception_path.read_text(encoding="utf-8"))
        if perception_path.is_file()
        else {}
    )
    final, context, application, shadow = build_planner_memory(
        project,
        config,
        base_plan,
        perception,
    )
    output = project / "output"
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("edit_plan.base.json", base_plan),
        ("memory_context.json", context),
        ("memory_application.json", application),
        ("edit_plan.json", final),
    ):
        (output / name).write_text(json.dumps(payload), encoding="utf-8")
    if shadow is not None:
        (output / "memory_shadow_report.json").write_text(
            json.dumps(shadow), encoding="utf-8"
        )


def make_project(base: Path, name: str = "demo") -> Path:
    project = base / name
    (project / "script").mkdir(parents=True, exist_ok=True)
    (project / "raw_video").mkdir(parents=True, exist_ok=True)
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "script" / "script.txt").write_text("hello\nworld\n", encoding="utf-8")
    (project / "raw_video" / "clip.mp4").write_bytes(b"x" * 1024)
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    config["jianying_export"]["enabled"] = False
    # Existing scheduler tests opt out so they remain focused on non-Provider
    # stage behavior. Automatic Perception and Review have dedicated tests below.
    config["perception"].update({"enabled": True, "required": False, "auto_run": False})
    config["video_os"]["review"]["enabled"] = False
    (project / "config" / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    return project


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.render_count = 0

    def __call__(self, project_dir, stage, config, ffmpeg=None, ffprobe=None) -> None:
        self.calls.append(stage)
        project = Path(project_dir)
        if stage == "ANALYZE":
            (project / "output").mkdir(parents=True, exist_ok=True)
            (project / "output" / "analysis.json").write_text(
                json.dumps({"schema_version": 1, "ok": True}), encoding="utf-8"
            )
        elif stage == "PLAN":
            (project / "output").mkdir(parents=True, exist_ok=True)
            duration = float(config["duration_seconds"])
            write_plan_layers(
                project,
                config,
                {
                        "schema_version": 2,
                        "canvas": config["canvas"],
                        "duration_seconds": duration,
                        "segments": [
                            {
                                "id": "hook",
                                "timeline_start": 0.0,
                                "timeline_end": duration,
                                "duration": duration,
                                "source": "raw_video/clip.mp4",
                                "source_start": 0.0,
                                "source_duration": duration,
                                "has_audio": True,
                                "loop": False,
                                "selection": {"visual_fingerprint": "current-clip"},
                            }
                        ],
                        "subtitles": {"enabled": False},
                },
            )
        elif stage == "RENDER":
            self.render_count += 1
            (project / "output").mkdir(parents=True, exist_ok=True)
            (project / "output" / "final.mp4").write_bytes(
                f"media-{self.render_count}".encode("ascii")
            )
        elif stage == "QA":
            (project / "output").mkdir(parents=True, exist_ok=True)
            (project / "output" / "qa_report.json").write_text(
                json.dumps({"ok": True}), encoding="utf-8"
            )


class ProjectManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="video-os-test-")
        self.base = Path(self._tmp.name)
        self.project = make_project(self.base)
        self._original_execute = pm.execute_stage
        self._original_validate_final_media = pm._validate_final_media

    def tearDown(self) -> None:
        pm.execute_stage = self._original_execute
        pm._validate_final_media = self._original_validate_final_media
        self._tmp.cleanup()

    def _install_fake(self) -> FakeExecutor:
        fake = FakeExecutor()
        pm.execute_stage = fake
        pm._validate_final_media = lambda *args, **kwargs: []
        return fake

    def _install_hybrid_executor(self) -> FakeExecutor:
        fake = FakeExecutor()
        real_execute = self._original_execute

        def execute(project_dir, stage, config, ffmpeg=None, ffprobe=None):
            if stage == "REPAIR":
                fake.calls.append(stage)
                return real_execute(project_dir, stage, config, ffmpeg, ffprobe)
            return fake(project_dir, stage, config, ffmpeg, ffprobe)

        pm.execute_stage = execute
        pm._validate_final_media = lambda *args, **kwargs: []
        return fake

    def _install_auto_review_executor(self) -> FakeExecutor:
        fake = FakeExecutor()
        real_execute = self._original_execute

        def execute(project_dir, stage, config, ffmpeg=None, ffprobe=None):
            if stage in {"REVIEW", "REPAIR"}:
                fake.calls.append(stage)
                return real_execute(project_dir, stage, config, ffmpeg, ffprobe)
            return fake(project_dir, stage, config, ffmpeg, ffprobe)

        pm.execute_stage = execute
        pm._validate_final_media = lambda *args, **kwargs: []
        return fake

    def _enable_auto_review(self) -> None:
        path = self.project / "config" / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["video_os"]["review"]["enabled"] = True
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _enable_auto_perception(self) -> None:
        path = self.project / "config" / "config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["perception"].update(
            {"enabled": True, "required": True, "auto_run": True}
        )
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _write_review(self, verdict: str, issues: list[dict] | None = None) -> None:
        final_path = self.project / "output" / "final.mp4"
        config = pm.load_config(self.project)
        signature = pm.source_signature(final_path)
        task_id = f"review-{signature['sample_sha256'][:12]}"
        review = {
            "schema_version": 1,
            "status": "done",
            "task_id": task_id,
            "verdict": verdict,
            "overall_score": 80.0,
            "summary": "test review",
            "provider": {"name": "test", "model": "fixture"},
            "target": {
                "path": "output/final.mp4",
                "duration": float(config["duration_seconds"]),
                "signature": signature,
            },
            "categories": [],
            "issues": issues or [],
            "recommendations": [],
        }
        (self.project / "review").mkdir(parents=True, exist_ok=True)
        (self.project / "review" / "review.json").write_text(
            json.dumps(review, ensure_ascii=False), encoding="utf-8"
        )
        result_path = self.project / "review" / "results" / f"{task_id}.json"
        task = {
            "schema_version": 1,
            "task_type": "review",
            "task_id": task_id,
            "status": "done",
            "target": "output/final.mp4",
            "target_signature": signature,
            "result_path": str(result_path),
        }
        task_path = self.project / "review" / "tasks" / "done" / f"{task_id}.json"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        result_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    def _add_repair_candidate(self) -> None:
        config = pm.load_config(self.project)
        duration = float(config["duration_seconds"])
        (self.project / "material").mkdir(parents=True, exist_ok=True)
        (self.project / "material" / "alternate.mp4").write_bytes(b"alternate-media")
        (self.project / "perception").mkdir(parents=True, exist_ok=True)
        (self.project / "perception" / "perception.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "done",
                    "sources": [
                        {
                            "source": "material/alternate.mp4",
                            "duration": duration + 1.0,
                            "segments": [
                                {
                                    "id": "alternate-001",
                                    "start": 0.0,
                                    "end": duration,
                                    "safe_start": 0.0,
                                    "safe_end": duration,
                                    "confidence": 0.95,
                                    "quality": {"usable": True, "score": 0.95},
                                    "semantic_tags": ["hook", "product"],
                                    "visual_fingerprint": "alternate-clip",
                                    "summary": "safe alternate clip",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_claimed_success_artifacts(self, final_bytes: bytes = b"not-a-video") -> dict:
        config = pm.load_config(self.project)
        output = self.project / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "analysis.json").write_text(
            json.dumps({"schema_version": 1, "ok": True}), encoding="utf-8"
        )
        write_plan_layers(
            self.project,
            config,
            {
                    "schema_version": 2,
                    "canvas": config["canvas"],
                    "duration_seconds": config["duration_seconds"],
                    "segments": [{"id": "hook"}],
            },
        )
        (output / "final.mp4").write_bytes(final_bytes)
        (output / "qa_report.json").write_text(
            json.dumps({"ok": True}), encoding="utf-8"
        )
        return config

    @staticmethod
    def _valid_metadata(config: dict) -> dict:
        return {
            "duration": float(config["duration_seconds"]),
            "format": "mov,mp4,m4a,3gp,3g2,mj2",
            "has_video": True,
            "has_audio": True,
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": int(config["canvas"]["width"]),
            "height": int(config["canvas"]["height"]),
            "fps": float(config["canvas"]["fps"]),
        }

    def test_ensure_initialized_creates_state(self) -> None:
        state = pm.ensure_project_state(self.project)
        self.assertTrue(pm.state_path(self.project).is_file())
        self.assertEqual(state["stage"], "INIT")
        for stage in ("INIT", "ANALYZE", "PERCEPTION", "PLAN", "RENDER", "QA", "REVIEW", "REPAIR", "JIANYING_EXPORT", "FINAL"):
            self.assertIn(stage, state["stages"])

    def test_state_readable_after_atomic_write(self) -> None:
        state = pm.ensure_project_state(self.project)
        state["version"] = "v-test"
        pm.save_project_state(self.project, state)
        loaded = pm.load_project_state(self.project)
        self.assertEqual(loaded["version"], "v-test")
        # Simulate an abnormal interruption: any leftover temp file must not break reading.
        (pm.state_path(self.project).with_name("project_state.json.tmp.12345")).write_text(
            "{", encoding="utf-8"
        )
        self.assertEqual(pm.load_project_state(self.project)["version"], "v-test")

    def test_existing_state_is_migrated_with_repair_stage(self) -> None:
        state = pm.ensure_project_state(self.project)
        state["stages"].pop("REPAIR")
        pm.save_project_state(self.project, state)
        migrated = pm.ensure_project_state(self.project)
        self.assertIn("REPAIR", migrated["stages"])
        self.assertEqual(migrated["stages"]["REPAIR"]["status"], "idle")

    def test_lock_exclusive_and_release(self) -> None:
        first = ProjectLock(self.project)
        first.acquire()
        self.assertTrue(lock_status(self.project)["locked"])
        with self.assertRaises(ProjectLockError):
            ProjectLock(self.project).acquire()
        first.release()
        self.assertFalse(lock_status(self.project)["locked"])
        with ProjectLock(self.project):
            self.assertTrue(lock_status(self.project)["locked"])

    def test_stale_lock_takeover(self) -> None:
        lock_path = self.project / "project_state.lock"
        lock_path.write_text(
            json.dumps({"pid": 999999999, "started_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        with ProjectLock(self.project):
            self.assertTrue(lock_status(self.project)["locked"])

    def test_two_processes_cannot_run_simultaneously(self) -> None:
        scripts_dir = str(ROOT / "scripts")
        code = (
            "import sys, time\n"
            f"sys.path.insert(0, {scripts_dir!r})\n"
            "from video_os_core.locks import ProjectLock\n"
            f"lock = ProjectLock({str(self.project)!r})\n"
            "lock.acquire()\n"
            "time.sleep(1.5)\n"
            "lock.release()\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", code])
        try:
            time.sleep(0.6)
            with self.assertRaises(ProjectLockError):
                ProjectLock(self.project).acquire()
        finally:
            proc.wait(timeout=10)

    def test_fingerprint_invalidation_on_script_change(self) -> None:
        state = pm.ensure_project_state(self.project)
        record = state["stages"]["ANALYZE"]
        config = pm.load_config(self.project)
        files = pm.input_files(self.project, "ANALYZE", config)
        record["status"] = "done"
        record["inputs"] = [path.relative_to(self.project).as_posix() for path in files]
        record["input_fingerprint"] = pm.fingerprint_bundle(files, self.project)
        pm.save_project_state(self.project, state)

        (self.project / "script" / "script.txt").write_text("changed\n", encoding="utf-8")
        changed = pm.refresh_state_validity(self.project, state)
        self.assertTrue(changed)
        self.assertEqual(state["stages"]["ANALYZE"]["status"], "invalid")
        self.assertEqual(state["stages"]["PLAN"]["status"], "invalid")

    def test_missing_media_detected(self) -> None:
        state = pm.ensure_project_state(self.project)
        record = state["stages"]["ANALYZE"]
        config = pm.load_config(self.project)
        files = pm.input_files(self.project, "ANALYZE", config)
        record["status"] = "done"
        record["inputs"] = [path.relative_to(self.project).as_posix() for path in files]
        record["input_fingerprint"] = pm.fingerprint_bundle(files, self.project)
        pm.save_project_state(self.project, state)

        (self.project / "raw_video" / "clip.mp4").unlink()
        pm.refresh_state_validity(self.project, state)
        self.assertEqual(state["stages"]["ANALYZE"]["status"], "invalid")
        self.assertIn("raw_video/clip.mp4", state["stages"]["ANALYZE"]["missing_inputs"])
        status = pm.project_status(self.project)
        self.assertTrue(any("clip.mp4" in item for item in status["invalid_or_missing"]))

    def test_run_resume_and_idempotent_skip(self) -> None:
        fake = self._install_fake()
        result = pm.run_project(self.project, to="PLAN")
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, ["ANALYZE", "PLAN"])
        self.assertEqual(
            result["skipped_stages"],
            ["PERCEPTION", "REVIEW", "REPAIR", "JIANYING_EXPORT"],
        )

        # Completed and valid stages must not re-execute.
        pm.run_project(self.project, to="PLAN")
        self.assertEqual(fake.calls, ["ANALYZE", "PLAN"])

        # Simulate a forced kill during RENDER, then resume.
        state = pm.load_project_state(self.project)
        state["stages"]["RENDER"]["status"] = "running"
        pm.save_project_state(self.project, state)
        result = pm.run_project(self.project, to="QA")
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, ["ANALYZE", "PLAN", "RENDER", "QA"])
        self.assertEqual(result["executed_stages"], ["RENDER", "QA"])

        # Everything is done; a plain run reaches FINAL without executing again.
        result = pm.run_project(self.project)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "FINAL")
        self.assertEqual(fake.calls, ["ANALYZE", "PLAN", "RENDER", "QA"])

    def test_director_automatically_executes_perception_before_plan(self) -> None:
        self._enable_auto_perception()
        fake = FakeExecutor()
        real_execute = self._original_execute

        def execute(project_dir, stage, config, ffmpeg=None, ffprobe=None):
            if stage == "PERCEPTION":
                fake.calls.append(stage)
                return real_execute(project_dir, stage, config, ffmpeg, ffprobe)
            return fake(project_dir, stage, config, ffmpeg, ffprobe)

        pm.execute_stage = execute
        pm._validate_final_media = lambda *args, **kwargs: []

        def provider(*_args, **_kwargs):
            (self.project / "perception").mkdir(parents=True, exist_ok=True)
            (self.project / "perception" / "perception.json").write_text(
                json.dumps({"schema_version": 1, "status": "done", "sources": [{}]}),
                encoding="utf-8",
            )
            return {"status": "done"}

        with (
            mock.patch(
                "video_os_core.perception_manager.run_automatic_perception",
                side_effect=provider,
            ) as provider,
            mock.patch(
                "video_os_core.perception_manager.validate_perception_artifact",
                return_value={"ok": True},
            ),
            mock.patch.object(pm, "_validate_plan_perception_binding", return_value=[]),
        ):
            result = pm.run_project(self.project, to="PLAN")

        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, ["ANALYZE", "PERCEPTION", "PLAN"])
        provider.assert_called_once()

    def test_perception_provider_failure_blocks_before_plan(self) -> None:
        from video_os_core.perception_manager import PerceptionNeedsHumanError

        self._enable_auto_perception()
        fake = FakeExecutor()
        real_execute = self._original_execute

        def execute(project_dir, stage, config, ffmpeg=None, ffprobe=None):
            if stage == "PERCEPTION":
                fake.calls.append(stage)
                return real_execute(project_dir, stage, config, ffmpeg, ffprobe)
            return fake(project_dir, stage, config, ffmpeg, ffprobe)

        pm.execute_stage = execute
        pm._validate_final_media = lambda *args, **kwargs: []
        with mock.patch(
            "video_os_core.perception_manager.run_automatic_perception",
            side_effect=PerceptionNeedsHumanError("provider is not configured"),
        ):
            result = pm.run_project(self.project, to="PLAN")

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"]["kind"], "needs_human")
        self.assertEqual(result["blocked"]["stage"], "PERCEPTION")
        self.assertEqual(fake.calls, ["ANALYZE", "PERCEPTION"])
        self.assertFalse((self.project / "output" / "edit_plan.json").exists())

    def test_perception_nonrecoverable_failure_respects_stage_retry_limit(self) -> None:
        from video_os_core.perception_manager import PerceptionFailedError

        self._enable_auto_perception()
        fake = FakeExecutor()
        real_execute = self._original_execute

        def execute(project_dir, stage, config, ffmpeg=None, ffprobe=None):
            if stage == "PERCEPTION":
                fake.calls.append(stage)
                return real_execute(project_dir, stage, config, ffmpeg, ffprobe)
            return fake(project_dir, stage, config, ffmpeg, ffprobe)

        pm.execute_stage = execute
        pm._validate_final_media = lambda *args, **kwargs: []
        with mock.patch(
            "video_os_core.perception_manager.run_automatic_perception",
            side_effect=PerceptionFailedError("invalid provider JSON"),
        ) as provider:
            result = pm.run_project(self.project, to="PLAN")

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"]["kind"], "failed")
        self.assertEqual(result["blocked"]["stage"], "PERCEPTION")
        self.assertEqual(provider.call_count, pm.DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(
            fake.calls,
            ["ANALYZE", "PERCEPTION", "PERCEPTION"],
        )
        self.assertFalse((self.project / "output" / "edit_plan.json").exists())

    def test_force_reexecutes_stages(self) -> None:
        fake = self._install_fake()
        pm.run_project(self.project, to="ANALYZE")
        self.assertEqual(fake.calls, ["ANALYZE"])
        result = pm.run_project(self.project, to="ANALYZE", force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, ["ANALYZE", "ANALYZE"])

    def test_force_full_run_reaches_final_without_executing_final_stage(self) -> None:
        fake = self._install_fake()
        pm.run_project(self.project)
        result = pm.run_project(self.project, force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "FINAL")
        self.assertEqual(
            fake.calls,
            [
                "ANALYZE",
                "PLAN",
                "RENDER",
                "QA",
                "ANALYZE",
                "PLAN",
                "RENDER",
                "QA",
            ],
        )

    def test_unknown_target_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pm.run_project(self.project, to="BOGUS")

    def test_director_uses_validated_stage_transitions(self) -> None:
        self._install_fake()
        real_validate = pm.validate_transition
        with mock.patch.object(
            pm,
            "validate_transition",
            wraps=real_validate,
        ) as validate:
            result = pm.run_project(self.project, to="PLAN")
        self.assertTrue(result["ok"])
        pairs = [(call.args[0], call.args[1]) for call in validate.call_args_list]
        self.assertIn(("INIT", "ANALYZE"), pairs)
        self.assertIn(("ANALYZE", "PERCEPTION"), pairs)
        self.assertIn(("PERCEPTION", "PLAN"), pairs)

    def test_execute_stage_calls_exact_lower_level_stage(self) -> None:
        config = pm.load_config(self.project)
        commands: list[tuple[list[str], str]] = []

        def capture(command, stage):
            commands.append((list(command), stage))

        with mock.patch.object(pm, "_run_command", side_effect=capture):
            for stage in ("ANALYZE", "PLAN", "RENDER", "QA"):
                pm.execute_stage(self.project, stage, config)
        self.assertEqual([stage for _, stage in commands], ["ANALYZE", "PLAN", "RENDER", "QA"])
        for command, stage in commands:
            index = command.index("--stage")
            self.assertEqual(command[index + 1], stage.lower())
            self.assertNotIn("--plan-only", command)

    def test_review_pass_reaches_final_without_repair(self) -> None:
        fake = self._install_fake()
        pm.run_project(self.project, to="QA")
        self._write_review("pass")
        result = pm.run_project(self.project)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "FINAL")
        self.assertNotIn("REPAIR", fake.calls)

    def test_knowledge_sync_failure_does_not_turn_final_video_into_failure(self) -> None:
        self._install_fake()
        pm.run_project(self.project, to="QA")
        self._write_review("pass")
        with mock.patch(
            "video_os_core.production_evidence.sync_verified_evidence",
            return_value={
                "ok": False,
                "status": "unavailable",
                "synced": 0,
                "warning": "Knowledge Root unavailable in test",
            },
        ):
            result = pm.run_project(self.project)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "FINAL")
        self.assertEqual(result["knowledge"]["status"], "unavailable")
        self.assertEqual(result["warnings"], ["Knowledge Root unavailable in test"])

    def test_automatic_review_pass_reaches_final(self) -> None:
        self._enable_auto_review()
        fake = self._install_auto_review_executor()
        provider_calls: list[str] = []

        def provider(project_dir, config, **_kwargs):
            provider_calls.append(pm.source_signature(Path(project_dir) / "output" / "final.mp4")["sample_sha256"])
            self._write_review("pass")
            return {"status": "done"}

        with mock.patch(
            "video_os_core.review_manager.run_automatic_review",
            side_effect=provider,
        ):
            result = pm.run_project(self.project)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "FINAL")
        self.assertEqual(len(provider_calls), 1)
        self.assertIn("REVIEW", result["executed_stages"])
        self.assertNotIn("REPAIR", result["executed_stages"])
        self.assertEqual(fake.calls[-1], "REVIEW")

    def test_automatic_review_fix_repair_rerender_review_pass_reaches_final(self) -> None:
        self._enable_auto_review()
        self._add_repair_candidate()
        fake = self._install_auto_review_executor()
        provider_signatures: list[dict] = []
        memory_application_signatures: list[str] = []

        def provider(project_dir, config, **_kwargs):
            signature = pm.source_signature(Path(project_dir) / "output" / "final.mp4")
            provider_signatures.append(signature)
            current_plan = json.loads(
                (Path(project_dir) / "output" / "edit_plan.json").read_text(
                    encoding="utf-8"
                )
            )
            memory_application_signatures.append(
                current_plan["memory"]["memory_application_signature"]
            )
            if len(provider_signatures) == 1:
                self._write_review(
                    "fix",
                    [
                        {
                            "id": "duplicate-hook",
                            "severity": "high",
                            "category": "duplicate_shot",
                            "start": 0.0,
                            "end": 1.0,
                            "description": "hook repeats an existing shot",
                            "evidence": "same visual fingerprint",
                            "suggestion": "replace with an unused candidate",
                        }
                    ],
                )
            else:
                self._write_review("pass")
            return {"status": "done"}

        with mock.patch(
            "video_os_core.review_manager.run_automatic_review",
            side_effect=provider,
        ):
            result = pm.run_project(self.project)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "FINAL")
        self.assertEqual(len(provider_signatures), 2)
        self.assertNotEqual(provider_signatures[0], provider_signatures[1])
        self.assertEqual(memory_application_signatures[0], memory_application_signatures[1])
        self.assertEqual(
            result["executed_stages"],
            ["ANALYZE", "PLAN", "RENDER", "QA", "REVIEW", "REPAIR", "RENDER", "QA", "REVIEW"],
        )
        self.assertEqual(fake.calls, result["executed_stages"])
        # This scheduler fixture deliberately lacks a valid Perception signature.
        # Production succeeds, but the incomplete evidence chain fails closed.
        self.assertEqual(result["knowledge"]["status"], "gate_rejected")
        self.assertTrue(result["warnings"])
        evidence_files = list((self.project / "repair" / "evidence").glob("*/evidence.json"))
        self.assertEqual(len(evidence_files), 1)
        evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
        self.assertEqual(evidence["evidence_tier"], "observed")
        repaired_plan = json.loads(
            (self.project / "output" / "edit_plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repaired_plan["segments"][0]["source"], "material/alternate.mp4")
        self.assertEqual(len(repaired_plan["memory"]["post_plan_repairs"]), 1)

    def test_automatic_review_provider_failure_needs_human_without_retry(self) -> None:
        from video_os_core.review_manager import ReviewNeedsHumanError

        self._enable_auto_review()
        self._install_auto_review_executor()
        with mock.patch(
            "video_os_core.review_manager.run_automatic_review",
            side_effect=ReviewNeedsHumanError("provider timeout"),
        ) as provider:
            result = pm.run_project(self.project)

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"]["kind"], "needs_human")
        self.assertEqual(result["blocked"]["stage"], "REVIEW")
        self.assertEqual(provider.call_count, 1)
        state = pm.load_project_state(self.project)
        self.assertIsNotNone(state["stages"]["REVIEW"]["input_fingerprint"])

    def test_review_fix_runs_repair_rerender_qa_then_requires_new_review(self) -> None:
        self._add_repair_candidate()
        fake = self._install_hybrid_executor()
        pm.run_project(self.project, to="QA")
        self._write_review(
            "fix",
            [
                {
                    "id": "duplicate-hook",
                    "severity": "high",
                    "category": "duplicate_shot",
                    "start": 0.0,
                    "end": 1.0,
                    "description": "hook repeats an existing shot",
                    "evidence": "same visual fingerprint",
                    "suggestion": "replace with an unused candidate",
                }
            ],
        )
        result = pm.run_project(self.project)
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["blocked"])
        self.assertEqual(result["blocked"]["kind"], "needs_human")
        self.assertEqual(result["blocked"]["stage"], "REVIEW")
        self.assertEqual(
            fake.calls,
            ["ANALYZE", "PLAN", "RENDER", "QA", "REPAIR", "RENDER", "QA"],
        )
        self.assertEqual(result["executed_stages"], ["REPAIR", "RENDER", "QA"])
        repaired_plan = json.loads(
            (self.project / "output" / "edit_plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            repaired_plan["segments"][0]["source"],
            "material/alternate.mp4",
        )
        self.assertTrue((self.project / "repair" / "repair_diff.json").is_file())

    def test_unfixable_review_issue_stops_at_repair_needs_human(self) -> None:
        fake = self._install_hybrid_executor()
        pm.run_project(self.project, to="QA")
        self._write_review(
            "fix",
            [
                {
                    "id": "music-issue",
                    "severity": "high",
                    "category": "music",
                    "start": 0.0,
                    "end": 1.0,
                    "description": "music masks speech",
                    "evidence": "voice is hard to hear",
                    "suggestion": "remix manually",
                }
            ],
        )
        result = pm.run_project(self.project)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"]["kind"], "needs_human")
        self.assertEqual(result["blocked"]["stage"], "REPAIR")
        self.assertEqual(fake.calls[-1], "REPAIR")

    def test_same_review_issue_respects_repair_retry_limit(self) -> None:
        fake = self._install_fake()
        pm.run_project(self.project, to="QA")
        self._write_review(
            "fix",
            [
                {
                    "id": "persistent-issue",
                    "severity": "high",
                    "category": "semantic_alignment",
                    "start": 0.0,
                    "end": 1.0,
                    "description": "persistent mismatch",
                    "evidence": "wrong visual",
                    "suggestion": "replace clip",
                }
            ],
        )
        state = pm.load_project_state(self.project)
        pm.refresh_state_validity(self.project, state)
        repair = state["stages"]["REPAIR"]
        repair["status"] = "idle"
        repair["attempts"] = pm.DEFAULT_MAX_REPAIR_ATTEMPTS
        repair["repair_issue_fingerprint"] = pm._repair_issue_fingerprint(self.project)
        pm.save_project_state(self.project, state)

        result = pm.run_project(self.project)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"]["kind"], "needs_human")
        self.assertEqual(result["blocked"]["stage"], "REPAIR")
        self.assertNotIn("REPAIR", fake.calls)

    def test_forged_qa_cannot_promote_invalid_video_to_final(self) -> None:
        self._write_claimed_success_artifacts(b"not-a-video")
        with (
            mock.patch.object(pm, "resolve_executable", side_effect=lambda explicit, default: explicit or default),
            mock.patch.object(pm, "probe_media", side_effect=RuntimeError("invalid media")),
            mock.patch.object(pm, "_decode_media", return_value=(False, "decode failed")),
        ):
            status = pm.project_status(self.project)
        self.assertEqual(status["stage"], "RENDER")
        self.assertEqual(status["stages"]["RENDER"]["status"], "failed")
        self.assertEqual(status["blocked"]["stage"], "RENDER")
        self.assertIn("invalid media", status["last_error"])

    def test_final_media_contract_rejects_codec_duration_and_resolution_mismatch(self) -> None:
        config = self._write_claimed_success_artifacts(b"non-empty")
        valid = self._valid_metadata(config)
        cases = {
            "codec": ({**valid, "video_codec": "hevc"}, "video codec"),
            "duration": ({**valid, "duration": valid["duration"] + 5.0}, "duration is"),
            "resolution": ({**valid, "width": valid["width"] // 2}, "resolution is"),
        }
        for name, (metadata, expected_error) in cases.items():
            with (
                self.subTest(name=name),
                mock.patch.object(pm, "resolve_executable", side_effect=lambda explicit, default: explicit or default),
                mock.patch.object(pm, "probe_media", return_value=metadata),
                mock.patch.object(pm, "_decode_media", return_value=(True, "")),
            ):
                ok, errors = pm.artifact_valid(self.project, "RENDER", config)
                self.assertFalse(ok)
                self.assertTrue(any(expected_error in error for error in errors), errors)

    def test_full_decode_failure_rejects_probeable_video(self) -> None:
        config = self._write_claimed_success_artifacts(b"non-empty")
        with (
            mock.patch.object(pm, "resolve_executable", side_effect=lambda explicit, default: explicit or default),
            mock.patch.object(pm, "probe_media", return_value=self._valid_metadata(config)),
            mock.patch.object(pm, "_decode_media", return_value=(False, "corrupt frame")),
        ):
            ok, errors = pm.artifact_valid(self.project, "RENDER", config)
        self.assertFalse(ok)
        self.assertTrue(any("full decode failed" in error for error in errors), errors)

    def test_missing_media_tools_requires_human(self) -> None:
        self._write_claimed_success_artifacts(b"non-empty")
        with mock.patch.object(
            pm,
            "resolve_executable",
            side_effect=FileNotFoundError("ffmpeg tools unavailable"),
        ):
            status = pm.project_status(self.project)
        self.assertEqual(status["stage"], "RENDER")
        self.assertEqual(status["stages"]["RENDER"]["status"], "needs_human")
        self.assertTrue(status["needs_human"])

    def test_valid_media_and_qa_pass_independent_validation(self) -> None:
        config = self._write_claimed_success_artifacts(b"non-empty")
        with (
            mock.patch.object(pm, "resolve_executable", side_effect=lambda explicit, default: explicit or default),
            mock.patch.object(pm, "probe_media", return_value=self._valid_metadata(config)),
            mock.patch.object(pm, "_decode_media", return_value=(True, "")),
        ):
            render_ok, render_errors = pm.artifact_valid(self.project, "RENDER", config)
            qa_ok, qa_errors = pm.artifact_valid(self.project, "QA", config)
        self.assertTrue(render_ok, render_errors)
        self.assertTrue(qa_ok, qa_errors)

    def test_qa_ok_true_does_not_bypass_final_media_validation(self) -> None:
        config = self._write_claimed_success_artifacts(b"not-a-video")
        with (
            mock.patch.object(pm, "resolve_executable", side_effect=lambda explicit, default: explicit or default),
            mock.patch.object(pm, "probe_media", side_effect=RuntimeError("ffprobe rejected final.mp4")),
            mock.patch.object(pm, "_decode_media", return_value=(False, "decode rejected final.mp4")),
        ):
            ok, errors = pm.artifact_valid(self.project, "QA", config)
        self.assertFalse(ok)
        self.assertTrue(any("ffprobe rejected" in error for error in errors), errors)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "real media validation requires ffmpeg and ffprobe",
    )
    def test_real_tools_reject_eleven_byte_fake_success(self) -> None:
        self._write_claimed_success_artifacts(b"not-a-video")
        status = pm.project_status(self.project)
        self.assertEqual(status["stage"], "RENDER")
        self.assertEqual(status["stages"]["RENDER"]["status"], "failed")
        self.assertIn("validation failed", status["last_error"])

    @unittest.skipUnless(
        (ROOT.parent / "qa" / "demo-project" / "output" / "final.mp4").is_file()
        and shutil.which("ffmpeg")
        and shutil.which("ffprobe"),
        "real demo media or ffmpeg tools are unavailable",
    )
    def test_real_demo_render_and_qa_pass_independent_validation(self) -> None:
        project = ROOT.parent / "qa" / "demo-project"
        config = pm.load_config(project)
        render_ok, render_errors = pm.artifact_valid(project, "RENDER", config)
        qa_ok, qa_errors = pm.artifact_valid(project, "QA", config)
        self.assertTrue(render_ok, render_errors)
        self.assertTrue(qa_ok, qa_errors)

    def test_status_output_fields(self) -> None:
        status = pm.project_status(self.project)
        for key in (
            "project",
            "version",
            "stage",
            "locked",
            "next_action",
            "needs_human",
            "needs_login",
            "last_error",
            "invalid_or_missing",
            "stages",
        ):
            self.assertIn(key, status)
        self.assertEqual(status["stage"], "ANALYZE")


if __name__ == "__main__":
    unittest.main()
