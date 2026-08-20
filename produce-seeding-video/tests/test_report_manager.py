from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core import report_manager  # noqa: E402


class ReportManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="video-os-report-")
        self.project = Path(self.temporary.name) / "项目 空格"
        (self.project / "output").mkdir(parents=True)
        (self.project / "review").mkdir()
        (self.project / "repair").mkdir()
        (self.project / "raw_video").mkdir()
        (self.project / "script").mkdir()
        (self.project / "script" / "script.txt").write_text("test", encoding="utf-8")
        (self.project / "raw_video" / "private.mp4").write_bytes(b"media")
        (self.project / ".env").write_text("API_KEY=never-package", encoding="utf-8")
        self.home_text = str(Path.home() / "private" / "project")
        state = {
            "schema_version": 1,
            "project": self.project.name,
            "project_dir": self.home_text,
            "stage": "PERCEPTION",
            "blocked": {"kind": "needs_human", "stage": "PERCEPTION", "error": "Bearer secret-token-value"},
            "private_prompt": "user private prompt",
            "prompt_hash": "abc123",
            "stages": {"PERCEPTION": {"last_error": "token=secret-token-value"}},
        }
        self.state_path = self.project / "project_state.json"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        (self.project / "output" / "qa_report.json").write_text(
            json.dumps({"ok": False, "authorization": "Bearer secret-token-value"}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_report_has_fixed_inventory_and_strict_redaction(self) -> None:
        before = self.state_path.read_bytes()
        output = Path(self.temporary.name) / "report.zip"
        fake_runtime = {"schema_version": 1, "ok": False, "components": {}}
        environment = {"VIDEO_OS_TEST_TOKEN": "secret-token-value"}
        with mock.patch.object(report_manager, "discover_runtime", return_value=fake_runtime):
            result = report_manager.create_report(
                self.project, output=output, environ=environment
            )
        self.assertTrue(result["ok"])
        self.assertEqual(self.state_path.read_bytes(), before)
        expected = {
            "summary.json", "system.json", "runtime.json", "provider.json",
            "project_state.json", "qa_report.json", "review.json",
            "repair_summary.json", "memory_application.json", "errors.log",
        }
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            combined = b"\n".join(archive.read(name) for name in archive.namelist()).decode("utf-8")
        self.assertNotIn("secret-token-value", combined)
        self.assertNotIn("user private prompt", combined)
        self.assertNotIn(str(Path.home()), combined)
        self.assertIn("<USER_HOME>", combined)
        self.assertIn("abc123", combined)
        self.assertNotIn("private.mp4", combined)
        self.assertNotIn("never-package", combined)

    def test_report_rejects_media_input_destination(self) -> None:
        with self.assertRaisesRegex(ValueError, "media input"):
            report_manager.create_report(
                self.project,
                output=self.project / "raw_video" / "report.zip",
                environ={},
            )

    def test_cli_report_smoke(self) -> None:
        output = Path(self.temporary.name) / "cli-report.zip"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "video_os.py"),
                "report",
                str(self.project),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["redacted"])
        self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
