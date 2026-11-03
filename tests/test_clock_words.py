"""Tests for features/clock_words.py -- natural spoken clock times."""

import datetime
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import clock_words  # noqa: E402


def _t(h, m):
    return datetime.datetime(2026, 6, 10, h, m)


class TestSpokenTime(unittest.TestCase):
    def test_midnight_and_noon(self):
        self.assertEqual(clock_words.spoken_time(_t(0, 0)), "midnight")
        self.assertEqual(clock_words.spoken_time(_t(12, 0)), "noon")

    def test_o_clock(self):
        self.assertEqual(clock_words.spoken_time(_t(9, 0)), "nine o'clock")
        self.assertEqual(clock_words.spoken_time(_t(21, 0)), "nine o'clock")

    def test_quarter_and_half(self):
        self.assertEqual(clock_words.spoken_time(_t(10, 15)),
                         "quarter past ten")
        self.assertEqual(clock_words.spoken_time(_t(10, 30)),
                         "half past ten")
        self.assertEqual(clock_words.spoken_time(_t(10, 45)),
                         "quarter to eleven")

    def test_quarter_to_rolls_over_midday(self):
        self.assertEqual(clock_words.spoken_time(_t(11, 45)),
                         "quarter to twelve")

    def test_minutes_under_ten(self):
        self.assertEqual(clock_words.spoken_time(_t(13, 5)), "one oh five")

    def test_regular_minutes(self):
        self.assertEqual(clock_words.spoken_time(_t(13, 25)),
                         "one twenty five")

    def test_period_suffix(self):
        self.assertEqual(clock_words.spoken_time(_t(21, 0), period=True),
                         "nine o'clock at night")
        self.assertEqual(clock_words.spoken_time(_t(10, 45), period=True),
                         "quarter to eleven in the morning")
        self.assertEqual(clock_words.spoken_time(_t(14, 30), period=True),
                         "half past two in the afternoon")


class TestSpokenHmString(unittest.TestCase):
    def test_hh_mm(self):
        self.assertEqual(clock_words.spoken_hm_string("15:00"),
                         "three o'clock")
        self.assertEqual(clock_words.spoken_hm_string("08:30"),
                         "half past eight")

    def test_non_clock_passthrough(self):
        self.assertEqual(clock_words.spoken_hm_string("nonsense"), "nonsense")
        self.assertEqual(clock_words.spoken_hm_string(""), "")


if __name__ == "__main__":
    unittest.main()
