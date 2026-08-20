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

from video_os_core import runtime  # noqa: E402
from video_os_core import perception_manager, review_manager  # noqa: E402


class RuntimeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="video-os-runtime-")
        self.root = Path(self.temporary.name) / "包含 空格"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _executable(self, name: str) -> Path:
        path = self.root / name
        path.write_bytes(b"test executable")
        return path

    def test_explicit_path_with_spaces_and_unicode_is_authoritative(self) -> None:
        node = self._executable("node.exe")
        result = runtime.discover_node(
            str(node), environ={}, which=lambda _name: "fallback", probe_version=False
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(Path(result["path"]), node.resolve())
        self.assertEqual(result["source"], "explicit")

    def test_invalid_explicit_path_does_not_silently_fallback(self) -> None:
        fallback = self._executable("fallback-node.exe")
        result = runtime.discover_node(
            str(self.root / "missing-node.exe"),
            environ={},
            which=lambda _name: str(fallback),
            probe_version=False,
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["source"], "explicit")
        self.assertEqual(result["error"]["code"], "runtime.node.unavailable")

    def test_ffprobe_is_discovered_as_ffmpeg_sibling_not_ffmpeg_itself(self) -> None:
        ffmpeg = self._executable("ffmpeg.exe")
        ffprobe = self._executable("ffprobe.exe")
        result = runtime.discover_ffprobe(
            ffmpeg_path=str(ffmpeg), environ={}, which=lambda _name: None, probe_version=False
        )
        self.assertEqual(Path(result["path"]), ffprobe.resolve())
        self.assertEqual(result["source"], "ffmpeg_sibling")

    def test_edge_is_accepted_when_chrome_is_absent(self) -> None:
        edge = self._executable("msedge.exe")
        result = runtime.discover_browser(
            environ={},
            which=lambda _name: None,
            common_candidates=[("chrome", self.root / "missing-chrome.exe"), ("edge", edge)],
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["kind"], "edge")
        self.assertEqual(Path(result["path"]), edge.resolve())

    def test_runtime_snapshot_is_structured_and_complete(self) -> None:
        python = self._executable("python.exe")
        node = self._executable("node.exe")
        ffmpeg = self._executable("ffmpeg.exe")
        ffprobe = self._executable("ffprobe.exe")
        browser = self._executable("msedge.exe")
        result = runtime.discover_runtime(
            {
                "python": str(python),
                "node": str(node),
                "ffmpeg": str(ffmpeg),
                "ffprobe": str(ffprobe),
                "browser": str(browser),
            },
            environ={},
            which=lambda _name: None,
            probe_versions=False,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(set(result["components"]), {"python", "node", "ffmpeg", "ffprobe", "browser"})

    def test_runtime_cli_returns_json_even_when_components_are_missing(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "video_os_core" / "runtime.py"), "--no-version-probe"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("python", payload["components"])


class RuntimeManagerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="video-os-runtime-manager-")
        self.project = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_shared_worker_config_environment_is_supported(self) -> None:
        worker = self.project / "worker.json"
        worker.write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"VIDEO_OS_WORKER_CONFIG": str(worker)}, clear=True):
            self.assertEqual(perception_manager.resolve_worker_config(self.project, {}), worker.resolve())
            self.assertEqual(review_manager.resolve_worker_config(self.project, {}), worker.resolve())

    def test_missing_node_has_structured_fail_closed_error(self) -> None:
        unavailable = {
            "status": "unavailable",
            "path": None,
            "error": {"code": "runtime.node.unavailable", "message": "not found"},
        }
        with mock.patch.object(review_manager, "discover_node", return_value=unavailable):
            with self.assertRaisesRegex(review_manager.ReviewNeedsHumanError, "runtime.node.unavailable"):
                review_manager._node_executable(self.project, {})
        with mock.patch.object(perception_manager, "discover_node", return_value=unavailable):
            with self.assertRaisesRegex(perception_manager.PerceptionNeedsHumanError, "runtime.node.unavailable"):
                perception_manager._node_executable(self.project, {})


class RuntimePortabilityTests(unittest.TestCase):
    def test_release_runtime_files_have_no_developer_machine_defaults(self) -> None:
        targets = [
            ROOT / "scripts" / "configure_gemini_worker.ps1",
            ROOT / "scripts" / "launch_gemini_pwa.ps1",
            ROOT / "scripts" / "gemini_worker.mjs",
            ROOT / "scripts" / "export_jianying.py",
            ROOT / "scripts" / "video_pipeline" / "jianying.py",
            ROOT / "scripts" / "video_os_core" / "perception_manager.py",
            ROOT / "scripts" / "video_os_core" / "review_manager.py",
            ROOT / "references" / "gemini-worker.md",
            ROOT / "SKILL.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8-sig") for path in targets)
        for forbidden in (
            "D:\\AI_Video_Worker",
            "D:\\Apps\\JianyingPro",
            "D:\\CodexVideo",
            ".cache\\codex-runtimes",
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
