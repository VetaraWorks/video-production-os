from __future__ import annotations

import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import prepare_perception as preparation  # noqa: E402


class PerceptionPreparationTests(unittest.TestCase):
    def test_cross_source_provider_ids_are_deterministically_namespaced(self) -> None:
        sources = [
            {"source": "material/a.mp4", "segments": [{"id": "segment-0"}]},
            {"source": "material/b.mp4", "segments": [{"id": "segment-0"}]},
        ]
        preparation._namespace_cross_source_segment_ids(sources)
        self.assertEqual(sources[0]["segments"][0]["id"], "segment-0")
        qualified = sources[1]["segments"][0]
        self.assertEqual(qualified["provider_segment_id"], "segment-0")
        self.assertTrue(qualified["id"].startswith("segment-0--"))

        repeated = [
            {"source": "material/a.mp4", "segments": [{"id": "segment-0"}]},
            {"source": "material/b.mp4", "segments": [{"id": "segment-0"}]},
        ]
        preparation._namespace_cross_source_segment_ids(repeated)
        self.assertEqual(repeated, sources)

    def test_script_change_creates_new_signature_bound_tasks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="perception-prepare-") as temporary:
            project = Path(temporary)
            (project / "script").mkdir()
            (project / "material").mkdir()
            (project / "config").mkdir()
            (project / "script" / "script.txt").write_text(
                "first task", encoding="utf-8"
            )
            video = project / "material" / "clip.mp4"
            video.write_bytes(b"signature-bound-video" * 2048)
            config = json.loads(
                (ROOT / "assets" / "default-config.json").read_text(encoding="utf-8")
            )
            (project / "config" / "config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            args = Namespace(
                project_dir=project,
                ffmpeg="ffmpeg",
                ffprobe="ffprobe",
                work_root=None,
                force=False,
            )
            metadata = {
                "has_video": True,
                "has_audio": False,
                "duration": 8.0,
            }
            with (
                mock.patch.object(
                    preparation,
                    "resolve_executable",
                    side_effect=lambda value, _name: value,
                ),
                mock.patch.object(preparation, "probe_media", return_value=metadata),
                mock.patch.object(preparation, "_make_proxy", return_value=False),
            ):
                first = preparation.prepare(args)
                (project / "script" / "script.txt").write_text(
                    "second task", encoding="utf-8"
                )
                second = preparation.prepare(args)

            self.assertNotEqual(
                first["input_signature"]["digest_sha256"],
                second["input_signature"]["digest_sha256"],
            )
            self.assertNotEqual(
                first["tasks"][0]["task_id"], second["tasks"][0]["task_id"]
            )
            self.assertTrue(
                Path(second["tasks"][0]["task_path"]).is_file()
            )


if __name__ == "__main__":
    unittest.main()
