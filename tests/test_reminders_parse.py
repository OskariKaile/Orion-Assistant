"""Tests for the local time parsing in features/reminders.py.

Everything runs against a pinned `now` (Wed 2026-06-10 12:00) so results
are deterministic regardless of when the suite runs.
"""

import datetime as dt
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import reminders  # noqa: E402

NOW = dt.datetime(2026, 6, 10, 12, 0)  # Wednesday, noon


class TestParseClock(unittest.TestCase):
    def test_smart_pm(self):
        # Bare 1-7 means PM (appointments at 3am are rare).
        self.assertEqual(reminders._parse_clock("3"), (15, 0))
        self.assertEqual(reminders._parse_clock("3", smart_pm=False), (3, 0))
        # 8-11 stay literal.
        self.assertEqual(reminders._parse_clock("9"), (9, 0))

    def test_suffixes(self):
        self.assertEqual(reminders._parse_clock("3pm"), (15, 0))
        self.assertEqual(reminders._parse_clock("3:30 pm"), (15, 30))
        self.assertEqual(reminders._parse_clock("12am"), (0, 0))
        self.assertEqual(reminders._parse_clock("15:00"), (15, 0))

    def test_invalid(self):
        self.assertIsNone(reminders._parse_clock("25:00"))
        self.assertIsNone(reminders._parse_clock("soon"))


class TestParseWhen(unittest.TestCase):
    def test_in_minutes(self):
        when, recur = reminders.parse_when("in 30 minutes", now=NOW)
        self.assertEqual(when, NOW + dt.timedelta(minutes=30))
        self.assertIsNone(recur)

    def test_at_clock(self):
        when, recur = reminders.parse_when("at 3pm", now=NOW)
        self.assertEqual(when, NOW.replace(hour=15, minute=0))
        self.assertIsNone(recur)

    def test_past_clock_pushes_to_tomorrow(self):
        when, _ = reminders.parse_when("at 9am", now=NOW)
        self.assertEqual(when, NOW.replace(hour=9) + dt.timedelta(days=1))

    def test_tomorrow_morning(self):
        when, recur = reminders.parse_when("tomorrow morning", now=NOW)
        self.assertEqual(when, dt.datetime(2026, 6, 11, 8, 0))
        self.assertIsNone(recur)

    def test_tonight_at_9_is_pm(self):
        when, _ = reminders.parse_when("tonight at 9", now=NOW)
        self.assertEqual(when, dt.datetime(2026, 6, 10, 21, 0))

    def test_every_day(self):
        when, recur = reminders.parse_when("every day at 9", now=NOW)
        self.assertEqual(when, dt.datetime(2026, 6, 11, 9, 0))
        self.assertEqual(recur, {"kind": "daily"})

    def test_every_weekday(self):
        when, recur = reminders.parse_when("every weekday at 8am", now=NOW)
        self.assertEqual(when, dt.datetime(2026, 6, 11, 8, 0))
        self.assertEqual(recur, {"kind": "weekdays"})

    def test_every_interval(self):
        when, recur = reminders.parse_when("every 30 minutes", now=NOW)
        self.assertEqual(when, NOW + dt.timedelta(minutes=30))
        self.assertEqual(recur, {"kind": "interval", "minutes": 30})

    def test_weekday_at_time(self):
        when, recur = reminders.parse_when("friday at 5pm", now=NOW)
        self.assertEqual(when, dt.datetime(2026, 6, 12, 17, 0))
        self.assertIsNone(recur)

    def test_o_clock_variants_normalised(self):
        when, _ = reminders.parse_when("at 9 o'clock", now=NOW)
        self.assertEqual(when, NOW.replace(hour=9) + dt.timedelta(days=1))

    def test_bare_today_refused(self):
        self.assertEqual(reminders.parse_when("today", now=NOW), (None, None))

    def test_unparseable(self):
        self.assertEqual(reminders.parse_when("when the cows come home",
                                              now=NOW), (None, None))


class TestParseDelta(unittest.TestCase):
    def test_an_hour_later(self):
        self.assertEqual(reminders.parse_delta("an hour later"),
                         dt.timedelta(hours=1))

    def test_minutes_earlier(self):
        self.assertEqual(reminders.parse_delta("30 minutes earlier"),
                         -dt.timedelta(minutes=30))

    def test_half_an_hour(self):
        self.assertEqual(reminders.parse_delta("half an hour later"),
                         dt.timedelta(minutes=30))

    def test_word_numbers(self):
        self.assertEqual(reminders.parse_delta("two hours back"),
                         -dt.timedelta(hours=2))
        self.assertEqual(reminders.parse_delta("one day later"),
                         dt.timedelta(days=1))

    def test_no_delta(self):
        self.assertIsNone(reminders.parse_delta("sometime soon"))
        self.assertIsNone(reminders.parse_delta(""))


if __name__ == "__main__":
    unittest.main()
