"""AI-first routing (config.AI_FIRST): which requests stay local quick
commands, and which reach the model -- with tools, or with web search."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths  # noqa: E402,F401
import brain  # noqa: E402
import commands  # noqa: E402


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


class FakeOrion:
    hud = None

    def __init__(self):
        self.speaker = FakeSpeaker()

    def show_last(self, text):
        pass


class QuickDispatchTest(unittest.TestCase):
    """Only instant controls run locally; the handlers are stubbed so no
    media keys are actually pressed."""

    def setUp(self):
        self.calls = []
        mc, pc = commands.media_control, commands.pc_control
        self._saved = (mc.play_pause, mc.next_track, mc.previous_track,
                       pc.nudge_volume, pc.set_volume)
        mc.play_pause = lambda: self.calls.append("play_pause") or ""
        mc.next_track = lambda: self.calls.append("next") or ""
        mc.previous_track = lambda: self.calls.append("previous") or ""
        pc.nudge_volume = lambda up: self.calls.append(f"vol{'+' if up else '-'}") or ""
        pc.set_volume = lambda n: self.calls.append(f"vol={n}") or ""

    def tearDown(self):
        mc, pc = commands.media_control, commands.pc_control
        (mc.play_pause, mc.next_track, mc.previous_track,
         pc.nudge_volume, pc.set_volume) = self._saved

    def quick(self, text):
        self.calls.clear()
        handled = commands.quick_dispatch(text, FakeOrion())
        return self.calls[0] if handled and self.calls else (handled or None)

    def test_instant_controls_stay_local(self):
        self.assertEqual(self.quick("skip"), "next")
        self.assertEqual(self.quick("skip this song"), "next")
        self.assertEqual(self.quick("next track please"), "next")
        self.assertEqual(self.quick("previous song"), "previous")
        self.assertEqual(self.quick("pause"), "play_pause")
        self.assertEqual(self.quick("pause the music"), "play_pause")
        self.assertEqual(self.quick("stop the music"), "play_pause")
        self.assertEqual(self.quick("volume up"), "vol+")
        self.assertEqual(self.quick("set volume to 30"), "vol=30")

    def test_everything_else_goes_to_the_model(self):
        for text in ("what's next on my calendar this week",
                     "play some lofi",
                     "play some music",
                     "open spotify",
                     "set a timer for five minutes",
                     "remind me to call mom at six",
                     "any new emails",
                     "why did the music stop",
                     "can you skip to the next song after this one ends please"):
            self.assertIsNone(self.quick(text), text)


class ExactIntentTest(unittest.TestCase):
    """stop / mute / unmute stay local only on an exact phrase."""

    Q = {"stop", "mute", "unmute"}

    def test_exact_phrases_match(self):
        for text, want in (("stop", "stop"), ("shut up", "stop"),
                           ("please stop", "stop"), ("be quiet", "stop"),
                           ("mute yourself", "mute"), ("voice on", "unmute")):
            self.assertEqual(brain.exact_intent(text, self.Q), want, text)

    def test_sentences_go_to_the_model(self):
        for text in ("stop the timer", "cancel my reminder",
                     "silence my phone", "stop reminding me",
                     "turn off the lights", "close spotify"):
            self.assertIsNone(brain.exact_intent(text, self.Q), text)


class WebSearchSplitTest(unittest.TestCase):
    """With tools first, 'today'/'right now' alone no longer forces web
    search -- the user's own stuff goes to the tool-using model."""

    def web(self, text):
        return brain.wants_web_search(text, tools_first=True)

    def test_personal_and_commands_use_tools(self):
        for text in ("what's on today", "what's on my calendar this week",
                     "what song is playing right now",
                     "what time is it right now",
                     "any emails this morning", "remind me tonight to stretch",
                     "set the temperature to 20"):
            self.assertFalse(self.web(text), text)

    def test_real_world_questions_still_search(self):
        for text in ("what's the weather today", "latest news",
                     "what's the weather like for me today",
                     "who won the game last night",
                     "look up the release date of the new zelda"):
            self.assertTrue(self.web(text), text)

    def test_legacy_behaviour_unchanged(self):
        self.assertTrue(brain.wants_web_search("what's on today"))


if __name__ == "__main__":
    unittest.main()
