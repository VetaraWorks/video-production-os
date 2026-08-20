from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from video_os_core.state_machine import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    STAGE_ORDER,
    TransitionError,
    next_stage,
    validate_transition,
)


class StateMachineTests(unittest.TestCase):
    def test_expected_chain(self) -> None:
        for current, target in (
            ("INIT", "ANALYZE"),
            ("ANALYZE", "PERCEPTION"),
            ("PERCEPTION", "PLAN"),
            ("PLAN", "RENDER"),
            ("RENDER", "QA"),
            ("QA", "REVIEW"),
            ("REVIEW", "REPAIR"),
            ("REPAIR", "RENDER"),
            ("REVIEW", "FINAL"),
            ("JIANYING_EXPORT", "FINAL"),
        ):
            self.assertTrue(
                validate_transition(current, target),
                f"expected {current} -> {target}",
            )

    def test_review_branch(self) -> None:
        self.assertIn("REPAIR", ALLOWED_TRANSITIONS["REVIEW"])
        self.assertIn("JIANYING_EXPORT", ALLOWED_TRANSITIONS["REVIEW"])
        self.assertIn("FINAL", ALLOWED_TRANSITIONS["REVIEW"])
        self.assertIn("RENDER", ALLOWED_TRANSITIONS["REPAIR"])

    def test_invalid_transitions_rejected(self) -> None:
        for current, target in (
            ("INIT", "RENDER"),
            ("INIT", "FINAL"),
            ("PLAN", "QA"),
            ("QA", "PLAN"),
            ("RENDER", "ANALYZE"),
            ("FINAL", "INIT"),
            ("REVIEW", "RENDER"),
        ):
            with self.assertRaises(TransitionError, msg=f"{current} -> {target}"):
                validate_transition(current, target)

    def test_unknown_stage_rejected(self) -> None:
        with self.assertRaises(TransitionError):
            validate_transition("INIT", "BOGUS")
        with self.assertRaises(ValueError):
            next_stage("BOGUS")

    def test_next_stage(self) -> None:
        self.assertEqual(next_stage("ANALYZE"), "PERCEPTION")
        self.assertEqual(next_stage("REVIEW"), "REPAIR")
        self.assertEqual(next_stage("REPAIR"), "JIANYING_EXPORT")
        self.assertEqual(next_stage("JIANYING_EXPORT"), "FINAL")
        self.assertIsNone(next_stage("FINAL"))

    def test_stage_order_contains_required(self) -> None:
        for stage in (
            "INIT",
            "ANALYZE",
            "PERCEPTION",
            "PLAN",
            "RENDER",
            "QA",
            "REVIEW",
            "REPAIR",
            "JIANYING_EXPORT",
            "FINAL",
        ):
            self.assertIn(stage, STAGE_ORDER)

    def test_explicit_rewind_is_separate_from_normal_transition(self) -> None:
        with self.assertRaises(TransitionError):
            validate_transition("FINAL", "ANALYZE")
        self.assertTrue(
            validate_transition("FINAL", "ANALYZE", allow_rewind=True)
        )
