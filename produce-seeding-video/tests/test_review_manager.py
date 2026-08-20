from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import prepare_perception as perception  # noqa: E402
import video_os_core.review_manager as review_manager  # noqa: E402


class PrepareReviewTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="review-task-test-")
        self.project = Path(self._tmp.name) / "project"
        (self.project / "script").mkdir(parents=True)
        (self.project / "output").mkdir()
        (self.project / "script" / "script.txt").write_text("test", encoding="utf-8")
        (self.project / "output" / "edit_plan.json").write_text("{}", encoding="utf-8")
        (self.project / "output" / "final.mp4").write_bytes(b"video")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self) -> Namespace:
        return Namespace(
            project_dir=self.project,
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            work_root=None,
            force=False,
        )

    def _target(self, mtime_ns: int) -> dict:
        return {
            "path": "output/final.mp4",
            "absolute_path": str((self.project / "output" / "final.mp4").resolve()),
            "duration": 3.0,
            "signature": {
                "size_bytes": 5,
                "mtime_ns": mtime_ns,
                "sample_sha256": "a" * 64,
            },
        }

    def test_prepare_review_reuses_same_signature_task(self) -> None:
        with (
            mock.patch.object(perception, "resolve_executable", side_effect=lambda value, _name: value),
            mock.patch.object(perception, "_review_target", return_value=self._target(100)),
            mock.patch.object(perception, "_make_review_proxy", return_value=False),
        ):
            first = perception.prepare_review(self._args())
            second = perception.prepare_review(self._args())

        self.assertEqual(first["task"]["task_id"], second["task"]["task_id"])
        self.assertEqual(len(list((self.project / "review" / "tasks" / "queued").glob("*.json"))), 1)

    def test_task_id_changes_when_full_signature_changes(self) -> None:
        with (
            mock.patch.object(perception, "resolve_executable", side_effect=lambda value, _name: value),
            mock.patch.object(
                perception,
                "_review_target",
                side_effect=[self._target(100), self._target(200)],
            ),
            mock.patch.object(perception, "_make_review_proxy", return_value=False),
        ):
            first = perception.prepare_review(self._args())
            second = perception.prepare_review(self._args())

        self.assertNotEqual(first["task"]["task_id"], second["task"]["task_id"])
        self.assertEqual(len(list((self.project / "review" / "tasks" / "queued").glob("*.json"))), 2)


class AutomaticReviewManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="review-manager-test-")
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        self.signature = {
            "size_bytes": 10,
            "mtime_ns": 20,
            "sample_sha256": "b" * 64,
        }
        self.task = {
            "task_id": "project-review-current",
            "task_type": "review",
            "status": "queued",
            "target_signature": self.signature,
        }
        self.config = {
            "video_os": {
                "review": {
                    "enabled": True,
                    "timeout_seconds": 30,
                }
            }
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manifest(self, status: str = "queued") -> dict:
        return {"task": {**self.task, "status": status}}

    def test_worker_is_invoked_for_exact_review_task(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "done",
                    "kind": "review",
                    "taskId": self.task["task_id"],
                }
            ),
            stderr="",
        )
        durable = {**self.task, "status": "done"}
        with (
            mock.patch.object(review_manager, "prepare_review", return_value=self._manifest()),
            mock.patch.object(review_manager, "resolve_worker_config", return_value=Path("worker.json")),
            mock.patch.object(review_manager, "_node_executable", return_value="node"),
            mock.patch.object(review_manager.subprocess, "run", return_value=completed) as run,
            mock.patch.object(review_manager, "_task_path", return_value=(Path("task.json"), durable)),
            mock.patch.object(review_manager, "_verify_provider_result", return_value={"verdict": "pass"}),
        ):
            result = review_manager.run_automatic_review(self.project, self.config)

        self.assertEqual(result["status"], "done")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--kind") + 1], "review")
        self.assertEqual(command[command.index("--project") + 1], str(self.project.resolve()))
        self.assertIn("--fail-closed", command)
        self.assertIn("--prepare-script", command)

    def test_provider_timeout_fails_closed_and_marks_task(self) -> None:
        with (
            mock.patch.object(review_manager, "prepare_review", return_value=self._manifest()),
            mock.patch.object(review_manager, "resolve_worker_config", return_value=Path("worker.json")),
            mock.patch.object(review_manager, "_node_executable", return_value="node"),
            mock.patch.object(
                review_manager.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("worker", 30),
            ),
            mock.patch.object(review_manager, "_mark_needs_human") as mark,
        ):
            with self.assertRaisesRegex(review_manager.ReviewNeedsHumanError, "timed out"):
                review_manager.run_automatic_review(self.project, self.config)

        mark.assert_called_once()

    def test_idle_or_mismatched_worker_result_is_not_success(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "status": "idle"}),
            stderr="",
        )
        with (
            mock.patch.object(review_manager, "prepare_review", return_value=self._manifest()),
            mock.patch.object(review_manager, "resolve_worker_config", return_value=Path("worker.json")),
            mock.patch.object(review_manager, "_node_executable", return_value="node"),
            mock.patch.object(review_manager.subprocess, "run", return_value=completed),
            mock.patch.object(review_manager, "_mark_needs_human") as mark,
        ):
            with self.assertRaisesRegex(review_manager.ReviewNeedsHumanError, "idle"):
                review_manager.run_automatic_review(self.project, self.config)

        mark.assert_called_once()

    def test_terminal_provider_task_is_not_silently_requeued(self) -> None:
        with (
            mock.patch.object(
                review_manager,
                "prepare_review",
                return_value=self._manifest("needs_human"),
            ),
            mock.patch.object(review_manager, "_mark_needs_human") as mark,
            mock.patch.object(review_manager.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(review_manager.ReviewNeedsHumanError, "non-runnable"):
                review_manager.run_automatic_review(self.project, self.config)

        mark.assert_called_once()
        run.assert_not_called()

    def test_missing_worker_configuration_requires_human(self) -> None:
        missing = self.project / "missing-worker.json"
        config = {
            "video_os": {
                "review": {
                    "enabled": True,
                    "worker_config": str(missing),
                }
            }
        }
        with mock.patch.object(review_manager, "DEFAULT_WORKER_CONFIG", self.project / "absent.json"):
            with self.assertRaisesRegex(review_manager.ReviewNeedsHumanError, "not configured"):
                review_manager.resolve_worker_config(self.project, config)

    def test_explicit_none_provider_fails_before_worker_resolution(self) -> None:
        config = {"video_os": {"review": {"enabled": True, "provider": "none"}}}
        with (
            mock.patch.object(review_manager, "prepare_review", return_value=self._manifest()),
            mock.patch.object(review_manager, "resolve_worker_config") as worker_config,
            mock.patch.object(review_manager, "_mark_needs_human") as mark,
        ):
            with self.assertRaisesRegex(
                review_manager.ReviewNeedsHumanError, "provider.unconfigured"
            ):
                review_manager.run_automatic_review(self.project, config)
        worker_config.assert_not_called()
        mark.assert_called_once()


if __name__ == "__main__":
    unittest.main()
