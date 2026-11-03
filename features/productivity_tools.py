"""Orion's productivity tools (Group 9).

Background timers, Windows toast reminders, a spoken calendar summary, and a
local JSON to-do list. Email drafting lives in orion.py because it needs the
AI to write the body.
"""

import json
import os
import threading

import _paths
import briefing
import config


def _orion_path(name):
    return os.path.join(_paths.ROOT, name)


# --- Timers -------------------------------------------------------------

def start_timer(minutes, label, speak):
    """Start a background timer that speaks via `speak` when it fires."""
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        return "How many minutes should the timer run, Sir?"
    if minutes <= 0:
        return "I need a timer length greater than zero."

    label = (label or "").strip()
    pretty = f"{int(minutes)}" if minutes == int(minutes) else f"{minutes}"

    def _fire():
        name = f"{label} " if label else ""
        speak(f"Sir, your {name}{pretty} minute timer is done.")

    timer = threading.Timer(minutes * 60, _fire)
    timer.daemon = True
    timer.start()
    return f"Timer set for {pretty} minutes."


# --- Reminders ----------------------------------------------------------

def set_reminder(minutes=None, text="", speak=None, when=""):
    """Persistent reminder. Pass either `minutes` (legacy) or `when` (a natural
    phrase like 'at 3pm', 'tomorrow morning', 'every weekday at 8am').

    Routes to `reminders.add()` so the reminder survives restarts and can be
    listed / cancelled / snoozed later.
    """
    import reminders
    phrase = (when or "").strip()
    if not phrase:
        try:
            n = float(minutes)
            if n <= 0:
                return "I need a reminder time in the future."
            phrase = f"in {int(n) if n == int(n) else n} minutes"
        except (TypeError, ValueError):
            return "When should I remind you, Sir?"
    reply, _ok = reminders.add(text, phrase)
    return reply


# --- Calendar -----------------------------------------------------------

def calendar_summary():
    """Return a spoken summary of today's calendar events."""
    if not config.CALENDAR_ICS:
        return ("No calendar is set up. Add a calendar file path to "
                "CALENDAR_ICS in the config.")
    events = briefing.get_calendar()
    if not events:
        return "You have nothing scheduled today, Sir."
    parts = []
    for time_str, summary in events:
        parts.append(f"{summary} at {time_str}" if time_str else summary)
    return f"Today you have: {'; '.join(parts)}."


# --- To-do list ---------------------------------------------------------

def _load_todos():
    try:
        with open(_orion_path(config.TODO_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_todos(items):
    try:
        with open(_orion_path(config.TODO_FILE), "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)
        return True
    except OSError:
        return False


def add_todo(item):
    """Append an item to the to-do list."""
    item = (item or "").strip()
    if not item:
        return "What should I add to your to-do list, Sir?"
    items = _load_todos()
    items.append(item)
    if _save_todos(items):
        return f"Added {item} to your to-do list."
    return "I couldn't save the to-do list."


def read_todos():
    """Return a spoken read-back of the to-do list."""
    items = _load_todos()
    if not items:
        return "Your to-do list is empty, Sir."
    listed = "; ".join(f"{i}, {t}" for i, t in enumerate(items, 1))
    return f"You have {len(items)} items: {listed}."


def clear_todos():
    """Empty the to-do list."""
    if _save_todos([]):
        return "Cleared your to-do list, Sir."
    return "I couldn't clear the to-do list."
