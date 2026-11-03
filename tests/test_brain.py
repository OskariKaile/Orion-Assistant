"""Offline tests for brain.py's matchers and routing helpers.

No mic, no network, no models -- pure text in, decision out. Run all suites
from the project root with:

    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402  -- puts audio/llm/features/ui on sys.path

import brain  # noqa: E402


class TestFindWake(unittest.TestCase):
    def test_wake_then_command(self):
        self.assertEqual(brain.find_wake("orion what time is it"),
                         "what time is it")

    def test_wake_alias(self):
        self.assertEqual(brain.find_wake("oreo open spotify"), "open spotify")

    def test_wake_only(self):
        self.assertEqual(brain.find_wake("orion"), "")

    def test_wake_with_punctuation(self):
        self.assertEqual(brain.find_wake("Orion, play music"), "play music")

    def test_no_wake(self):
        self.assertIsNone(brain.find_wake("turn on the lights"))

    def test_empty(self):
        self.assertIsNone(brain.find_wake(""))


class TestClassifyIntent(unittest.TestCase):
    def test_simple_commands(self):
        self.assertEqual(brain.classify_intent("stop"), "stop")
        self.assertEqual(brain.classify_intent("take a screenshot"),
                         "screenshot")
        self.assertEqual(brain.classify_intent("please mute yourself"),
                         "mute")
        self.assertEqual(brain.classify_intent("new conversation"), "clear")

    def test_long_question_is_not_a_command(self):
        self.assertIsNone(brain.classify_intent(
            "what is the tallest building in the world right now"))

    def test_empty(self):
        self.assertIsNone(brain.classify_intent(""))


class TestFillerCategory(unittest.TestCase):
    def test_web_search_wins(self):
        self.assertEqual(brain.filler_category("anything", web_search=True),
                         "search")

    def test_question_first_word(self):
        self.assertEqual(brain.filler_category("what is the weather"),
                         "think")
        # Regression for the _QUESTION_LEADS name shadowing: these words
        # only exist in the first-words set, not the lead-phrases tuple.
        self.assertEqual(brain.filler_category("was that correct"), "think")
        self.assertEqual(brain.filler_category("explain quantum physics"),
                         "think")

    def test_imperative_verb(self):
        self.assertEqual(brain.filler_category("open spotify"), "tool")
        self.assertEqual(brain.filler_category("set a timer"), "tool")

    def test_fallback(self):
        self.assertEqual(brain.filler_category(""), "think")
        self.assertEqual(brain.filler_category("bananas are great"), "think")


class TestEmailMatchers(unittest.TestCase):
    def test_check_any_account(self):
        self.assertEqual(brain.email_check_match("do i have any new email"),
                         "")

    def test_check_named_account(self):
        self.assertEqual(brain.email_check_match("check my work email"),
                         "work")

    def test_summary_named_account(self):
        self.assertEqual(
            brain.email_summary_match("summarize my personal email"),
            "personal")

    def test_summary_not_picked_up_by_check(self):
        self.assertIsNone(brain.email_check_match("summarize my email"))

    def test_no_email_noun(self):
        self.assertIsNone(brain.email_check_match("do i have any new games"))


class TestMusicMatcher(unittest.TestCase):
    def test_wrapped_track_request(self):
        self.assertEqual(
            brain.music_play_match("hey can you play eye of the tiger"),
            "eye of the tiger")

    def test_music_noun(self):
        self.assertEqual(brain.music_play_match("play some lofi"), "lofi")

    def test_bare_play(self):
        self.assertEqual(brain.music_play_match("play"), "")

    def test_play_tennis_not_intercepted(self):
        self.assertIsNone(brain.music_play_match("play tennis"))

    def test_unrelated(self):
        self.assertIsNone(brain.music_play_match("open spotify"))


class TestReminderMatchers(unittest.TestCase):
    def test_task_then_when(self):
        self.assertEqual(brain.reminder_set_match("remind me to call mom at 3pm"),
                         ("call mom", "at 3pm"))

    def test_when_then_task(self):
        self.assertEqual(
            brain.reminder_set_match("remind me today at 9pm to watch starship"),
            ("watch starship", "today at 9pm"))

    def test_no_time_returns_empty_when(self):
        self.assertEqual(brain.reminder_set_match("remind me to stretch"),
                         ("stretch", ""))

    def test_not_a_reminder(self):
        self.assertIsNone(brain.reminder_set_match("open the hub"))

    def test_list(self):
        self.assertTrue(brain.reminder_list_match("what are my reminders"))
        self.assertFalse(brain.reminder_list_match("remind me to call mom"))

    def test_clear(self):
        self.assertTrue(brain.reminder_clear_match("clear all reminders"))

    def test_cancel(self):
        self.assertEqual(
            brain.reminder_cancel_match("cancel the dentist reminder"),
            "dentist")

    def test_snooze_default_and_explicit(self):
        self.assertEqual(brain.reminder_snooze_match("snooze that"), 10)
        self.assertEqual(brain.reminder_snooze_match("snooze for 5 minutes"), 5)
        self.assertEqual(brain.reminder_snooze_match("snooze for 1 hour"), 60)
        self.assertIsNone(brain.reminder_snooze_match("open spotify"))


class TestAgendaMatcher(unittest.TestCase):
    def test_with_range_word(self):
        self.assertEqual(brain.agenda_query_match("what's on for tomorrow"),
                         "tomorrow")

    def test_default_range(self):
        self.assertEqual(brain.agenda_query_match("whats my schedule"),
                         "today")

    def test_no_match(self):
        self.assertIsNone(brain.agenda_query_match("open spotify"))

    def test_todo_query_routes_separately(self):
        self.assertTrue(brain.is_todo_query("what's on my todo list"))
        self.assertFalse(brain.is_todo_query("what's on for today"))


class TestSongTimeWebRouting(unittest.TestCase):
    def test_song_query(self):
        self.assertTrue(brain.is_song_query("what song is this"))
        # Contains the real-time hint "right now" but must stay a song query.
        self.assertTrue(brain.is_song_query("whats playing right now"))

    def test_time_query(self):
        self.assertTrue(brain.is_time_query("what time is it"))
        self.assertTrue(brain.is_time_query("what's the date today"))
        self.assertFalse(brain.is_time_query("set a timer for 5 minutes"))

    def test_web_search_hints(self):
        self.assertTrue(brain.wants_web_search("latest news on the election"))
        self.assertFalse(brain.wants_web_search("tell me a joke"))


class TestSelfQuery(unittest.TestCase):
    def test_topics(self):
        self.assertEqual(brain.self_query_match("what model are you"), "brain")
        self.assertEqual(brain.self_query_match("how do you hear me"), "stt")
        self.assertEqual(brain.self_query_match("what voice do you use"), "tts")
        self.assertEqual(brain.self_query_match("what can you do"),
                         "capabilities")

    def test_general(self):
        self.assertEqual(brain.self_query_match("who are you"), "general")

    def test_no_match(self):
        self.assertIsNone(brain.self_query_match("what is a transformer"))


class TestMiscMatchers(unittest.TestCase):
    def test_hub_open(self):
        self.assertTrue(brain.hub_open_match("open the hub"))
        self.assertTrue(brain.hub_open_match("show me the dashboard"))
        self.assertFalse(brain.hub_open_match("open spotify"))

    def test_health_query(self):
        self.assertTrue(brain.is_health_query("run diagnostics"))
        self.assertTrue(brain.is_health_query("status report"))
        self.assertFalse(brain.is_health_query("how are you"))

    def test_eod_skip(self):
        self.assertTrue(brain.eod_skip_match("skip tonight's nudge"))
        self.assertFalse(brain.eod_skip_match("what's on tonight"))

    def test_starts_with_imperative(self):
        self.assertTrue(brain.starts_with_imperative(
            "hey can you decrease my volume"))
        self.assertTrue(brain.starts_with_imperative("open the photos folder"))
        self.assertFalse(brain.starts_with_imperative(
            "why is the sky blue"))

    def test_looks_like_question(self):
        self.assertTrue(brain.looks_like_question("what time is it"))
        self.assertTrue(brain.looks_like_question("is it raining?"))
        self.assertFalse(brain.looks_like_question("open spotify"))


if __name__ == "__main__":
    unittest.main()
