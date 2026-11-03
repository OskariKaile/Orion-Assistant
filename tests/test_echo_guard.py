"""Tests for the transcript layer of the open-mic echo guard (audio/ear.py).

This layer decides whether a phrase captured while Orion was talking was the
user or Orion's own voice off the loudspeakers. Getting it wrong in one
direction makes Orion interrupt itself endlessly; in the other it makes Orion
ignore you while it talks. Both failures are covered here.
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import ear  # noqa: E402


REPLY = ("The weather in Helsinki today is sunny and mild, Sir, with a high "
         "of eighteen degrees.")


class EchoDetection(unittest.TestCase):
    def test_verbatim_echo_is_rejected(self):
        heard = "the weather in helsinki today is sunny and mild"
        self.assertTrue(ear.looks_like_echo(heard, REPLY))

    def test_partial_echo_is_rejected(self):
        # Speech-to-text on speaker bleed is lossy; a partial match still has
        # to be caught or Orion interrupts itself.
        heard = "helsinki today is sunny with a high of eighteen"
        self.assertTrue(ear.looks_like_echo(heard, REPLY))

    def test_real_command_passes(self):
        self.assertFalse(ear.looks_like_echo("change the song", REPLY))

    def test_unrelated_question_passes(self):
        heard = "what time is my meeting tomorrow"
        self.assertFalse(ear.looks_like_echo(heard, REPLY))


class Interruptions(unittest.TestCase):
    def test_stop_is_never_an_echo(self):
        # "stop" appearing in Orion's own sentence must not make Orion deaf to
        # being told to stop.
        self.assertFalse(
            ear.looks_like_echo("stop", "I will stop the music now, Sir."))

    def test_wait_is_never_an_echo(self):
        self.assertFalse(
            ear.looks_like_echo("wait", "Wait a moment while I look that up."))

    def test_short_word_from_the_reply_is_not_treated_as_echo(self):
        # A one-word phrase that is also a short filler word proves nothing.
        self.assertFalse(ear.looks_like_echo("and", REPLY))

    def test_content_word_from_the_reply_is_echo(self):
        self.assertTrue(ear.looks_like_echo("helsinki", REPLY))


class Edges(unittest.TestCase):
    def test_nothing_spoken_means_nothing_to_echo(self):
        self.assertFalse(ear.looks_like_echo("change the song", ""))

    def test_empty_transcript_is_not_echo(self):
        self.assertFalse(ear.looks_like_echo("", REPLY))

    def test_punctuation_and_case_are_ignored(self):
        self.assertTrue(
            ear.looks_like_echo("SUNNY AND MILD, SIR!", REPLY))


if __name__ == "__main__":
    unittest.main()
