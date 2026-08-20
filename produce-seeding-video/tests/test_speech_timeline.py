from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_speech_timeline.py"
SPEC = importlib.util.spec_from_file_location("build_speech_timeline", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PhraseRangeTests(unittest.TestCase):
    def test_long_phrase_does_not_leave_single_character_tail(self) -> None:
        script = "上午洗头下午就变条形码，直接颠覆我对洗发水认知。"
        phrases = MODULE._phrase_ranges(script, 12)
        self.assertTrue(phrases)
        self.assertTrue(all(len(text) >= 3 for _, _, text in phrases))
        self.assertEqual(
            "".join(text.replace("、", "") for _, _, text in phrases),
            "上午洗头下午就变条形码直接颠覆我对洗发水认知",
        )

    def test_short_punctuation_clause_merges_with_previous_phrase(self) -> None:
        phrases = MODULE._phrase_ranges("现在家里的床上、地上、梳子上都干净了不少。", 14)
        texts = [text for _, _, text in phrases]
        self.assertIn("现在家里的床上、地上", texts)
        self.assertNotIn("地上", texts)


if __name__ == "__main__":
    unittest.main()
