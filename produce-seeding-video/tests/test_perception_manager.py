from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core import perception_manager as manager  # noqa: E402


class PerceptionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="perception-manager-")
        self.project = Path(self.temporary.name)
        for state in manager.QUEUE_STATES:
            (self.project / "perception" / "tasks" / state).mkdir(
                parents=True, exist_ok=True
            )
        self.config = {
            "perception": {
                "enabled": True,
                "required": True,
                "auto_run": True,
                "timeout_seconds": 2,
            }
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_task(self, status: str, task_id: str = "current-task") -> dict:
        task = {
            "schema_version": 1,
            "task_id": task_id,
            "status": status,
            "attempts": 0,
            "error": None,
        }
        path = self.project / "perception" / "tasks" / status / f"{task_id}.json"
        path.write_text(json.dumps(task), encoding="utf-8")
        return task

    @staticmethod
    def _manifest(task_id: str = "current-task") -> dict:
        return {"schema_version": 1, "tasks": [{"task_id": task_id}]}

    def test_valid_done_tasks_are_reused_without_provider_call(self) -> None:
        self._write_task("done")
        with (
            mock.patch.object(manager, "prepare", return_value=self._manifest()),
            mock.patch.object(manager, "merge", return_value={"ok": True}),
            mock.patch.object(
                manager,
                "validate_perception_artifact",
                return_value={"ok": True},
            ),
            mock.patch.object(manager, "resolve_worker_config") as resolve_provider,
        ):
            result = manager.run_automatic_perception(self.project, self.config)

        self.assertTrue(result["reused"])
        self.assertEqual(result["task_ids"], ["current-task"])
        resolve_provider.assert_not_called()

    def test_missing_provider_fails_closed_with_queued_task(self) -> None:
        self._write_task("queued")
        with (
            mock.patch.object(manager, "prepare", return_value=self._manifest()),
            mock.patch.object(
                manager,
                "resolve_worker_config",
                side_effect=manager.PerceptionNeedsHumanError("not configured"),
            ),
        ):
            with self.assertRaisesRegex(
                manager.PerceptionNeedsHumanError, "not configured"
            ):
                manager.run_automatic_perception(self.project, self.config)

    def test_explicit_none_provider_fails_before_worker_resolution(self) -> None:
        self._write_task("queued")
        config = {"perception": {**self.config["perception"], "provider": "none"}}
        with (
            mock.patch.object(manager, "prepare", return_value=self._manifest()),
            mock.patch.object(manager, "resolve_worker_config") as worker_config,
        ):
            with self.assertRaisesRegex(
                manager.PerceptionNeedsHumanError, "provider.unconfigured"
            ):
                manager.run_automatic_perception(self.project, config)
        worker_config.assert_not_called()

    def test_failed_durable_task_is_not_reused_as_success(self) -> None:
        task = self._write_task("failed")
        task["error"] = "provider returned invalid JSON"
        failed_path = (
            self.project
            / "perception"
            / "tasks"
            / "failed"
            / "current-task.json"
        )
        failed_path.write_text(json.dumps(task), encoding="utf-8")
        with mock.patch.object(manager, "prepare", return_value=self._manifest()):
            with self.assertRaisesRegex(
                manager.PerceptionFailedError, "invalid JSON"
            ):
                manager.run_automatic_perception(self.project, self.config)

    def test_provider_timeout_moves_exact_task_to_needs_human(self) -> None:
        self._write_task("queued")
        with (
            mock.patch.object(manager, "prepare", return_value=self._manifest()),
            mock.patch.object(manager, "resolve_worker_config", return_value=Path("worker.json")),
            mock.patch.object(manager, "_node_executable", return_value="node"),
            mock.patch.object(
                manager.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["node"], timeout=2),
            ),
        ):
            with self.assertRaisesRegex(
                manager.PerceptionNeedsHumanError, "timed out"
            ):
                manager.run_automatic_perception(self.project, self.config)

        self.assertTrue(
            (
                self.project
                / "perception"
                / "tasks"
                / "needs_human"
                / "current-task.json"
            ).is_file()
        )

    def test_idle_or_mismatched_worker_result_cannot_complete_stage(self) -> None:
        self._write_task("queued")
        completed = subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout='{"ok":true,"status":"idle"}\n',
            stderr="",
        )
        with (
            mock.patch.object(manager, "prepare", return_value=self._manifest()),
            mock.patch.object(manager, "resolve_worker_config", return_value=Path("worker.json")),
            mock.patch.object(manager, "_node_executable", return_value="node"),
            mock.patch.object(manager.subprocess, "run", return_value=completed),
        ):
            with self.assertRaisesRegex(
                manager.PerceptionNeedsHumanError, "mismatched completion"
            ):
                manager.run_automatic_perception(self.project, self.config)

        durable = json.loads(
            (
                self.project
                / "perception"
                / "tasks"
                / "needs_human"
                / "current-task.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(durable["status"], "needs_human")

    def test_qwen_payload_is_admitted_without_gemini_worker(self) -> None:
        script = self.project / "script" / "script.txt"
        script.parent.mkdir(parents=True)
        script.write_text("test script", encoding="utf-8")
        proxy = self.project / "proxy.mp4"
        proxy.write_bytes(b"proxy")
        task = self._write_task("queued")
        task.update(
            {
                "source": "material/source.mp4",
                "source_duration": 3.0,
                "source_signature": {"sample_sha256": "a" * 64},
                "input_signature_digest": "b" * 64,
                "proxy_path": str(proxy),
                "script_path": str(script),
                "prompt_contract": "references/perception-prompt.md",
                "result_path": str(self.project / "perception" / "results" / "current-task.json"),
            }
        )
        queued = self.project / "perception" / "tasks" / "queued" / "current-task.json"
        queued.write_text(json.dumps(task), encoding="utf-8")
        config = {"perception": {**self.config["perception"], "provider": "qwen_api"}}

        class FakeQwen:
            name = "qwen-api"

            def invoke(self, request, **kwargs):
                return {
                    "status": "done",
                    "payload": {
                        "provider": {"name": "qwen-api", "model": "test"},
                        "source": {"segments": []},
                    },
                }

        with (
            mock.patch.object(manager, "prepare", return_value=self._manifest()),
            mock.patch.object(manager.providers, "get_provider", return_value=FakeQwen()),
            mock.patch.object(manager, "merge", return_value={"ok": True}),
            mock.patch.object(manager, "validate_perception_artifact", return_value={"ok": True}),
            mock.patch.object(manager, "resolve_worker_config") as worker_config,
        ):
            result = manager.run_automatic_perception(self.project, config)
        self.assertFalse(result["reused"])
        self.assertTrue((self.project / "perception" / "tasks" / "done" / "current-task.json").is_file())
        worker_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
