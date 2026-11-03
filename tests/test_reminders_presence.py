"""Tests for holding reminders back while nobody is at the computer.

Exercises reminders._deliver_or_hold directly with a fake clock and a fake
presence answer, so nothing here touches the real store, the microphone or
the speaker.

The behaviours that matter: a reminder is never announced to an empty room,
it is never lost either, and coming back gets you one summary rather than a
burst of separate announcements.
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import config  # noqa: E402
import reminders  # noqa: E402


def _item(text):
    return {"id": text, "text": text, "fires_at": "2026-01-01T00:00:00"}


class HoldWhileAway(unittest.TestCase):
    def setUp(self):
        self.spoken = []
        reminders._HELD.clear()
        reminders._PRESENT_SINCE = None
        self._real_speak = reminders._SPEAK
        self._real_fire = reminders._fire
        reminders._SPEAK = self.spoken.append
        reminders._fire = lambda item: self.spoken.append(item["text"])
        self.settle = float(getattr(config, "REMINDER_RETURN_DELAY", 4))

    def tearDown(self):
        reminders._SPEAK = self._real_speak
        reminders._fire = self._real_fire
        reminders._HELD.clear()
        reminders._PRESENT_SINCE = None

    def test_fires_immediately_when_present(self):
        reminders._deliver_or_hold([_item("take a break")], True, 1000.0)
        self.assertEqual(self.spoken, ["take a break"])
        self.assertEqual(reminders.held_count(), 0)

    def test_held_while_away(self):
        reminders._deliver_or_hold([_item("take a break")], False, 1000.0)
        self.assertEqual(self.spoken, [])
        self.assertEqual(reminders.held_count(), 1)

    def test_released_after_settling_back_in(self):
        reminders._deliver_or_hold([_item("take a break")], False, 1000.0)
        # The moment presence returns, nothing is said yet -- the user has
        # only just touched the mouse.
        reminders._deliver_or_hold([], True, 1001.0)
        self.assertEqual(self.spoken, [])
        self.assertEqual(reminders.held_count(), 1)
        # Once they have settled, it arrives.
        reminders._deliver_or_hold([], True, 1001.0 + self.settle + 1)
        self.assertEqual(len(self.spoken), 1)
        self.assertIn("take a break", self.spoken[0])
        self.assertEqual(reminders.held_count(), 0)

    def test_several_held_reminders_arrive_as_one_summary(self):
        reminders._deliver_or_hold(
            [_item("call mum"), _item("stretch")], False, 1000.0)
        reminders._deliver_or_hold([_item("drink water")], False, 1200.0)
        self.assertEqual(reminders.held_count(), 3)
        reminders._deliver_or_hold([], True, 1300.0)
        reminders._deliver_or_hold([], True, 1300.0 + self.settle + 1)
        self.assertEqual(len(self.spoken), 1)
        for text in ("call mum", "stretch", "drink water"):
            self.assertIn(text, self.spoken[0])

    def test_leaving_again_restarts_the_settling_clock(self):
        reminders._deliver_or_hold([_item("stretch")], False, 1000.0)
        reminders._deliver_or_hold([], True, 1001.0)      # back
        reminders._deliver_or_hold([], False, 1002.0)     # gone again
        reminders._deliver_or_hold([], True, 1003.0)      # back once more
        self.assertEqual(self.spoken, [])
        self.assertEqual(reminders.held_count(), 1)

    def test_hold_gives_up_eventually(self):
        cap = float(getattr(config, "REMINDER_MAX_HOLD_MINUTES", 240)) * 60
        reminders._deliver_or_hold([_item("stretch")], False, 1000.0)
        reminders._deliver_or_hold([], False, 1000.0 + cap - 10)
        self.assertEqual(self.spoken, [])
        # Past the cap it is announced anyway rather than lost.
        reminders._deliver_or_hold([], False, 1000.0 + cap + 1)
        self.assertEqual(len(self.spoken), 1)
        self.assertIn("stretch", self.spoken[0])
        self.assertEqual(reminders.held_count(), 0)

    def test_snooze_target_is_the_last_delivered_reminder(self):
        reminders._deliver_or_hold(
            [_item("call mum"), _item("stretch")], False, 1000.0)
        reminders._deliver_or_hold([], True, 1100.0)
        reminders._deliver_or_hold([], True, 1100.0 + self.settle + 1)
        self.assertEqual(reminders._LAST_FIRED["text"], "stretch")


class PresenceFailure(unittest.TestCase):
    """A presence check that can't answer must never silence a reminder."""

    def test_missing_presence_module_counts_as_present(self):
        real = reminders.presence
        reminders.presence = None
        try:
            self.assertTrue(reminders._at_computer())
        finally:
            reminders.presence = real

    def test_raising_presence_check_counts_as_present(self):
        class Boom:
            AWAY = "away"

            @staticmethod
            def state():
                raise RuntimeError("no")

        real = reminders.presence
        reminders.presence = Boom
        try:
            self.assertTrue(reminders._at_computer())
        finally:
            reminders.presence = real


if __name__ == "__main__":
    unittest.main()
