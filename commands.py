"""Orion's parameterized command router (Groups 6-11).

brain.classify_intent handles fixed control words. This module handles
commands that carry an argument ("open spotify", "shutdown in 5 minutes",
"set a timer for 10 minutes", "work mode", ...).

dispatch() is called with the post-wake-word text and the Orion instance. It
returns True if it recognised and ran a command, False otherwise.
"""

import datetime
import re

import browser_control
import config
import media_control
import pc_control
import productivity_tools
import scenes
import smart_home

# Leading politeness words stripped before matching.
_LEAD_FILLER = {
    "can", "you", "u", "could", "would", "will", "please", "pls", "plz",
    "now", "just", "hey", "hi", "ok", "okay", "um", "uh", "kindly", "really",
    "for", "me", "yo", "lets", "let",
}

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "a": 1, "an": 1, "half": 0,
}

_MEDIA_NAMES = ("spotify", "youtube", "netflix")

# Conjunctions that join two separate spoken actions ("play music and then
# open youtube"). When one appears, the request is handed to the AI so it can
# return several tool calls -- the single-shot dispatcher below only ever runs
# the first action it matches and silently drops the rest.
_ACTION_JOINERS = (
    " and then ", " then ", " and also ", " and ", " also ", " after that ",
    " as well as ", " plus ",
)


def has_multiple_actions(query):
    """True if `query` looks like it joins two or more separate actions."""
    q = " " + query.lower().strip() + " "
    return any(j in q for j in _ACTION_JOINERS)


def _clean(text):
    """Lowercase, fix common mishearings, and strip leading filler words."""
    misheard = {k.lower(): v for k, v in
                getattr(config, "MISHEARD_WORDS", {}).items()}
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    words = [misheard.get(w, w) for w in words]
    while words and words[0] in _LEAD_FILLER:
        words.pop(0)
    return " ".join(words)


def _extract_number(text):
    """Return the first integer found in `text` (digits or words), or None."""
    digits = re.search(r"\d+", text)
    if digits:
        return int(digits.group())
    total = None
    for word in text.split():
        if word in _NUMBER_WORDS:
            total = (total or 0) + _NUMBER_WORDS[word]
    return total


def _extract_minutes(text):
    """Return a duration in minutes parsed from `text`, or None."""
    n = _extract_number(text)
    if n is None:
        return None
    if re.search(r"\bhour", text):
        return n * 60
    if re.search(r"\bsecond", text):
        return n / 60.0
    return n  # default unit is minutes


def _minutes_until_clock(text):
    """Parse 'at 3pm' / 'at 14:30' and return minutes from now, or None."""
    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = m.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    now = datetime.datetime.now()
    target = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds() / 60.0


def _after(text, *prefixes):
    """If `text` starts with any prefix, return the remainder; else None."""
    for prefix in prefixes:
        if text == prefix:
            return ""
        if text.startswith(prefix + " "):
            return text[len(prefix) + 1:].strip()
    return None


def dispatch(query, orion):
    """Parse `query` and run a Group 6-11 command. Returns True if handled."""
    speak = orion.speaker.say
    q = _clean(query)
    if not q:
        return False

    result = _route(q, query, orion, speak)
    if result is None:
        return False
    if result:  # non-empty string -> speak it
        orion.show_last(result)
        speak(result)
    return True


# Quick commands (config.AI_FIRST): only instant controls skip the model.
# Anything longer than this is a sentence worth the model's reading.
_QUICK_MAX_WORDS = 6


def quick_dispatch(query, orion):
    """AI_FIRST counterpart of dispatch(): runs only instant controls --
    media play/pause/skip/previous, volume, HUD show/hide. Everything else
    returns False so the model can work out what was meant."""
    q = _clean(query)
    if not q or len(q.split()) > _QUICK_MAX_WORDS:
        return False
    for route in (_route_hud, _route_media, _route_volume):
        result = (route(q, orion, strict=True) if route is _route_media
                  else route(q, orion))
        if result is not None:
            if result:
                orion.show_last(result)
                orion.speaker.say(result)
            return True
    return False


def _route_hud(q, orion):
    if q in ("show yourself", "show the ui", "show ui", "show orion",
             "show the hud", "show hud", "show the interface",
             "show your face", "open the ui", "open ui", "appear"):
        if orion.hud is not None:
            orion.hud.show()
        return ""
    if q in ("hide yourself", "hide the ui", "hide ui", "hide orion",
             "hide the hud", "hide hud", "hide the interface",
             "close the ui", "close ui", "disappear", "go away"):
        if orion.hud is not None:
            orion.hud.hide()
        return ""
    return None


def _route_media(q, orion=None, strict=False):
    # Tolerant matching: STT rarely produces the exact phrase, so accept any
    # wording that clearly means "control playback" -- "play some music",
    # "put the music on", "skip this one", etc.
    # strict (quick commands) is narrower: the control word must come first
    # ("what's next on my calendar this week" is not a skip), and "play ..."
    # is left to the model -- "play some music" may mean "find me something
    # on YouTube".
    find = re.match if strict else re.search
    verbs = (r"(pause|resume|stop|unpause)\b" if strict
             else r"\b(play|pause|resume|stop|unpause|put|turn|back)\b")
    exact = ("play", "pause", "resume", "pause music", "resume music",
             "play pause", "unpause") + (() if strict else ("play music",))
    if q in exact or (
            find(verbs, q)
            and re.search(r"\b(music|song|track|tune|playback|audio)\b", q)):
        return media_control.play_pause()
    if q in ("skip", "next", "next track", "next song", "skip song",
             "skip track", "skip this song") or find(
            r"\b(skip|next)\b.*\b(song|track|tune|music|one|this)\b", q):
        return media_control.next_track()
    if q in ("previous track", "previous song", "last song", "go back a song",
             "previous", "go back a track") or find(
            r"\b(previous|last|go back)\b.*\b(song|track|tune|one)\b", q):
        return media_control.previous_track()
    return None


def _route_volume(q, orion=None):
    arg = _after(q, "set volume to", "set the volume to", "volume to",
                 "change volume to")
    if arg is not None:
        return pc_control.set_volume(_extract_number(arg))
    if q in ("volume up", "turn up the volume", "turn the volume up",
             "louder", "increase the volume", "raise the volume"):
        return pc_control.nudge_volume(True)
    if q in ("volume down", "turn down the volume", "turn the volume down",
             "quieter", "decrease the volume", "lower the volume"):
        return pc_control.nudge_volume(False)
    return None


def _route(q, raw, orion, speak):
    """Match `q` to a handler. Returns a spoken string, "" (silent), or None."""

    # --- HUD visibility ---
    result = _route_hud(q, orion)
    if result is not None:
        return result

    # --- Group 11: scenes & routines (checked early; very specific) ---
    if q == "panic":
        return scenes.panic()
    for scene in scenes.list_scenes():
        s = scene.lower()
        if q == s or q == s.replace(" mode", "") or q == s + " mode":
            return scenes.run_scene(scene)
    arg = _after(q, "activate", "run", "start", "scene")
    if arg and arg in [s.lower() for s in scenes.list_scenes()]:
        return scenes.run_scene(arg)

    # --- Group 6: browser & app control ---
    if q in ("open work tabs", "open my work tabs", "open the work tabs"):
        return browser_control.open_work_tabs()
    if q in ("close all tabs", "close all my tabs", "close the tabs",
             "close all the tabs", "close my tabs"):
        return browser_control.close_all_tabs()
    if q in ("save this as a work tab", "save work tab", "save this tab",
             "save this work tab", "save as work tab", "save the tab"):
        return browser_control.save_work_tab()

    arg = _after(q, "google", "search google for", "search for", "search")
    if arg is not None:
        return browser_control.google_search(arg)

    arg = _after(q, "go to", "navigate to", "open the site", "open website",
                 "open site", "open the website")
    if arg is not None:
        return browser_control.go_to_site(arg)

    # --- Group 8: media (open service / playback) ---
    result = _route_media(q)
    if result is not None:
        return result

    # --- Group 7: PC control (no-argument) ---
    if q in ("lock my pc", "lock the pc", "lock my computer",
             "lock the computer", "lock pc", "lock computer", "lock"):
        return pc_control.lock_pc()
    if q in ("empty the trash", "empty trash", "empty the recycle bin",
             "empty recycle bin", "clear the trash", "clear the recycle bin"):
        return pc_control.empty_trash()
    if q in ("cancel shutdown", "abort shutdown", "stop shutdown",
             "cancel the shutdown"):
        return pc_control.cancel_shutdown()

    arg = _after(q, "shutdown in", "shut down in", "shutdown the pc in",
                 "turn off the pc in")
    if arg is not None:
        minutes = _extract_minutes(arg)
        return pc_control.shutdown_in(int(minutes) if minutes is not None
                                      else None)

    arg = _after(q, "find the file", "find file", "find a file called",
                 "search for the file", "locate the file", "find the file called")
    if arg is not None:
        return pc_control.find_file(arg)

    arg = _after(q, "open the folder", "open folder")
    if arg is not None:
        return pc_control.open_folder(arg)

    # --- Volume (Groups 7 & 8 share this) ---
    result = _route_volume(q)
    if result is not None:
        return result

    # --- Group 9: productivity ---
    arg = _after(q, "set a timer for", "set timer for", "start a timer for",
                 "timer for", "set a timer", "start a timer")
    if arg is not None:
        minutes = _extract_minutes(arg)
        label = re.sub(r"\b(for|minutes?|minute|hours?|hour|seconds?|second)\b",
                       "", arg)
        label = re.sub(r"\d+", "", label).strip()
        return productivity_tools.start_timer(minutes, label, speak)

    arg = _after(q, "remind me to", "remind me", "set a reminder to",
                 "set a reminder")
    if arg is not None:
        minutes = _minutes_until_clock(arg)
        if minutes is None:
            minutes = _extract_minutes(arg)
        task = re.split(r"\bin\b|\bat\b", arg)[0].strip()
        return productivity_tools.set_reminder(minutes, task, speak)

    if q in ("whats on my calendar", "what is on my calendar", "my calendar",
             "calendar", "calendar today", "todays calendar",
             "whats my schedule", "what is my schedule", "my schedule",
             "whats on my schedule", "whats on today"):
        return productivity_tools.calendar_summary()

    arg = _after(q, "add to my todo list", "add to my to do list",
                 "add to my to-do list", "add to the todo list", "add a todo",
                 "add a to do", "add to do", "add todo", "add to my todos")
    if arg is not None:
        return productivity_tools.add_todo(arg)
    if q in ("read my todo list", "read my to do list", "read my to-do list",
             "whats on my todo list", "whats on my to do list", "my todos",
             "my todo list", "read my todos", "what are my todos",
             "whats on my to-do list"):
        return productivity_tools.read_todos()
    if q in ("clear my todo list", "clear my to do list",
             "clear my to-do list", "empty my todo list"):
        return productivity_tools.clear_todos()

    arg = _after(q, "draft an email", "draft email", "write an email",
                 "write email", "compose an email", "compose email")
    if arg is not None:
        orion.draft_email(arg)
        return ""  # draft_email speaks its own confirmation

    # --- Group 10: smart home ---
    if q in ("turn on the lights", "lights on", "turn the lights on",
             "turn on lights", "switch on the lights"):
        return smart_home.lights_on()
    if q in ("turn off the lights", "lights off", "turn the lights off",
             "turn off lights", "switch off the lights"):
        return smart_home.lights_off()
    arg = _after(q, "set brightness to", "set the brightness to",
                 "dim the lights to", "set the lights to", "brightness to")
    if arg is not None:
        n = _extract_number(arg)
        color = next((c for c in smart_home.COLORS if c in arg), None)
        if color:
            return smart_home.lights_on(brightness=n, color=color)
        return smart_home.set_brightness(n)
    arg = _after(q, "set the temperature to", "set temperature to",
                 "set the thermostat to", "temperature to")
    if arg is not None:
        return smart_home.set_temperature(_extract_number(arg))
    if q in ("turn on the plug", "plug on", "turn on the smart plug"):
        return smart_home.switch(True)
    if q in ("turn off the plug", "plug off", "turn off the smart plug"):
        return smart_home.switch(False)

    # --- "open X" (app / media service / folder) -- checked last ---
    arg = _after(q, "open", "launch")
    if arg is not None and arg:
        media = media_control.open_media(arg)
        if media is not None:
            return media
        if "folder" in arg:
            return pc_control.open_folder(arg)
        return browser_control.open_app(arg)

    return None
