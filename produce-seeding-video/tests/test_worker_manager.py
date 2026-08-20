from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core import worker_manager  # noqa: E402


class WorkerManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="video-os-worker-")
        self.data_root = Path(self.temporary.name) / "用户 数据"
        self.bin_root = Path(self.temporary.name) / "runtime files"
        self.bin_root.mkdir()
        self.components = {}
        for name, filename in (
            ("python", "python.exe"),
            ("node", "node.exe"),
            ("ffmpeg", "ffmpeg.exe"),
            ("ffprobe", "ffprobe.exe"),
            ("browser", "msedge.exe"),
        ):
            path = self.bin_root / filename
            path.write_bytes(b"runtime")
            self.components[name] = {
                "status": "ready",
                "path": str(path.resolve()),
                "source": "test",
                "version": None,
                "error": None,
            }
        self.components["browser"]["kind"] = "edge"
        modules = self.bin_root / "node_modules"
        (modules / "playwright").mkdir(parents=True)
        (modules / "playwright" / "package.json").write_text("{}", encoding="utf-8")
        self.components["playwright"] = {
            "status": "ready",
            "path": str(modules.resolve()),
            "source": "test",
            "version": None,
            "error": None,
        }
        self.runtime = {"schema_version": 1, "ok": True, "components": self.components}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _initialize(self, port: int = 19223) -> dict:
        with (
            mock.patch.object(worker_manager, "discover_worker_runtime", return_value=self.runtime),
            mock.patch.object(worker_manager, "allocate_cdp_port", return_value=port),
        ):
            return worker_manager.initialize_worker(self.data_root)

    def test_port_allocation_skips_occupied_candidate(self) -> None:
        result = worker_manager.allocate_cdp_port(
            19222,
            19224,
            available=lambda port: port == 19223,
        )
        self.assertEqual(result, 19223)
        self.assertNotEqual(result, 9222)

    def test_initialization_uses_edge_isolated_profile_and_persists_port(self) -> None:
        first = self._initialize(19231)
        config = first["config"]
        expected_profile = self.data_root / "worker" / "browser-profile"
        self.assertTrue(first["created"])
        self.assertEqual(config["browserType"], "edge")
        self.assertEqual(Path(config["userDataDir"]), expected_profile.resolve())
        self.assertEqual(config["remoteDebuggingPort"], 19231)
        self.assertEqual(config["ffprobePath"], self.components["ffprobe"]["path"])
        self.assertNotEqual(config["ffprobePath"], config["ffmpegPath"])
        second = worker_manager.initialize_worker(self.data_root)
        self.assertFalse(second["created"])
        self.assertEqual(second["config"]["remoteDebuggingPort"], 19231)

    def test_incomplete_runtime_fails_with_structured_details(self) -> None:
        runtime = {
            "schema_version": 1,
            "ok": False,
            "components": {
                **self.components,
                "playwright": {
                    "status": "unavailable",
                    "path": None,
                    "error": {"code": "runtime.playwright.unavailable", "message": "missing"},
                },
            },
        }
        with mock.patch.object(worker_manager, "discover_worker_runtime", return_value=runtime):
            with self.assertRaises(worker_manager.WorkerError) as caught:
                worker_manager.initialize_worker(self.data_root)
        self.assertEqual(caught.exception.code, "worker.runtime_incomplete")
        self.assertIn("playwright", caught.exception.details)

    def test_playwright_discovery_accepts_skill_local_install(self) -> None:
        skill_root = Path(self.temporary.name) / "portable skill"
        modules = skill_root / "node_modules"
        package = modules / "playwright" / "package.json"
        package.parent.mkdir(parents=True)
        package.write_text("{}", encoding="utf-8")
        with (
            mock.patch.object(worker_manager, "SCRIPT_DIR", skill_root / "scripts"),
            mock.patch.object(worker_manager.shutil, "which", return_value=None),
        ):
            result = worker_manager.discover_playwright(None, environ={})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source"], "skill_local")
        self.assertEqual(Path(result["path"]), modules.resolve())

    def test_status_detects_needs_login_from_dedicated_cdp_page(self) -> None:
        initialized = self._initialize()
        paths = worker_manager.worker_paths(self.data_root)
        paths["browser_session"].write_text(
            json.dumps(
                {
                    "worker_instance_id": initialized["config"]["workerInstanceId"],
                    "profile": initialized["config"]["userDataDir"],
                    "port": initialized["config"]["remoteDebuggingPort"],
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                worker_manager,
                "_cdp_state",
                return_value={"ready": True, "urls": ["https://accounts.google.com/signin"]},
            ),
            mock.patch.object(worker_manager, "_pid_alive", return_value=False),
        ):
            status = worker_manager.worker_status(self.data_root)
        self.assertTrue(status["ok"])
        self.assertEqual(status["status"], "needs_login")
        self.assertEqual(status["login_state"], "needs_login")
        self.assertIn("dedicated Worker browser", status["action"])

    def test_unowned_cdp_endpoint_is_reported_as_port_conflict(self) -> None:
        self._initialize()
        with (
            mock.patch.object(
                worker_manager,
                "_cdp_state",
                return_value={"ready": True, "urls": ["https://gemini.google.com/app"]},
            ),
            mock.patch.object(worker_manager, "_pid_alive", return_value=False),
        ):
            status = worker_manager.worker_status(self.data_root)
        self.assertFalse(status["ok"])
        self.assertEqual(status["status"], "port_conflict")
        self.assertFalse(status["browser"]["session_owned"])

    def test_managed_config_cannot_point_at_another_browser_profile(self) -> None:
        initialized = self._initialize()
        config_path = Path(initialized["config_path"])
        config = initialized["config"]
        config["userDataDir"] = str(self.data_root / "normal-browser-profile")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(worker_manager.WorkerError) as caught:
            worker_manager.load_worker_config(self.data_root)
        self.assertEqual(caught.exception.code, "worker.profile_not_isolated")

    def test_browser_command_always_uses_isolated_profile(self) -> None:
        config = self._initialize()["config"]
        command = worker_manager._browser_command(config)
        self.assertIn(f"--user-data-dir={config['userDataDir']}", command)
        self.assertNotIn("--user-data-dir=Default", command)
        self.assertIn(f"--remote-debugging-port={config['remoteDebuggingPort']}", command)

    def test_stop_refuses_unmanaged_process(self) -> None:
        initialized = self._initialize()
        paths = worker_manager.worker_paths(self.data_root)
        Path(initialized["config"]["lockPath"]).write_text(
            json.dumps({"pid": 321}), encoding="utf-8"
        )
        with (
            mock.patch.object(worker_manager, "_pid_alive", return_value=True),
            mock.patch.object(worker_manager.os, "kill") as kill,
        ):
            with self.assertRaises(worker_manager.WorkerError) as caught:
                worker_manager.worker_stop(self.data_root)
        self.assertEqual(caught.exception.code, "worker.unmanaged_process")
        kill.assert_not_called()
        self.assertFalse(paths["process"].exists())

    def test_stop_terminates_only_verified_worker_pid(self) -> None:
        initialized = self._initialize()
        paths = worker_manager.worker_paths(self.data_root)
        config_path = Path(initialized["config_path"]).resolve()
        lock_path = Path(initialized["config"]["lockPath"])
        lock_path.write_text(json.dumps({"pid": 654}), encoding="utf-8")
        paths["process"].write_text(json.dumps({"pid": 654}), encoding="utf-8")
        command_line = f'node "{worker_manager.WORKER_SCRIPT.resolve()}" run --config "{config_path}"'
        with (
            mock.patch.object(worker_manager, "_pid_alive", side_effect=[True, False, False]),
            mock.patch.object(worker_manager, "_process_command_line", return_value=command_line),
            mock.patch.object(worker_manager.os, "kill") as kill,
            mock.patch.object(
                worker_manager,
                "worker_status",
                return_value={"ok": True, "status": "stopped"},
            ),
        ):
            result = worker_manager.worker_stop(self.data_root)
        kill.assert_called_once_with(654, worker_manager.signal.SIGTERM)
        self.assertTrue(result["stopped"])
        self.assertFalse(lock_path.exists())
        self.assertFalse(paths["process"].exists())

    def test_cli_status_is_machine_readable_when_not_configured(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "video_os.py"),
                "worker",
                "status",
                "--data-root",
                str(self.data_root),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["code"], "worker.not_configured")
        self.assertNotIn("Traceback", completed.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows process check")
    def test_windows_pid_check_observes_current_and_missing_process(self) -> None:
        self.assertTrue(worker_manager._pid_alive(os.getpid()))
        self.assertFalse(worker_manager._pid_alive(2_000_000_000))


if __name__ == "__main__":
    unittest.main()
