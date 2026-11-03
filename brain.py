"""Orion's lightweight local brain.

Handles wake-word detection (tolerant of speech-to-text mishears) and intent
matching, so Orion understands "can you mute yourself right now" as a mute
command without it being an exact phrase.

Two layers of intent matching:
  1. difflib  -- pure standard library, microsecond-fast, great for the
     short command-like phrasings it was tuned for.
  2. semantic -- a tiny CPU embedding model (see semantic.py) that catches
     reworded, natural phrasings difflib would miss. Optional; if it isn't
     installed Orion runs on layer 1 alone.
"""

import difflib
import re

import config
import semantic

WAKE_WORD = config.WAKE_WORD.lower()

# Words speech-to-text commonly produces instead of "Orion".
_WAKE_ALIASES = {
    "orion", "oryan", "o'ryan", "orien", "oreon", "orin", "oran", "aurion",
    "auryon", "oriom", "ryan", "oreo", "oriana", "audion", "o'brien",
    "oreon", "aurean", "aurion", "oriyon", "oreyon",
}

# Polite/filler words stripped before intent matching.
_FILLER = {
    "can", "you", "could", "would", "will", "please", "now", "right", "just",
    "the", "a", "an", "to", "hey", "ok", "okay", "um", "uh", "your",
    "yourself", "self", "for", "me", "and", "do", "would", "kindly", "go",
    "really", "like", "i", "want", "need", "should",
}

# Canonical phrases for each built-in intent.
_INTENTS = {
    "stop": [
        "stop", "stop talking", "stop speaking", "be quiet", "quiet",
        "shut up", "silence", "enough", "cancel", "stop it", "hush",
        "stop reading", "pause",
    ],
    "mute": [
        "mute", "mute yourself", "go mute", "go silent", "no voice",
        "turn off your voice", "stop your voice", "disable voice",
        "voice off", "silent mode", "be silent",
    ],
    "unmute": [
        "unmute", "unmute yourself", "voice on", "turn on your voice",
        "speak again", "you can talk", "enable voice", "talk again",
        "start talking",
    ],
    "clear": [
        "clear", "clear chat", "clear the chat", "new chat",
        "new conversation", "start over", "fresh chat", "reset chat",
        "wipe chat", "clear conversation",
    ],
    "exit": [
        "exit", "quit", "shut down", "shutdown", "close", "turn off",
        "goodbye", "bye", "close yourself", "stop running",
        "shut yourself down", "power off", "go offline", "sign off",
    ],
    "explain": [
        "explain this", "explain the screen", "explain whats on screen",
        "describe this", "describe the screen", "look at this",
        "what am i looking at", "whats on my screen", "what is on my screen",
        "what do you see",
    ],
    "read": [
        "read this", "read it", "read the screen", "read this page",
        "read this out loud", "read this to me", "read aloud",
        "read the page",
    ],
    "summarize": [
        "summarize this", "summarize this page", "summarize the page",
        "summarize the screen", "summarise this", "summarise this page",
        "sum this up", "give me a summary", "summary of this page",
    ],
    "translate": [
        "translate this", "translate this page", "translate the screen",
        "translate this text", "translate the text", "translate the page",
    ],
    "screenshot": [
        "take a screenshot", "screenshot", "take a screen shot",
        "capture the screen", "capture my screen", "grab a screenshot",
        "save a screenshot", "take a picture of the screen",
    ],
    "code_review": [
        "whats wrong", "what is wrong", "whats wrong with this",
        "whats wrong with my code", "whats wrong with the code",
        "review my code", "review this code", "check my code",
        "check this code", "find the bug", "find the bugs",
        "is there a bug", "any bugs", "debug this", "debug my code",
    ],
    "code_fix": [
        "fix this", "fix the code", "fix my code", "fix this code",
        "fix the bug", "fix the bugs", "correct this code",
        "fix this file",
    ],
    "code_explain": [
        "explain this code", "explain the code", "explain my code",
        "explain this file", "explain this function", "what does this code do",
        "walk me through this code",
    ],
    "code_refactor": [
        "refactor this", "refactor this code", "refactor the code",
        "refactor my code", "clean up this code", "clean up the code",
        "tidy this code",
    ],
    "code_tests": [
        "write tests", "write tests for this", "write unit tests",
        "write tests for this code", "add tests", "test this code",
        "generate tests",
    ],
    "code_comments": [
        "add comments", "add comments to this", "add comments to this code",
        "comment this code", "comment my code", "document this code",
    ],
}

# How confident a fuzzy match must be to count as a command.
_INTENT_THRESHOLD = 0.80


def _normalize(text):
    return "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()


def find_wake(text):
    """Inspect a transcribed phrase for the wake word.

    Returns:
        - the text spoken *after* the wake word, or
        - "" if only the wake word was heard, or
        - None if the wake word was not detected.
    """
    if not text:
        return None
    lowered = text.lower()
    words = lowered.split()

    # The wake word should appear at or near the start of the phrase.
    for i, raw in enumerate(words[:3]):
        word = raw.strip(".,!?;:'\"")
        if word in _WAKE_ALIASES:
            return " ".join(words[i + 1:]).strip()
        # Fuzzy match only for words of 3+ letters: at the tuned 0.55
        # threshold, two-letter words like "on" / "no" score 0.57 against
        # "orion" and would wake Orion on any "turn on the..." phrase.
        # Real STT mishearings of the name are full-length words (and the
        # common ones are in _WAKE_ALIASES anyway).
        if len(word) < 3:
            continue
        ratio = difflib.SequenceMatcher(None, word, WAKE_WORD).ratio()
        if ratio >= config.WAKE_FUZZY_THRESHOLD:
            return " ".join(words[i + 1:]).strip()

    # Fallback: wake word spoken mid-sentence.
    if WAKE_WORD in lowered:
        return lowered.split(WAKE_WORD, 1)[1].strip()
    return None


# Words that suggest a question needs current, real-world information and
# should be answered with the AI's web search tool. Also includes explicit
# "search / look it up" phrasings so a vague web request still gets routed
# through search instead of being answered from the model's memory.
_REALTIME_HINTS = (
    "today", "tonight", "right now", "currently", "current", "latest",
    "news", "headline", "weather", "forecast", "temperature", "stock",
    "stocks", "share price", "exchange rate", "score", "who won", "this week",
    "this morning", "recent", "happening", "trending", "release date",
    "results", "open now", "as of", "this year", "live",
    "look up", "look it up", "look something up", "search for", "search the",
    "on the web", "from the web", "google", "find out", "find online",
)


# Phrasings that explicitly ask for the web, or topics only the web can
# answer (Orion has no weather/news/scores tool) -- always searched.
_EXPLICIT_WEB_HINTS = (
    "look up", "look it up", "look something up", "search for", "search the",
    "on the web", "from the web", "google", "find out", "find online",
    "weather", "forecast", "news", "headline", "stock", "share price",
    "exchange rate", "score", "who won", "release date",
)

# Words that tie a "today / right now" question to the user's own stuff,
# which Orion's tools answer -- not the web. ("What's on today", "what's
# playing right now", "any email this morning", "remind me tonight".)
_PERSONAL_HINTS = (
    " my ", " me ", " i ", "remind", "reminder", "calendar", "agenda",
    "schedule", "todo", "to do", "to-do", "email", "mail", "inbox", "timer",
    "alarm", "song", "playing", "music", "what time", "time is it", "date",
    "what's on", "whats on", "what is on", "volume", "lights", "thermostat",
)


def wants_web_search(text, tools_first=False):
    """True if `text` likely needs up-to-date information from the web.

    tools_first (config.AI_FIRST): a time word alone ("today", "right
    now") isn't enough, because that request now reaches the model with
    Orion's tools attached instead of a local fast-path. Commands and
    questions about the user's own calendar/email/music stay on the tools
    path; real-world questions (weather, news) and explicit "look it up"
    still go to web search.
    """
    low = " " + text.lower() + " "
    if any(hint in low for hint in _EXPLICIT_WEB_HINTS):
        return True
    if not any(hint in low for hint in _REALTIME_HINTS):
        return False
    if tools_first and (starts_with_imperative(text)
                        or any(hint in low for hint in _PERSONAL_HINTS)):
        return False
    return True


# Verbs that usually mean the user is asking Orion to DO something rather
# than answer a question -- they pick the "tool" filler bucket (e.g. "on it"
# instead of "let me think").
_IMPERATIVE_VERBS = {
    "open", "play", "start", "stop", "pause", "resume", "set", "turn",
    "mute", "unmute", "lock", "unlock", "schedule", "remind", "add",
    "delete", "remove", "find", "search", "show", "hide", "take", "send",
    "launch", "close", "restart", "exit", "switch", "run", "create",
    "make", "skip", "next", "previous", "increase", "decrease", "raise",
    "lower", "save", "load", "copy", "paste", "screenshot", "translate",
    "summarize", "summarise", "kill", "shut", "log", "draft", "write",
    "say", "speak", "read", "give", "list", "compose", "translate",
}

# First words that almost always make the utterance a question, so the
# "think" bucket fits even when an imperative verb appears later.
# (Distinct from _QUESTION_LEADS below: this one is single first-words only,
# used by filler_category; the other holds multi-word lead phrases for
# looks_like_question. They used to share a name and the later definition
# silently shadowed this one.)
_QUESTION_FIRST_WORDS = {
    "what", "whats", "who", "whos", "when", "where", "why", "how",
    "which", "whose", "is", "are", "was", "were", "does", "did", "can",
    "could", "should", "would", "will", "may", "might", "tell", "explain",
    "do",  # "do you ..." pattern; pure "do X" is rare in voice anyway
}


# --- intent fast-paths ---
# When the user's phrasing makes the intent obvious, we route DIRECTLY to the
# matching tool function instead of asking Gemini -- which on Flash-tier
# models can refuse or hallucinate even with the tool in front of it. These
# are intentionally narrow: any miss falls through to the normal Gemini
# function-calling path, which still works.

# Email INTENT trigger words. We split on a presence check (not strict
# prefix) so wrapper phrases like "hey can you ..." still match.
_EMAIL_NOUNS = ("email", "emails", "mail", "inbox", "unread")

_EMAIL_CHECK_HINTS = (
    "any new", "do i have", "anything new", "got any new",
    "check my", "is there anything", "are there any new",
    "any unread", "got anything", "anything in",
)

_EMAIL_SUMMARY_HINTS = (
    "summari", "summary", "read me", "read out",
    "go through", "go over", "tell me about",
    "what does", "what's in", "what is in",
)

_ACCOUNT_HINTS = ("personal", "work")  # match config.EMAIL_ACCOUNTS


def _email_account_in(text):
    """Return the account name mentioned in text, or '' (any)."""
    low = text.lower()
    for acc in _ACCOUNT_HINTS:
        if acc in low:
            return acc
    return ""


def email_check_match(text):
    """If text is clearly an unread-count check, return the account ('' for
    any). Returns None on no match."""
    if not text:
        return None
    low = text.lower()
    if not any(noun in low for noun in _EMAIL_NOUNS):
        return None
    if any(hint in low for hint in _EMAIL_SUMMARY_HINTS):
        return None  # leave summaries to the other matcher
    if any(hint in low for hint in _EMAIL_CHECK_HINTS):
        return _email_account_in(text)
    return None


def email_summary_match(text):
    """If text is clearly a request to summarise/read email, return the
    account ('' for any). Returns None on no match."""
    if not text:
        return None
    low = text.lower()
    if not any(noun in low for noun in _EMAIL_NOUNS):
        return None
    if any(hint in low for hint in _EMAIL_SUMMARY_HINTS):
        return _email_account_in(text)
    return None


# --- music fast-path ---
_MUSIC_LEADS = (
    "play ", "put on ", "queue up ", "start playing ", "play me ",
    "play some ", "play something ", "find me some ", "throw on ",
)
_MUSIC_NOUNS = ("music", "song", "track", "playlist", "tune", "tunes",
                "album", "beats", "lofi", "lo-fi", "radio")


def music_play_match(text):
    """If text is clearly a request to play music/audio, return the query
    Orion should hand to YouTube. Returns None on no match.

    "Play some lofi" -> "lofi". "Hey can you play eye of the tiger" ->
    "eye of the tiger". Just "play" alone -> "" (caller falls back to
    its own default).
    """
    if not text:
        return None
    low = text.lower().strip().rstrip(".!?")
    # Strip common conversational wrappers off the front.
    for wrap in ("hey ", "hey orion ", "orion ", "can you ", "could you ",
                 "would you ", "please "):
        if low.startswith(wrap):
            low = low[len(wrap):]
    # Need at least one of the leading verbs AND optionally a music noun
    # somewhere, OR the user literally just said "play".
    if low == "play":
        return ""
    # Match the LONGEST lead, not the first: "play some lofi" must strip
    # "play some " (-> "lofi"), not stop at "play " (-> "some lofi").
    matched_lead = None
    for lead in _MUSIC_LEADS:
        if low.startswith(lead) and (matched_lead is None
                                     or len(lead) > len(matched_lead)):
            matched_lead = lead
    if matched_lead is None:
        return None
    rest = low[len(matched_lead):].strip()
    # "play X" where X mentions a music noun OR is just a track/artist --
    # be slightly conservative so we don't intercept "play tennis" or
    # "play the next round". Require either a music noun present OR the
    # rest of the utterance to be 2+ words (treating it as a track name).
    if (any(n in rest for n in _MUSIC_NOUNS)
            or len(rest.split()) >= 2):
        return rest
    return None


def filler_category(text, web_search=False):
    """Pick a thinking-filler bucket ("search", "tool", or "think").

    Used so the spoken filler that plays while Orion thinks matches the
    kind of request -- "searching" for web lookups, "on it" for commands,
    "let me think" for open questions. Cheap, runs locally.
    """
    if web_search:
        return "search"
    if not text:
        return "think"
    words = text.strip().lower().split()
    if not words:
        return "think"
    first = words[0].strip(",.!?")
    if first in _QUESTION_FIRST_WORDS:
        return "think"
    if first in _IMPERATIVE_VERBS:
        return "tool"
    return "think"


# Phrasings that ask Orion to identify the song/music currently playing.
# Stored in _normalize form (apostrophes already stripped).
_SONG_QUERY_HINTS = (
    "what song", "which song", "whats this song", "what is this song",
    "song is this", "song is playing", "song thats playing",
    "what music", "which music", "music is playing", "what track",
    "name this song", "name the song", "name this track", "name that song",
    "whats playing", "what is playing", "identify this song",
    "identify the song", "identify this music", "recognize this song",
    "recognise this song", "shazam this", "shazam",
)


_EOD_SKIP_HINTS = (
    "skip tonight", "skip the nudge", "no nudge tonight",
    "dont nudge me tonight", "don't nudge me tonight",
    "skip the heads up", "skip todays nudge", "skip today's nudge",
)


def eod_skip_match(text):
    low = _normalize(text)
    if not low:
        return False
    return any(h in low for h in _EOD_SKIP_HINTS)


_TODO_QUERY_HINTS = (
    "todo list", "to do list", "to-do list", "my todos", "my to dos",
    "my to-dos", "whats on my todo", "what's on my todo",
    "read my todos", "read my to do", "read my to-do", "list my todos",
    "show my todos", "show me my todos", "what are my todos",
    "what's in my todo", "whats in my todo",
)


def is_todo_query(text):
    """True if the user is specifically asking about todos (not the full
    agenda). Routed BEFORE agenda_query_match because phrases like
    'what's on my todo list' would otherwise trigger the broader
    agenda summary (which also reads out reminders/events)."""
    low = _normalize(text)
    if not low:
        return False
    return any(h in low for h in _TODO_QUERY_HINTS)


_AGENDA_QUERY_HINTS = (
    "whats on", "what's on", "whats my schedule", "what is on",
    "what do i have", "anything coming up", "anything on my schedule",
    "what's my day", "whats my day", "what is my schedule",
    "show me my agenda", "show my agenda", "my agenda",
    "whats my plan", "what's my plan", "agenda for",
)
_AGENDA_WHEN_HINTS = (
    "today", "tomorrow", "tonight", "this week", "next week",
)


def agenda_query_match(text):
    """If text is asking for the agenda/schedule, return the range word.
    Returns None on no match. Default range is 'today' if no hint found."""
    low = _normalize(text)
    if not low:
        return None
    if not any(h in low for h in _AGENDA_QUERY_HINTS):
        return None
    for when in _AGENDA_WHEN_HINTS:
        if when in low:
            return when
    return "today"


_REMINDER_SET_LEADS = (
    "remind me ", "set a reminder ", "set reminder ", "remind me to ",
    "remind me that ", "give me a reminder ", "make a reminder ",
)
# Time-phrase markers that let us split "<task> ... <when>" apart locally.
_WHEN_MARKERS = (
    " in ", " at ", " on ", " by ", " today", " tonight", " tomorrow",
    " every ", " next ",
)


def _strip_reminder_lead(low):
    for lead in _REMINDER_SET_LEADS:
        if low.startswith(lead):
            return low[len(lead):].strip()
    return None


def reminder_set_match(text):
    """If `text` is a 'remind me ...' command, return (task, when) locally.

    Splits on the first time marker. Handles both orders:
        'remind me to call mom at 3pm'  -> ('call mom', 'at 3pm')
        'remind me today at 9pm to watch starship' -> ('watch starship',
                                                       'today at 9pm')
    Returns None if no time marker is present (caller falls through to
    Gemini, which can ask for clarification)."""
    if not text:
        return None
    low = text.strip().lower().rstrip(".!?")
    # Strip conversational wrappers.
    for wrap in ("hey orion ", "hey ", "orion ", "can you ", "could you ",
                 "would you ", "please "):
        if low.startswith(wrap):
            low = low[len(wrap):]
    rest = _strip_reminder_lead(low)
    if rest is None:
        return None
    # Find the earliest time marker. Special-case markers that can start the
    # remainder (no leading space): "today...", "tomorrow...", "tonight...",
    # "in ...", "at ...".
    marker_idx = -1
    for lead in ("today ", "tonight ", "tomorrow ", "in ", "at ", "on ",
                  "by ", "every ", "next "):
        if rest.startswith(lead):
            marker_idx = 0
            break
    if marker_idx == -1:
        for m in _WHEN_MARKERS:
            i = rest.find(m)
            if i != -1 and (marker_idx == -1 or i < marker_idx):
                marker_idx = i
    if marker_idx == -1:
        # No time phrase found -- task only, ask for time.
        return (re.sub(r"^(to|that)\s+", "", rest).strip(), "")
    if marker_idx == 0:
        # "<when> to <task>" pattern -- split on " to " AFTER the when.
        to_idx = rest.find(" to ")
        if to_idx == -1:
            # Just a time, no task -- task missing.
            return ("", rest.strip())
        when = rest[:to_idx].strip()
        task = rest[to_idx + len(" to "):].strip()
    else:
        # "<task> <when>" pattern.
        task = rest[:marker_idx].strip()
        when = rest[marker_idx:].strip()
    # Drop a leading "to "/"that " from task.
    task = re.sub(r"^(to|that)\s+", "", task)
    return (task.strip(), when.strip())


_REMINDER_LIST_HINTS = (
    "what are my reminders", "list my reminders", "list reminders",
    "show my reminders", "show me my reminders", "any reminders",
    "do i have any reminders", "what reminders do i have",
    "whats on my reminders", "what's on my reminders",
    "read my reminders", "pending reminders",
)
_REMINDER_CLEAR_HINTS = (
    "clear all reminders", "cancel all reminders", "cancel all my reminders",
    "delete all reminders", "wipe my reminders", "remove all reminders",
)
_REMINDER_CANCEL_LEADS = (
    "cancel the ", "cancel my ", "remove the ", "delete the ",
    "cancel reminder ", "remove reminder ",
)
_REMINDER_SNOOZE_HINTS = (
    "snooze", "snooze that", "snooze it", "remind me again",
)


def reminder_list_match(text):
    low = _normalize(text)
    if not low:
        return False
    return any(h in low for h in _REMINDER_LIST_HINTS)


def reminder_clear_match(text):
    low = _normalize(text)
    if not low:
        return False
    return any(h in low for h in _REMINDER_CLEAR_HINTS)


def reminder_cancel_match(text):
    """Returns the words to match against, or None."""
    low = _normalize(text)
    if not low:
        return None
    for lead in _REMINDER_CANCEL_LEADS:
        if low.startswith(lead):
            rest = low[len(lead):].strip()
            # Strip a trailing " reminder" if present.
            rest = re.sub(r"\s+reminder$", "", rest)
            if rest:
                return rest
    return None


def reminder_snooze_match(text):
    """Returns minutes (int) if this is a snooze command, else None."""
    low = _normalize(text)
    if not low:
        return None
    if not any(h in low for h in _REMINDER_SNOOZE_HINTS):
        return None
    m = re.search(r"(\d+)\s*(minute|min|hour|hr)", low)
    if m:
        n = int(m.group(1))
        return n * 60 if m.group(2).startswith(("hour", "hr")) else n
    return 10


_HEALTH_HINTS = (
    "system check", "systems check", "self check", "self test",
    "diagnostic", "diagnostics", "status report", "health check",
    "system status", "run diagnostics", "everything working",
    "everything ok", "are you healthy", "how are your systems",
)


def is_health_query(text):
    """True if the user is asking for the Orion + PC self-diagnostic."""
    low = _normalize(text)
    if not low:
        return False
    return any(hint in low for hint in _HEALTH_HINTS)


_HUB_OPEN_HINTS = (
    "open the hub", "open hub", "open dashboard", "open the dashboard",
    "show me the hub", "show the hub", "show the dashboard",
    "show me the dashboard", "open the universal hub", "universal hub",
    "open my dashboard", "show stats", "show me stats", "open stats",
    "open the orion hub",
)


def hub_open_match(text):
    """True if the user is asking to open the Universal Hub dashboard."""
    low = _normalize(text)
    if not low:
        return False
    return any(hint in low for hint in _HUB_OPEN_HINTS)


# Self-knowledge fast-path. Returns a topic key matching about._TOPICS,
# or "general" for an open question, or None for no match. Checked BEFORE
# Gemini so Flash-Lite can't hallucinate a different stack.
_SELF_TOPIC_HINTS = (
    # (topic, hint substrings)
    ("brain", (
        "what model are you", "what model do you use", "which model are you",
        "what llm", "which llm", "what ai are you", "which ai do you use",
        "what are you running on", "what's your brain", "whats your brain",
        "what powers you", "are you gemini", "are you grok", "are you claude",
        "are you chatgpt", "what model is this",
    )),
    ("stt", (
        "how do you hear me", "how do you understand me", "how do you transcribe",
        "what speech recognition", "what stt", "which stt", "do you use whisper",
        "are you using whisper", "how does your speech recognition",
        "speech to text",
    )),
    ("tts", (
        "what voice are you", "what voice do you use", "whose voice is that",
        "what tts", "which tts", "how do you speak", "what's your voice",
        "whats your voice", "what text to speech", "are you using elevenlabs",
    )),
    ("wake", (
        "what's your wake word", "whats your wake word", "what is your wake word",
        "how do you hear the wake", "what wakes you up",
    )),
    ("capabilities", (
        "what can you do", "what are you able to do", "what are your features",
        "what features do you have", "what are your capabilities",
        "list your features", "list your capabilities", "what can you help with",
    )),
    ("stack", (
        "what are you built with", "what are you built on", "what libraries",
        "what tech stack", "what's your stack", "whats your stack",
        "how are you built", "what code are you",
    )),
    ("privacy", (
        "are you private", "is this private", "do you send my data",
        "where does my data go", "what runs locally", "what runs in the cloud",
        "are you offline",
    )),
)

_SELF_GENERAL_HINTS = (
    "tell me about yourself", "describe yourself", "who are you",
    "what are you", "introduce yourself", "tell me what you are",
)


def self_query_match(text):
    """Return topic key, 'general', or None."""
    low = _normalize(text)
    if not low:
        return None
    for topic, hints in _SELF_TOPIC_HINTS:
        if any(h in low for h in hints):
            return topic
    if any(h in low for h in _SELF_GENERAL_HINTS):
        return "general"
    return None


def is_song_query(text):
    """True if `text` is asking Orion to identify the song that's playing.

    Checked before web-search routing so phrasings like "what's playing right
    now" -- which contain a real-time hint word -- still reach the song
    recognizer instead of being sent to web search.
    """
    low = _normalize(text)
    if not low:
        return False
    return any(hint in low for hint in _SONG_QUERY_HINTS)


# Phrasings that ask for the current time or date. Stored in _normalize form.
_TIME_QUERY_HINTS = (
    "what time is it", "whats the time", "what is the time", "time is it",
    "current time", "tell me the time", "got the time", "do you have the time",
    "what day is it", "what is the day", "whats the date", "what is the date",
    "todays date", "what is todays date", "what is the date today",
    "what day is today", "the time right now",
)


def is_time_query(text):
    """True if `text` is asking for the current time or date.

    Checked before web-search routing so "what's the date today" -- which
    contains the real-time hint word "today" -- is answered from the PC clock
    instead of being sent to web search.
    """
    low = _normalize(text)
    if not low:
        return False
    return any(hint in low for hint in _TIME_QUERY_HINTS)


def _phrase_score(candidate, phrase):
    """Score how well a candidate string matches a canonical phrase (0..1)."""
    if candidate == phrase:
        return 1.0
    cand_words = candidate.split()
    if " " in phrase:
        if phrase in candidate:
            return 0.9
    else:
        # Single-word intent: only count it for short, command-like phrases.
        if phrase in cand_words and len(cand_words) <= 3:
            return 0.9
    return difflib.SequenceMatcher(None, candidate, phrase).ratio()


def classify_intent(text):
    """Return a built-in intent name for `text`, or None if it's a question.

    Intents: "stop", "mute", "unmute", "clear", "exit", "explain", "read",
    "summarize", "translate", "screenshot", "code_review", "code_fix",
    "code_explain", "code_refactor", "code_tests", "code_comments".
    Anything that isn't a clear, short command returns None so it goes to
    the AI as a normal query.
    """
    raw = _normalize(text)
    if not raw:
        return None

    core = " ".join(w for w in raw.split() if w not in _FILLER).strip()
    candidates = [c for c in (raw, core) if c]

    # Long phrases are real questions for the AI, not control commands.
    if core and len(core.split()) > 5:
        return None

    best_intent, best_score = None, 0.0
    for intent, phrases in _INTENTS.items():
        for phrase in phrases:
            for cand in candidates:
                score = _phrase_score(cand, phrase)
                if score > best_score:
                    best_intent, best_score = intent, score

    return best_intent if best_score >= _INTENT_THRESHOLD else None


def exact_intent(text, intents):
    """Return one of `intents` only when `text` IS one of its canonical
    phrases (after dropping filler words) -- no fuzzy scoring. Used by
    AI-first routing, where "stop the timer" or "cancel my reminder" must
    reach the model instead of fuzzily matching "stop" (stop talking)."""
    raw = _normalize(text)
    if not raw:
        return None
    core = " ".join(w for w in raw.split() if w not in _FILLER).strip()
    for intent in intents:
        phrases = _INTENTS.get(intent, ())
        if raw in phrases or core in phrases:
            return intent
    return None


def classify_intent_scored(text):
    """Classify `text` into a built-in intent with a confidence score.

    Returns (intent, confidence) where confidence is in 0..1:
      - difflib finds a clear match  -> (intent, 1.0)
      - the semantic brain matches   -> (intent, cosine similarity)
      - nothing matches              -> (None, 0.0)

    The caller decides what to do with the score: act on a high score, ask
    the user to confirm a middling one, and ignore a low one.
    """
    if not _normalize(text):
        return None, 0.0

    # Fast path: the difflib matcher. If it is confident, trust it fully.
    exact = classify_intent(text)
    if exact is not None:
        return exact, 1.0

    # Slow path: semantic matching for natural, reworded phrasings.
    if config.SEMANTIC_INTENT:
        return semantic.classify(text)
    return None, 0.0


# Words a phrase tends to start with when it is a question for the AI rather
# than a command for Orion to carry out.
_QUESTION_LEADS = (
    "what", "whats", "what's", "how", "why", "who", "whos", "who's", "when",
    "where", "which", "whose", "is", "are", "am", "do", "does", "did", "can",
    "could", "should", "would", "will", "tell me", "give me", "i wonder",
)


def starts_with_imperative(text):
    """True if `text` begins with a verb that clearly asks Orion to DO
    something (decrease, open, play...).

    Used to short-circuit mid-confidence semantic intent confirmation: an
    imperative command that didn't match a built-in intent at high confidence
    is almost always a tool request -- handing it to Gemini's tool picker is
    safer than guessing at a clarifier.
    """
    if not text:
        return False
    # Strip conversational wrappers ("hey orion ", "can you ", ...) the way
    # the other matchers do, so STT variants land on the real first verb.
    low = text.strip().lower().rstrip(".!?")
    for wrap in ("hey orion ", "hey ", "orion ", "can you ", "could you ",
                 "would you ", "will you ", "please ", "i want you to ",
                 "i need you to "):
        if low.startswith(wrap):
            low = low[len(wrap):]
    first = low.split(" ", 1)[0].strip(",.!?") if low else ""
    return first in _IMPERATIVE_VERBS


def looks_like_question(text):
    """True if `text` reads like a question for the AI, not a command."""
    low = _normalize(text)
    if not low:
        return False
    if text.strip().endswith("?"):
        return True
    return any(low == lead or low.startswith(lead + " ")
               for lead in _QUESTION_LEADS)
