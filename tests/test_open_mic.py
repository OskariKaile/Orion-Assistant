"""Tests for the open-mic turn logic in orion.py and audio/ear.py.

Two decisions are covered, because they are the ones that decide whether the
feature feels right or maddening:

  * When speech arriving mid-turn should be folded into the command already
    running, and when it should replace it.
  * How the echo guard's loudness bar reacts to hearing Orion's own voice, so
    an open mic is usable on loudspeakers and not just on headphones.

Neither test touches a microphone. `_watch_turn` is exercised against a stub
ear, and the ear's bar is exercised through its calibration entry point.
"""

import importlib.util
import os
import sys
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import config  # noqa: E402
import ear as ear_mod  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "orion_under_test", os.path.join(_ROOT, "orion.py"))
orion_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orion_mod)


class _StubEar:
    """Hands out pre-made phrases, then nothing."""

    def __init__(self, phrases):
        self._phrases = list(phrases)

    def get(self, timeout=0.0):
        if self._phrases:
            return self._phrases.pop(0)
        time.sleep(min(timeout, 0.01))
        return None


class _Watcher:
    """The parts of Orion that _watch_turn actually touches."""

    _watch_turn = orion_mod.Orion._watch_turn

    def __init__(self, phrases, acted_token=None):
        self.running = True
        self._ear = _StubEar(phrases)
        self._acted_token = acted_token

    def _judge_phrase(self, phrase):
        return phrase.text


def _phrase(text, t_start, during_playback=False):
    p = ear_mod.Phrase(audio=None, t_start=t_start, t_end=t_start + 1.0,
                       rms=900.0, during_playback=during_playback)
    p.text = text
    return p


def _busy_worker(seconds):
    t = threading.Thread(target=time.sleep, args=(seconds,), daemon=True)
    t.start()
    return t


class MergeDecision(unittest.TestCase):
    """A pause mid-thought continues the command; a later interruption
    replaces it."""

    def test_speech_inside_the_window_continues_the_command(self):
        window = float(config.MERGE_WINDOW)
        watcher = _Watcher([_phrase("and make it quieter", 100.0 + window - 1)])
        result = watcher._watch_turn(_busy_worker(2), token=1, last_end=100.0)
        self.assertIsNotNone(result)
        text, _, merged = result
        self.assertEqual(text, "and make it quieter")
        self.assertTrue(merged)

    def test_speech_after_the_window_starts_a_new_command(self):
        window = float(config.MERGE_WINDOW)
        watcher = _Watcher([_phrase("what's the weather", 100.0 + window + 2)])
        result = watcher._watch_turn(_busy_worker(2), token=1, last_end=100.0)
        _, _, merged = result
        self.assertFalse(merged)

    def test_no_merging_once_the_turn_has_already_acted(self):
        # The song has already been changed. Folding new speech back into the
        # old command would change it a second time.
        window = float(config.MERGE_WINDOW)
        watcher = _Watcher([_phrase("something calmer", 100.0 + window - 1)],
                           acted_token=1)
        _, _, merged = watcher._watch_turn(_busy_worker(2), token=1,
                                           last_end=100.0)
        self.assertFalse(merged)

    def test_echoes_are_skipped_and_the_turn_survives(self):
        class EchoWatcher(_Watcher):
            def _judge_phrase(self, phrase):
                return "" if phrase.text == "ECHO" else phrase.text

        watcher = EchoWatcher([_phrase("ECHO", 101.0),
                               _phrase("stop", 102.0)])
        text, _, _ = watcher._watch_turn(_busy_worker(2), token=1,
                                         last_end=100.0)
        self.assertEqual(text, "stop")

    def test_untouched_turn_returns_nothing(self):
        watcher = _Watcher([])
        finished = threading.Thread(target=lambda: None)
        finished.start()
        finished.join()
        self.assertIsNone(
            watcher._watch_turn(finished, token=1, last_end=100.0))


class EchoBar(unittest.TestCase):
    """The speech bar has to rise above whatever Orion leaks into the mic,
    and come back down when it stops leaking."""

    def setUp(self):
        self.ear = ear_mod.ContinuousEar()
        # Before the mic has run, the floor is the seeded ambient estimate.
        # Once frames start arriving, _track_ambient replaces it with a real
        # measurement of the room.
        self.floor = max(
            float(getattr(config, "ENERGY_THRESHOLD", 300))
            * float(getattr(config, "EAR_AMBIENT_MARGIN", 2.2)),
            float(getattr(config, "EAR_MIN_BAR", 120)))

    def test_bar_is_the_ambient_floor_when_silent(self):
        self.assertEqual(self.ear.bar(), self.floor)

    def test_loud_leak_raises_the_bar_while_speaking(self):
        self.ear.note_self(2000.0)
        quiet_bar = self.ear.bar()
        self.ear.set_playing(True, "some reply")
        self.assertGreater(self.ear.bar(), quiet_bar)
        self.assertGreater(self.ear.bar(), 2000.0)

    def test_headphones_never_raise_the_bar(self):
        # Nothing leaks back, so nothing is learned and the bar stays put:
        # talking over Orion on headphones is as easy as talking to it.
        self.ear.set_playing(True, "some reply")
        self.assertEqual(self.ear.bar(), self.floor)

    def test_calibration_measures_the_leak_when_the_window_closes(self):
        self.ear.set_playing(True, "some reply")
        self.assertTrue(self.ear._calibrating(1500.0))   # inside the window
        self.assertEqual(self.ear.self_level, 0.0)       # not committed yet
        self.ear._calib_until = time.monotonic() - 0.01  # window closes
        self.assertFalse(self.ear._calibrating(10.0))
        self.assertEqual(self.ear.self_level, 1500.0)

    def test_a_bad_calibration_sample_decays_away(self):
        # You happened to speak during one calibration window. The inflated
        # level must not deafen Orion for the rest of the session.
        self.ear.note_self(4000.0)
        for _ in range(20):
            self.ear.set_playing(True, "reply")
            self.ear._calibrating(5.0)
            self.ear._calib_until = time.monotonic() - 0.01
            self.ear._calibrating(5.0)
            self.ear.set_playing(False)
        self.assertLess(self.ear.self_level, 1000.0)

    def test_proven_echo_raises_the_level_immediately(self):
        self.ear.note_self(800.0)
        self.assertEqual(self.ear.self_level, 800.0)
        self.ear.note_self(300.0)  # quieter proof never lowers the ceiling
        self.assertEqual(self.ear.self_level, 800.0)


if __name__ == "__main__":
    unittest.main()
