"""Passive mood detection from the user's transcribed speech.

Pure-text heuristics -- no audio analysis, no model call, no measurable cost.
Returns "stressed" or "neutral". When stressed, Orion ADAPTS (shorter, plainer
replies, drop the JARVIS flourishes) but never comments on the user's mood
out loud. That meta-commentary is exactly what makes mood awareness annoying.
"""
import re

# Words/phrases that signal frustration. Whole-word, case-insensitive match.
_FRUSTRATION_WORDS = {
    "fuck", "fucking", "fucked", "shit", "damn", "damnit", "dammit",
    "bullshit", "wtf", "ffs", "hell",
    "ugh", "argh", "ughhh", "arghhh",
    "annoying", "stupid", "useless", "broken", "garbage", "trash",
    "hurry", "faster",
}

# Multi-word phrases that signal frustration.
_FRUSTRATION_PHRASES = (
    "come on",
    "for fuck",
    "shut up",
    "stop it",
    "not again",
    "still not",
    "doesnt work",
    "didnt work",
    "wont work",
    "what the hell",
    "are you serious",
)

_WORD_RE = re.compile(r"[a-zA-Z']+")
_CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")


def detect_mood(text):
    """Return "stressed" or "neutral" for the given user utterance."""
    if not text:
        return "neutral"

    lowered = text.lower().replace("'", "").replace("’", "")
    score = 0

    words = {w.lower() for w in _WORD_RE.findall(text)}
    if words & _FRUSTRATION_WORDS:
        score += 2

    for phrase in _FRUSTRATION_PHRASES:
        if phrase in lowered:
            score += 2
            break

    if _CAPS_RE.search(text):
        score += 1

    if text.count("!") >= 2:
        score += 1

    return "stressed" if score >= 2 else "neutral"


STRESSED_INSTRUCTION = (
    "The user sounds frustrated or stressed right now. Adapt: drop the dry "
    "wit, drop the 'Sir' address, skip any flourish or preamble, and answer "
    "in one or two short, direct sentences -- just the useful part. Do NOT "
    "comment on the user's mood or acknowledge it in any way; never say "
    "things like 'I sense you are upset' or apologise for the situation. "
    "Just be brief and helpful."
)
