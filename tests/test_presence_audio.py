"""Tests for the output-meter half of features/presence.py.

The meter is a COM object. Reading it in Orion's process killed Orion twice,
in a way worth stating precisely because the obvious fix does not work:

  * A COM pointer must be released on the thread that created it. Releasing
    it elsewhere is an access violation, which ends the process instantly
    with no exception and no traceback.
  * Creating it on a dedicated thread is not enough. Python decides when to
    free it, and an object caught in a reference cycle is freed by the cyclic
    collector on whatever thread happened to trigger it.

So the meter runs in its own process and Orion reads numbers off a pipe. The
contract pinned down here is: no COM ever enters Orion's process for this,
and callers only ever receive a bool.
"""

import gc
import os
import subprocess
import sys
import threading
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import presence  # noqa: E402


class NoComInOrionsProcess(unittest.TestCase):
    """The invariant that keeps Orion alive."""

    def tearDown(self):
        presence.suspend_audio_signal(False)

    def test_reading_the_meter_imports_no_com_library(self):
        presence.audio_playing()
        time.sleep(0.5)
        presence.audio_playing()
        gc.collect()
        self.assertNotIn("comtypes", sys.modules)
        self.assertNotIn("pycaw", sys.modules)

    def test_the_probe_is_a_real_separate_program(self):
        probe = presence._probe_path()
        self.assertTrue(os.path.exists(probe), probe)
        out = subprocess.run([sys.executable, probe, "--once"],
                             capture_output=True, text=True, timeout=60)
        first = (out.stdout or "").strip().splitlines()
        self.assertTrue(first, "probe printed nothing")
        # Every line is a float: a level, or -1 when the meter is unreadable.
        float(first[0])

    def test_caller_only_ever_gets_a_bool(self):
        self.assertIsInstance(presence.audio_playing(), bool)


class MeterStaysOutOfProcess(unittest.TestCase):
    def tearDown(self):
        presence.suspend_audio_signal(False)

    def test_sampling_happens_on_a_dedicated_thread(self):
        presence.audio_playing()
        names = [t.name for t in threading.enumerate()]
        self.assertIn("presence-audio", names)

    def test_only_one_sampler_however_many_callers(self):
        results = []

        def hammer():
            for _ in range(20):
                results.append(presence.audio_playing())

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        samplers = [t for t in threading.enumerate()
                    if t.name == "presence-audio"]
        self.assertEqual(len(samplers), 1)
        self.assertTrue(all(isinstance(r, bool) for r in results))

    def test_orions_own_voice_is_not_counted(self):
        presence.suspend_audio_signal(True)
        self.assertFalse(presence.audio_playing())

    def test_repeated_calls_do_not_accumulate_threads(self):
        for _ in range(50):
            presence.audio_playing()
        time.sleep(0.2)
        samplers = [t for t in threading.enumerate()
                    if t.name == "presence-audio"]
        self.assertLessEqual(len(samplers), 1)


class StatesDegradeSafely(unittest.TestCase):
    def test_state_is_one_of_the_three(self):
        self.assertIn(presence.state(),
                      (presence.ACTIVE, presence.PASSIVE, presence.AWAY))

    def test_describe_reports_every_signal(self):
        info = presence.describe()
        for key in ("state", "idle_seconds", "locked", "audio_playing",
                    "fullscreen"):
            self.assertIn(key, info)

    def test_hearing_the_user_means_present(self):
        presence.note_voice()
        self.assertEqual(presence.state(), presence.ACTIVE)


if __name__ == "__main__":
    unittest.main()
