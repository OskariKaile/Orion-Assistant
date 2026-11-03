"""Tests for llm/mood.py -- passive stress detection heuristics."""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import mood  # noqa: E402


class TestDetectMood(unittest.TestCase):
    def test_neutral_default(self):
        self.assertEqual(mood.detect_mood("hello there"), "neutral")
        self.assertEqual(mood.detect_mood(""), "neutral")
        self.assertEqual(mood.detect_mood(None), "neutral")

    def test_swearing(self):
        self.assertEqual(mood.detect_mood("this is fucking broken"),
                         "stressed")

    def test_phrase(self):
        self.assertEqual(mood.detect_mood("it still doesnt work"),
                         "stressed")

    def test_caps_plus_exclamations(self):
        self.assertEqual(mood.detect_mood("WHY is this happening!!"),
                         "stressed")

    def test_single_weak_signal_is_not_enough(self):
        # One exclamation pair alone scores 1 -> still neutral.
        self.assertEqual(mood.detect_mood("that was great!!"), "neutral")

    def test_apostrophe_variants(self):
        self.assertEqual(mood.detect_mood("it doesn't work"), "stressed")
        self.assertEqual(mood.detect_mood("it doesn’t work"), "stressed")


if __name__ == "__main__":
    unittest.main()
