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

from video_os_core import system_manager  # noqa: E402


def _runtime(root: Path, *, missing: str | None = None) -> dict:
    components = {}
    for name, filename in (
        ("python", "python.exe"),
        ("node", "node.exe"),
        ("ffmpeg", "ffmpeg.exe"),
        ("ffprobe", "ffprobe.exe"),
        ("browser", "msedge.exe"),
    ):
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime")
        if name == missing:
            components[name] = {
                "status": "unavailable",
                "path": None,
                "source": "explicit",
                "version": None,
                "error": {"code": f"runtime.{name}.unavailable", "message": "missing"},
            }
        else:
            components[name] = {
                "status": "ready",
                "path": str(path.resolve()),
                "source": "test",
                "version": "test",
                "error": None,
            }
    components["browser"]["kind"] = "edge"
    return {
        "schema_version": 1,
        "ok": missing is None,
        "components": components,
    }


class SystemManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="video-os-setup-")
        self.root = Path(self.temporary.name) / "用户 数据"
        self.runtime = _runtime(Path(self.temporary.name) / "runtime files")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_setup_creates_versioned_user_tree_without_secret(self) -> None:
        with mock.patch.object(system_manager, "discover_runtime", return_value=self.runtime):
            result = system_manager.setup_video_os(
                self.root,
                provider="qwen-api",
                api_key_env="MY_QWEN_KEY",
                model="qwen-test",
            )
        self.assertTrue(result["ok"])
        config = json.loads((self.root / "config" / "video-os.json").read_text(encoding="utf-8"))
        self.assertEqual(config["schema_version"], 1)
        self.assertEqual(config["provider"]["api_key_env"], "MY_QWEN_KEY")
        self.assertNotIn("secret-value", json.dumps(config))
        for name in ("config", "projects", "knowledge", "worker", "cache", "logs"):
            self.assertTrue((self.root / name).is_dir(), name)

    def test_setup_preserves_existing_configuration_by_default(self) -> None:
        with mock.patch.object(system_manager, "discover_runtime", return_value=self.runtime):
            first = system_manager.setup_video_os(self.root, provider="none")
            second = system_manager.setup_video_os(self.root, provider="qwen-api")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(second["preserved"])
        self.assertEqual(second["config"]["provider"]["type"], "none")

    def test_apply_config_sets_defaults_without_overriding_user_environment(self) -> None:
        with mock.patch.object(system_manager, "discover_runtime", return_value=self.runtime):
            setup = system_manager.setup_video_os(self.root, provider="none")
        environment = {
            "VIDEO_OS_CONFIG": setup["config_path"],
            "VIDEO_OS_FFMPEG": "user-choice",
        }
        config = system_manager.apply_system_config(environ=environment)
        self.assertIsNotNone(config)
        self.assertEqual(environment["VIDEO_OS_FFMPEG"], "user-choice")
        self.assertEqual(environment["VIDEO_OS_DATA_ROOT"], str(self.root.resolve()))
        self.assertEqual(environment["VIDEO_OS_KNOWLEDGE_ROOT"], str((self.root / "knowledge").resolve()))

    def test_doctor_reports_machine_code_and_does_not_modify_project_state(self) -> None:
        with mock.patch.object(system_manager, "discover_runtime", return_value=self.runtime):
            system_manager.setup_video_os(self.root, provider="none")
        state = self.root / "projects" / "demo" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"stage":"FINAL"}\n', encoding="utf-8")
        before = state.read_bytes()
        missing_runtime = _runtime(Path(self.temporary.name) / "missing-runtime", missing="ffmpeg")
        with mock.patch.object(system_manager, "discover_runtime", return_value=missing_runtime):
            result = system_manager.doctor(data_root=self.root)
        self.assertFalse(result["ok"])
        self.assertIn("RUNTIME_FFMPEG_MISSING", {item["code"] for item in result["checks"]})
        self.assertEqual(state.read_bytes(), before)

    def test_invalid_api_key_environment_name_is_rejected(self) -> None:
        with self.assertRaises(system_manager.SystemConfigError) as caught:
            system_manager.setup_video_os(
                self.root,
                provider="qwen-api",
                api_key_env="not=a-name",
            )
        self.assertEqual(caught.exception.code, "SETUP_API_KEY_ENV_INVALID")

    def test_cli_setup_json_works_in_unicode_path(self) -> None:
        runtime_dir = Path(self.temporary.name) / "cli runtime"
        runtime = _runtime(runtime_dir)
        components = runtime["components"]
        command = [
            sys.executable,
            str(ROOT / "scripts" / "video_os.py"),
            "setup",
            "--data-root",
            str(self.root),
            "--provider",
            "none",
            "--json",
        ]
        for name in ("python", "node", "ffmpeg", "ffprobe"):
            command.extend((f"--{name}", sys.executable))
        command.extend(("--browser", components["browser"]["path"]))
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(Path(payload["config"]["data_root"]), self.root.resolve())


if __name__ == "__main__":
    unittest.main()
