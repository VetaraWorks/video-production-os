from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.knowledge import init_knowledge  # noqa: E402


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_feedback(project: str, version: str, feedback_id: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "evidence_tier": "production_verified",
        "feedback_id": feedback_id,
        "project": project,
        "from_version": "v001",
        "to_version": version,
        "collector": "manual",
        "collected_at": "2026-08-05T00:00:00+00:00",
        "source_docs": ["review.json"],
        "snapshot_refs": [f"projects/{project}/snapshots/{version}"],
        "changes": [
            {
                "change_id": "c-1",
                "category": "timing",
                "rule_class": "editing",
                "target": {"kind": "whole_video"},
                "before": {
                    "description": "x",
                    "metric": {"name": "product_first_appearance_s", "value": 22.0},
                },
                "after": {
                    "description": "y",
                    "metric": {"name": "product_first_appearance_s", "value": 8.0},
                },
                "reason": "test",
                "status": "pending",
                "source_docs": ["review.json"],
            }
        ],
    }


class RuleCandidateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rule-candidate-cli-")
        self.base = Path(self._tmp.name)
        self.root = self.base / "knowledge"
        init_knowledge(self.root)
        self.edits = self.root / "edits"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str) -> dict[str, Any]:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "knowledge_tools.py"),
                "--root",
                str(self.root),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_dry_run_zero_candidates_with_reasons(self) -> None:
        # No evidence at all.
        result = self._run("extract-rules", "--dry-run")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["feedback_scanned"], 0)
        self.assertEqual(result["candidate_count"], 0)

    def test_extract_then_list_then_validate(self) -> None:
        write_json(
            self.edits / "fb-a.json",
            make_feedback("demo", "v002", "fb-a"),
        )
        write_json(
            self.edits / "fb-b.json",
            make_feedback("demo2", "v003", "fb-b"),
        )
        dry = self._run("extract-rules", "--dry-run")
        self.assertEqual(dry["feedback_scanned"], 2)
        self.assertEqual(dry["candidate_count"], 1)
        self.assertEqual(len(dry["candidates_preview"]), 1)

        extracted = self._run("extract-rules")
        self.assertEqual(extracted["written"], 1)
        self.assertEqual(extracted["candidate_count"], 1)

        listing = self._run("list-candidates")
        self.assertEqual(listing["candidate_count"], 1)
        self.assertEqual(listing["candidates"][0]["rule_type"], "timing")

        validated = self._run("validate-rules")
        self.assertTrue(validated["ok"])
        self.assertEqual(validated["valid_count"], 1)
        self.assertEqual(validated["manifest"]["counts"]["rule_candidates"], 1)

    def test_repeat_extract_is_idempotent(self) -> None:
        write_json(
            self.edits / "fb-a.json",
            make_feedback("demo", "v002", "fb-a"),
        )
        write_json(
            self.edits / "fb-b.json",
            make_feedback("demo2", "v003", "fb-b"),
        )
        first = self._run("extract-rules")
        second = self._run("extract-rules")
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["unchanged"], 1)
        candidates_dir = self.root / "rule_candidates"
        self.assertEqual(len(list(candidates_dir.glob("*.json"))), 1)
        self.assertEqual(first["candidate_count"], second["candidate_count"])


if __name__ == "__main__":
    unittest.main()
