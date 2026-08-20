from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_install_consistency as consistency  # noqa: E402


class SkillEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.input_contract = (ROOT / "references" / "input-contract.md").read_text(
            encoding="utf-8"
        )
        self.video_os = (ROOT / "scripts" / "video_os.py").read_text(encoding="utf-8")
        self.project_manager = (
            ROOT / "scripts" / "video_os_core" / "project_manager.py"
        ).read_text(encoding="utf-8")
        self.pipeline = (
            ROOT / "scripts" / "video_pipeline" / "pipeline.py"
        ).read_text(encoding="utf-8")
        self.default_config = json.loads(
            (ROOT / "assets" / "default-config.json").read_text(encoding="utf-8")
        )

    def test_default_skill_commands_use_video_os(self) -> None:
        self.assertIn(
            "python scripts/video_os.py run <project-dir> --to PLAN",
            self.skill,
        )
        self.assertIn(
            "python scripts/video_os.py run <project-dir> --to FINAL",
            self.skill,
        )
        self.assertNotRegex(
            self.skill,
            re.compile(r"python\s+scripts/run_pipeline\.py"),
        )
        self.assertNotIn("python scripts/run_batch.py", self.skill)
        self.assertIn(
            "python scripts/video_os.py run <child-project-dir> --to FINAL",
            self.input_contract,
        )
        self.assertIn(
            "run_batch.py <batch-root>` remains available only for legacy callers",
            self.input_contract,
        )

    def test_default_chain_reaches_pipeline_through_project_manager(self) -> None:
        self.assertIn("from video_os_core import project_manager", self.video_os)
        self.assertIn("project_manager.run_project(", self.video_os)
        self.assertIn('str(SCRIPT_DIR / "run_pipeline.py")', self.project_manager)
        self.assertTrue((ROOT / "scripts" / "run_pipeline.py").is_file())

    def test_default_chain_executes_and_consumes_required_perception(self) -> None:
        perception = self.default_config["perception"]
        self.assertTrue(perception["enabled"])
        self.assertTrue(perception["required"])
        self.assertTrue(perception["auto_run"])
        self.assertIn("run_automatic_perception(", self.project_manager)
        self.assertIn("attach_project_perception(", self.pipeline)
        self.assertIn("selected_segment_ids", self.pipeline)

    def test_release_inventory_includes_provider_managers(self) -> None:
        files = set(consistency.release_files(ROOT))
        self.assertIn(Path("scripts/video_os_core/perception_manager.py"), files)
        self.assertIn(Path("scripts/video_os_core/review_manager.py"), files)
        self.assertIn(Path("scripts/video_os_core/runtime.py"), files)
        self.assertIn(Path("scripts/video_os_core/worker_manager.py"), files)
        self.assertIn(Path("scripts/check_install_consistency.py"), files)
        self.assertIn(Path("scripts/video_os_core/knowledge_root.py"), files)
        self.assertIn(Path("references/knowledge-root.md"), files)
        self.assertIn(Path("package.json"), files)
        self.assertIn(Path("package-lock.json"), files)
        self.assertIn(Path("THIRD_PARTY_NOTICES.md"), files)
        self.assertFalse(
            any(path.parts and path.parts[0] == "knowledge" for path in files)
        )
        self.assertFalse(any(path.parts and path.parts[0] == ".tmp" for path in files))
        self.assertFalse(any("node_modules" in path.parts for path in files))

    def test_install_consistency_rejects_drift_and_missing_review_manager(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill-consistency-test-") as temporary:
            base = Path(temporary)
            source = base / "source"
            installed = base / "installed"
            for root in (source, installed):
                (root / "assets").mkdir(parents=True)
                (root / "scripts" / "video_os_core").mkdir(parents=True)
                (root / "SKILL.md").write_text(
                    "python scripts/video_os.py run <project-dir> --to PLAN\n"
                    "python scripts/video_os.py run <project-dir> --to FINAL\n",
                    encoding="utf-8",
                )
                for relative in consistency.REQUIRED_RELEASE_FILES - {Path("SKILL.md")}:
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(relative.as_posix(), encoding="utf-8")
                (root / "scripts" / "video_os_core" / "knowledge_root.py").write_text(
                    "VIDEO_OS_KNOWLEDGE_ROOT unconfigured path_missing "
                    "initialized_empty ready\n",
                    encoding="utf-8",
                )

            self.assertTrue(consistency.check_consistency(source, installed)["ok"])

            (installed / "SKILL.md").write_text(
                "python scripts/run_pipeline.py <project-dir>\n", encoding="utf-8"
            )
            (installed / "scripts" / "video_os_core" / "review_manager.py").unlink()
            result = consistency.check_consistency(source, installed)
            self.assertFalse(result["ok"])
            self.assertIn("SKILL.md", result["different"])
            self.assertIn(
                "scripts/video_os_core/review_manager.py",
                result["missing"],
            )

    def test_install_consistency_rejects_script_relative_knowledge_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="knowledge-contract-test-") as temporary:
            source = Path(temporary) / "source"
            installed = Path(temporary) / "installed"
            for root in (source, installed):
                for relative in consistency.REQUIRED_RELEASE_FILES:
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if relative == Path("SKILL.md"):
                        content = (
                            "python scripts/video_os.py run <project-dir> --to PLAN\n"
                            "python scripts/video_os.py run <project-dir> --to FINAL\n"
                        )
                    elif relative == Path("scripts/video_os_core/knowledge_root.py"):
                        content = (
                            "VIDEO_OS_KNOWLEDGE_ROOT unconfigured path_missing "
                            "initialized_empty ready\n"
                        )
                    else:
                        content = relative.as_posix()
                    path.write_text(content, encoding="utf-8")
            self.assertTrue(consistency.check_consistency(source, installed)["ok"])
            bad = "DEFAULT_KNOWLEDGE_ROOT = Path(__file__).resolve().parents[2] / 'knowledge'\n"
            for root in (source, installed):
                (root / "scripts" / "video_os.py").write_text(bad, encoding="utf-8")
            result = consistency.check_consistency(source, installed)
            self.assertFalse(result["ok"])
            self.assertTrue(
                any("script-relative Knowledge Root" in error for error in result["errors"])
            )

    def test_real_release_inventory_round_trips_without_runtime_knowledge(self) -> None:
        with tempfile.TemporaryDirectory(prefix="real-release-test-") as temporary:
            installed = Path(temporary) / "installed"
            for relative in consistency.release_files(ROOT):
                source = ROOT / relative
                target = installed / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            result = consistency.check_consistency(ROOT, installed)
            self.assertTrue(result["ok"], result)
            self.assertFalse((installed / "knowledge").exists())


if __name__ == "__main__":
    unittest.main()
