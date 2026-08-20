from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import init_knowledge  # noqa: E402
from video_os_core.knowledge_root import (  # noqa: E402
    KNOWLEDGE_ROOT_ENV,
    inspect_knowledge_root,
)


class KnowledgeRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="knowledge-root-test-")
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_four_root_states_are_distinct(self) -> None:
        self.assertEqual(
            inspect_knowledge_root(None, environ={})["state"], "unconfigured"
        )
        missing = self.base / "missing"
        self.assertEqual(
            inspect_knowledge_root(missing, environ={})["state"], "path_missing"
        )
        root = self.base / "knowledge"
        init_knowledge(root)
        empty = inspect_knowledge_root(root, environ={})
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["state"], "initialized_empty")
        (root / "edits" / "evidence.json").write_text("{}", encoding="utf-8")
        ready = inspect_knowledge_root(root, environ={})
        self.assertTrue(ready["ok"])
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["data_files"], 1)

    def test_installed_cli_four_states_and_no_script_relative_fallback(self) -> None:
        installed = self.base / "installed-skill"
        shutil.copytree(ROOT, installed)
        cli = installed / "scripts" / "knowledge_tools.py"
        unrelated_cwd = self.base / "outside"
        unrelated_cwd.mkdir()

        def run(root_value: Path | None) -> subprocess.CompletedProcess[str]:
            environment = os.environ.copy()
            environment.pop(KNOWLEDGE_ROOT_ENV, None)
            if root_value is not None:
                environment[KNOWLEDGE_ROOT_ENV] = str(root_value)
            return subprocess.run(
                [sys.executable, str(cli), "status"],
                cwd=unrelated_cwd,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        unconfigured = run(None)
        self.assertEqual(unconfigured.returncode, 1)
        self.assertEqual(json.loads(unconfigured.stdout)["state"], "unconfigured")

        missing_root = self.base / "missing-installed"
        missing = run(missing_root)
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(json.loads(missing.stdout)["state"], "path_missing")

        knowledge_root = self.base / "installed-knowledge"
        init_knowledge(knowledge_root)
        empty = run(knowledge_root)
        self.assertEqual(empty.returncode, 0)
        self.assertEqual(json.loads(empty.stdout)["state"], "initialized_empty")

        (knowledge_root / "edits" / "verified.json").write_text(
            "{}", encoding="utf-8"
        )
        ready = run(knowledge_root)
        self.assertEqual(ready.returncode, 0)
        self.assertEqual(json.loads(ready.stdout)["state"], "ready")

    def test_memory_cli_fails_closed_when_root_is_unconfigured(self) -> None:
        project = self.base / "project"
        (project / "script").mkdir(parents=True)
        (project / "script" / "script.txt").write_text("test", encoding="utf-8")
        environment = os.environ.copy()
        environment.pop(KNOWLEDGE_ROOT_ENV, None)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "video_os.py"),
                "memory-suggest",
                str(project),
                "--dry-run",
            ],
            cwd=self.base,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        error = json.loads(completed.stderr)
        self.assertFalse(error["ok"])
        self.assertEqual(error["knowledge_root"]["state"], "unconfigured")


if __name__ == "__main__":
    unittest.main()
