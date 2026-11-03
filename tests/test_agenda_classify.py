"""Tests for agenda.classify -- event / reminder / todo routing."""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import agenda  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_no_time_is_todo(self):
        self.assertEqual(agenda.classify("buy milk", ""), "todo")

    def test_recurring_is_always_reminder(self):
        self.assertEqual(agenda.classify("take out the trash",
                                         "every day at 9"), "reminder")
        self.assertEqual(agenda.classify("water the plants weekly", "at 9"),
                         "reminder")

    def test_with_person_is_event(self):
        self.assertEqual(agenda.classify("coffee with Sam", "at 3"), "event")

    def test_remind_me_is_reminder(self):
        self.assertEqual(agenda.classify("remind me to call mom", "at 3pm"),
                         "reminder")

    def test_event_vocabulary(self):
        self.assertEqual(agenda.classify("dentist appointment",
                                         "tomorrow at 3"), "event")
        self.assertEqual(agenda.classify("gym session", "tomorrow morning"),
                         "event")

    def test_aspirational_is_todo(self):
        self.assertEqual(agenda.classify("i need to clean the garage",
                                         "tomorrow"), "todo")

    def test_default_is_reminder(self):
        self.assertEqual(agenda.classify("call mom", "at 3pm"), "reminder")


if __name__ == "__main__":
    unittest.main()
