"""Tests for cancelling reminders in features/reminders.py.

Every test runs against a temporary store (reminders._PATH is redirected),
so the real reminders.json is never touched. Calendar mirroring is stubbed
out for the same reason -- cancelling here must not reach Google.

The fixture is the pair that broke in practice: two reminders at the same
time whose wording overlaps, where "study German" is a substring of
"study for German".
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import reminders  # noqa: E402

FIXTURE = [
    {"id": "r_1a89", "fires_at": "2026-09-09T22:00:00",
     "text": "study for German", "created": "2026-09-07T21:57:32",
     "recur": {"kind": "daily"}},
    {"id": "r_a286", "fires_at": "2026-09-09T22:00:00",
     "text": "study German", "created": "2026-09-07T21:57:43",
     "recur": {"kind": "daily"}},
    {"id": "r_915e", "fires_at": "2026-09-09T11:00:00",
     "text": "call mom", "created": "2026-09-08T01:54:34", "recur": None},
]


class CancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "reminders.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(FIXTURE, fh)
        self._path, reminders._PATH = reminders._PATH, self.path
        self._mirror = reminders._mirror_delete
        reminders._mirror_delete = lambda items: None

    def tearDown(self):
        reminders._PATH = self._path
        reminders._mirror_delete = self._mirror
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ids(self):
        return [r["id"] for r in reminders.list_pending()]

    # --- the id path -----------------------------------------------------

    def test_id_cancels_exactly_one(self):
        """An id takes out its own reminder and leaves its near-twin."""
        msg = reminders.cancel_matching("", rid="r_a286")
        self.assertIn("study German", msg)
        self.assertEqual(self.ids(), ["r_915e", "r_1a89"])

    def test_two_ids_cancel_both(self):
        """The case that used to drop the second one."""
        reminders.cancel_matching("", rid="r_1a89")
        reminders.cancel_matching("", rid="r_a286")
        self.assertEqual(self.ids(), ["r_915e"])

    def test_id_beats_text_when_both_are_given(self):
        """The unambiguous field wins, so a stale text can't misfire."""
        reminders.cancel_matching("call mom", rid="r_1a89")
        self.assertEqual(sorted(self.ids()), ["r_915e", "r_a286"])

    def test_unknown_id_changes_nothing(self):
        msg = reminders.cancel_matching("", rid="r_nope")
        self.assertIn("already gone", msg)
        self.assertEqual(len(self.ids()), 3)

    def test_id_match_is_case_insensitive(self):
        reminders.cancel_matching("", rid="R_A286")
        self.assertNotIn("r_a286", self.ids())

    # --- the spoken text fallback ---------------------------------------

    def test_text_still_works(self):
        msg = reminders.cancel_matching("call mom")
        self.assertIn("call mom", msg)
        self.assertNotIn("r_915e", self.ids())

    def test_substring_takes_both_overlapping_reminders(self):
        """'German' is in both, and cancelling both is what was asked."""
        msg = reminders.cancel_matching("German")
        self.assertIn("2", msg)
        self.assertEqual(self.ids(), ["r_915e"])

    def test_short_needle_refuses_rather_than_wiping(self):
        """A 1-char match is coincidence, not intent -- ask instead."""
        msg = reminders.cancel_matching("a")
        self.assertIn("Which one", msg)
        self.assertEqual(len(self.ids()), 3)  # nothing deleted

    def test_short_needle_still_works_when_unambiguous(self):
        """The guard only trips on MULTIPLE matches."""
        reminders.cancel_matching("mom")
        self.assertEqual(sorted(self.ids()), ["r_1a89", "r_a286"])

    def test_no_match_says_so(self):
        msg = reminders.cancel_matching("dentist")
        self.assertIn("No reminder", msg)
        self.assertEqual(len(self.ids()), 3)

    def test_empty_asks_which(self):
        msg = reminders.cancel_matching("")
        self.assertIn("Which reminder", msg)
        self.assertEqual(len(self.ids()), 3)


if __name__ == "__main__":
    unittest.main()
