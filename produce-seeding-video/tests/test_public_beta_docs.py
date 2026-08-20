from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PublicBetaDocumentationTests(unittest.TestCase):
    def test_required_public_beta_documents_exist(self) -> None:
        required = [
            ROOT / "LICENSE",
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "AGENTS.md",
            *[
                ROOT / "docs" / name
                for name in (
                    "INSTALL.md", "QUICKSTART.md", "PERCEPTION.md", "AGENTS.md",
                    "TESTING.md", "TROUBLESHOOTING.md", "ARCHITECTURE.md",
                )
            ],
            *[
                ROOT / ".github" / "ISSUE_TEMPLATE" / name
                for name in ("bug.yml", "quality.yml", "compatibility.yml", "config.yml")
            ],
        ]
        self.assertEqual([str(path) for path in required if not path.is_file()], [])

    def test_public_beta_license_metadata_is_consistent(self) -> None:
        package = json.loads(
            (ROOT / "produce-seeding-video" / "package.json").read_text(encoding="utf-8")
        )
        lock = json.loads(
            (ROOT / "produce-seeding-video" / "package-lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(package["version"], "7.5.0-public-beta")
        self.assertEqual(package["license"], "AGPL-3.0-or-later")
        self.assertEqual(lock["version"], package["version"])
        self.assertEqual(lock["packages"][""]["version"], package["version"])
        self.assertEqual(lock["packages"][""]["license"], package["license"])
        self.assertIn(
            "GNU AFFERO GENERAL PUBLIC LICENSE",
            (ROOT / "LICENSE").read_text(encoding="utf-8"),
        )
        notices = (
            ROOT / "produce-seeding-video" / "THIRD_PARTY_NOTICES.md"
        ).read_text(encoding="utf-8")
        self.assertIn("AGPL-3.0-or-later", notices)
        self.assertNotIn("must still choose", notices)

    def test_readme_local_markdown_links_resolve(self) -> None:
        for document in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
            content = document.read_text(encoding="utf-8")
            links = re.findall(r"\[[^]]+\]\(([^)]+)\)", content)
            local = [link.split("#", 1)[0] for link in links if "://" not in link]
            self.assertTrue(local)
            for link in local:
                self.assertTrue((document.parent / link).resolve().is_file(), link)

    def test_perception_document_answers_required_disclosure_questions(self) -> None:
        content = (ROOT / "docs" / "PERCEPTION.md").read_text(encoding="utf-8")
        for required in (
            "Who watches the video?", "API Provider versus Browser Worker",
            "not mandatory", "Does media leave the machine?", "needs_login",
            "API-key environment-variable name", "fail",
        ):
            self.assertIn(required, content)

    def test_agent_contract_prohibits_fabricated_production_state(self) -> None:
        content = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for term in (
            "video_os.py", "project_state.json", "Production Evidence",
            "Editing Rules", "repair completion", "signatures",
        ):
            self.assertIn(term, content)

    def test_legacy_material_is_out_of_repository_root(self) -> None:
        legacy = list(ROOT.glob("v7-Phase-*.md")) + list(ROOT.glob("V1*.md"))
        self.assertEqual(legacy, [])
        self.assertFalse((ROOT / "docs" / "archive").exists())

    def test_public_issue_links_do_not_point_to_private_source_repository(self) -> None:
        config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("VetaraWorks/video-production-os/security/advisories/new", config)
        self.assertEqual(config.count("https://github.com/"), 1)


if __name__ == "__main__":
    unittest.main()
