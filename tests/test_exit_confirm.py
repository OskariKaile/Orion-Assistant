"""Tests for the shutdown confirmation in orion.py.

Shutting down is the only action Orion cannot walk back: the process calls
os._exit, so there is nothing left to say "actually, no" to. The exit intent
matches ordinary words ("bye", "close", "turn off"), and with the microphone
held open across a turn there is more speech reaching the matcher than there
used to be. So the first word only asks the question.

The methods are exercised against a stub carrying just the attributes they
touch, so nothing here opens a microphone, speaks, or exits.
"""

import importlib.util
import os
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import config  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "orion_exit_under_test", os.path.join(_ROOT, "orion.py"))
orion_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orion_mod)


class _FakeSpeaker:
    def __init__(self, log):
        self._log = log

    def say_blocking(self, text):
        self._log.append(("say_blocking", text))


class _Stub:
    """Just the parts of Orion the confirmation path touches."""

    _confirm_exit = orion_mod.Orion._confirm_exit
    _receive_exit_confirmation = orion_mod.Orion._receive_exit_confirmation

    def __init__(self):
        self.events = []
        self._pending_exit = None
        self.speaker = _FakeSpeaker(self.events)
        self.shutdown_reasons = []

    def _speak(self, text):
        self.events.append(("speak", text))

    def shutdown(self, reason="unspecified"):
        self.shutdown_reasons.append(reason)


class AskingFirst(unittest.TestCase):
    def setUp(self):
        self.orion = _Stub()

    def test_exit_intent_asks_instead_of_exiting(self):
        self.orion._confirm_exit()
        self.assertEqual(self.orion.shutdown_reasons, [])
        self.assertIsNotNone(self.orion._pending_exit)
        self.assertIn("Shut down", self.orion.events[0][1])

    def test_yes_shuts_down(self):
        self.orion._confirm_exit()
        self.assertTrue(self.orion._receive_exit_confirmation("yes"))
        self.assertEqual(len(self.orion.shutdown_reasons), 1)
        self.assertIn("exit confirmed", self.orion.shutdown_reasons[0])

    def test_no_cancels_and_stays_running(self):
        self.orion._confirm_exit()
        self.assertTrue(self.orion._receive_exit_confirmation("no"))
        self.assertEqual(self.orion.shutdown_reasons, [])
        self.assertIsNone(self.orion._pending_exit)

    def test_unrelated_answer_cancels_and_falls_through(self):
        # The question is dropped and the phrase is handled as a normal
        # command instead of being swallowed.
        self.orion._confirm_exit()
        self.assertFalse(
            self.orion._receive_exit_confirmation("what's the weather"))
        self.assertEqual(self.orion.shutdown_reasons, [])
        self.assertIsNone(self.orion._pending_exit)

    def test_a_stale_question_cannot_be_answered(self):
        # Saying "yes" to something else an hour later must not shut Orion
        # down.
        self.orion._confirm_exit()
        self.orion._pending_exit = time.monotonic() - 1
        self.assertFalse(self.orion._receive_exit_confirmation("yes"))
        self.assertEqual(self.orion.shutdown_reasons, [])

    def test_second_yes_does_nothing(self):
        self.orion._confirm_exit()
        self.orion._receive_exit_confirmation("yes")
        self.orion.shutdown_reasons.clear()
        self.assertFalse(self.orion._receive_exit_confirmation("yes"))
        self.assertEqual(self.orion.shutdown_reasons, [])


class ConfirmationDisabled(unittest.TestCase):
    def setUp(self):
        self.orion = _Stub()
        self._real = getattr(config, "CONFIRM_EXIT", True)
        config.CONFIRM_EXIT = False

    def tearDown(self):
        config.CONFIRM_EXIT = self._real

    def test_exits_on_the_first_word(self):
        self.orion._confirm_exit()
        self.assertEqual(len(self.orion.shutdown_reasons), 1)
        self.assertIn("exit intent", self.orion.shutdown_reasons[0])


if __name__ == "__main__":
    unittest.main()
