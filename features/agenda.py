"""Unified personal agenda: events + reminders + todos.

One facade. Reads from three places, presents one chronological view.

* Events    -> calendar_sync.py  (Google Calendar primary, .ics fallback)
* Reminders -> reminders.py      (already persistent, has its own scheduler)
* Todos     -> agenda.json       (no time -- the "do it sometime" pile)

Public API
----------
* list(when)            -> dict with "events", "reminders", "todos"
* spoken_summary(when)  -> short spoken-style summary, JARVIS-toned
* classify_and_add(text, when_phrase) -> (reply, ok)
* todos_add(text)
* todos_complete(text_match)
* todos_clear()
* upcoming_calendar(start, end)
"""

import datetime as _dt
import json
import os
import re
import threading

import _paths
import config


_HERE = _paths.ROOT
_PATH = os.path.join(_HERE, getattr(config, "AGENDA_FILE", "agenda.json"))
_LOCK = threading.Lock()


# --- storage ---------------------------------------------------------------

_MIGRATED = False


def _migrate_legacy_todos():
    """One-time: pull todos out of the old `todo.json` into agenda.json.

    productivity_tools.py used to maintain a bare list at `todo.json`.
    When the unified agenda took over we kept the items but never moved
    them, so the new code saw an empty list. This runs once per process,
    merges any legacy items in, and renames the old file so we don't
    re-import them on next launch."""
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True
    legacy_path = os.path.join(_HERE,
                                getattr(config, "TODO_FILE", "todo.json"))
    if not os.path.exists(legacy_path):
        return
    try:
        with open(legacy_path, "r", encoding="utf-8") as fh:
            legacy = json.load(fh)
        if not isinstance(legacy, list) or not legacy:
            return
        # Read current agenda directly (avoid recursing into _load -> migrate).
        if os.path.exists(_PATH):
            with open(_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {"todos": []}
        else:
            data = {"todos": []}
        existing = data.setdefault("todos", [])
        # Merge, preserving order, no duplicates.
        seen = {t.lower() for t in existing}
        for item in legacy:
            if isinstance(item, str) and item.lower() not in seen:
                existing.append(item)
                seen.add(item.lower())
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _PATH)
        # Rename rather than delete so the user can recover if they want.
        os.replace(legacy_path, legacy_path + ".migrated")
    except (OSError, ValueError):
        pass


def _load():
    _migrate_legacy_todos()
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {"todos": []}
    except (OSError, ValueError):
        return {"todos": []}


def _save(data):
    tmp = _PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _PATH)
        return True
    except OSError:
        return False


# --- range helpers ---------------------------------------------------------

def _range_for(when):
    """Translate 'today' / 'tomorrow' / 'this week' / 'week' / 'next week'
    into (start, end) datetimes. Default: today."""
    now = _dt.datetime.now().replace(microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0)
    w = (when or "").strip().lower()

    if w in ("", "today", "now"):
        return (today_start, today_start + _dt.timedelta(days=1))
    if w in ("tomorrow",):
        s = today_start + _dt.timedelta(days=1)
        return (s, s + _dt.timedelta(days=1))
    if w in ("this week", "week", "rest of the week"):
        return (today_start, today_start + _dt.timedelta(days=7))
    if w in ("next week",):
        s = today_start + _dt.timedelta(days=7)
        return (s, s + _dt.timedelta(days=7))
    if w in ("tonight",):
        s = now.replace(hour=18, minute=0, second=0)
        return (s, today_start + _dt.timedelta(days=1))
    return (today_start, today_start + _dt.timedelta(days=1))


# --- read ------------------------------------------------------------------

def snapshot(when="today"):
    start, end = _range_for(when)
    try:
        import calendar_sync
        events = calendar_sync.list_events(start, end)
    except Exception:
        events = []
    try:
        import reminders
        rems_all = reminders.list_pending()
    except Exception:
        rems_all = []
    # A reminder for a later day is mirrored onto Google Calendar (so it
    # reaches your phone), which means it comes back in `events` too. Drop
    # the calendar copy -- it's listed below as the reminder it actually is.
    mirrored = {r["gcal_id"] for r in rems_all if r.get("gcal_id")}
    if mirrored:
        events = [e for e in events if e.get("id") not in mirrored]
    rems = []
    for r in rems_all:
        try:
            dt = _dt.datetime.fromisoformat(r["fires_at"])
        except (KeyError, ValueError):
            continue
        if start <= dt < end:
            rems.append({**r, "_dt": dt})
    with _LOCK:
        todos = _load().get("todos", [])
    return {"events": events, "reminders": rems, "todos": todos,
            "start": start, "end": end}


def spoken_summary(when="today"):
    """Return a short JARVIS-style spoken summary."""
    data = snapshot(when)
    events = data["events"]
    rems = data["reminders"]
    todos = data["todos"]
    label = (when or "today").strip().lower() or "today"

    if not events and not rems and not todos:
        return f"Nothing on for {label}, Sir."

    parts = []
    timed = []  # (dt, kind, text)
    for ev in events:
        dt = ev.get("start")
        if dt:
            timed.append((dt, "event", _format_event(ev)))
    for r in rems:
        timed.append((r["_dt"], "reminder",
                      f"reminder to {r['text']}"))
    timed.sort(key=lambda t: t[0])
    if timed:
        parts.append(_phrase_timed(timed))
    if todos:
        n = len(todos)
        word = "todo" if n == 1 else "todos"
        if len(todos) <= 3:
            parts.append(f"todos: {', '.join(todos)}")
        else:
            parts.append(f"plus {n} {word} pending")
    return f"For {label}, Sir: " + "; ".join(parts) + "."


def _format_event(ev):
    title = ev["title"]
    if ev.get("all_day"):
        return f"all-day {title}"
    dt = ev["start"]
    return f"{title} at {_clock(dt)}"


def _phrase_timed(timed):
    chunks = []
    for dt, _kind, text in timed[:8]:
        chunks.append(f"{_clock(dt)} {text}")
    return ", ".join(chunks)


def _clock(dt):
    from clock_words import spoken_time
    return spoken_time(dt)


# --- classify & add --------------------------------------------------------

# Words that strongly suggest "calendar event" rather than reminder.
_EVENT_HINTS = (
    "appointment", "meeting", "lunch", "dinner", "breakfast", "call with",
    "interview", "flight", "haircut", "doctor", "dentist", "class",
    "lecture", "session", "party", "wedding", "match", "game", "concert",
    "show", "reservation", "booking", "checkup", "vacation", "trip",
    "workout", "gym session", "training", "practice", "lesson",
    "rehearsal", "exam", "test ", "presentation", "standup", "stand-up",
    "1:1", "one on one", "sync with", "catchup", "catch up",
)
# Words that suggest "reminder" (a ping, not an event).
_REMINDER_HINTS = (
    "remind me", "remember to", "don't forget", "make sure", "ping me",
    "dont forget",
)
# Words that suggest a todo (no-time intention) even when ambiguous.
_TODO_HINTS = (
    "i need to", "i should", "i have to", "i want to", "i gotta",
    "got to", "must ", "need to ",
)
# Recurring phrasings -- always handled as reminders (calendar recurring
# events are intentionally out of v1).
_RECURRING_HINTS = ("every ", "each ", "daily", "weekly")


def classify(text, when_phrase):
    """Pick 'event' | 'reminder' | 'todo' from the user's phrasing.

    Order matters:
    1. Recurring phrase -> always reminder (calendar recurrence is v2).
    2. "with <name>" (a person reference) -> event, because that almost
       always means a real meeting/appointment.
    3. Explicit 'remind me' style -> reminder.
    4. Event vocabulary -> event.
    5. No time phrase at all -> todo (or treat 'I need to' / 'I should'
       as todo even if a time word slipped in, since those are usually
       aspirational not scheduled).
    6. Otherwise -> reminder (default).
    """
    t = (text or "").lower()
    w = (when_phrase or "").strip().lower()

    # Recurring beats everything else.
    if any(h in t for h in _RECURRING_HINTS) \
            or any(h in w for h in _RECURRING_HINTS):
        return "reminder"

    # No time at all -- todo, unless event vocab is so strong that the
    # user clearly means a calendar entry they forgot to time.
    if not w:
        return "todo"

    # "with <name>" is a strong meeting signal ("coffee with Sam at 3").
    if re.search(r"\bwith\s+[A-Za-z][A-Za-z0-9'\-]+", t):
        return "event"

    if any(h in t for h in _REMINDER_HINTS):
        return "reminder"

    if any(h in t for h in _EVENT_HINTS):
        return "event"

    # Aspirational "I need to" / "I should" with a vague time word -> todo.
    if any(h in t for h in _TODO_HINTS):
        return "todo"

    return "reminder"


def classify_and_add(text, when_phrase):
    """Route an agenda item to the right backend. Returns (reply, ok)."""
    kind = classify(text, when_phrase)
    if kind == "todo":
        return (todos_add(text), True)
    if kind == "event":
        return _add_event(text, when_phrase)
    # reminder
    import reminders
    return reminders.add(text, when_phrase)


def _add_event(title, when_phrase):
    """Convert when_phrase to (start, end) and create on Google Calendar."""
    import reminders as r
    start, _recur = r.parse_when(when_phrase)
    if start is None:
        start, _ = r._llm_parse(when_phrase)
    if start is None:
        return (f"I couldn't work out when '{when_phrase}' is, Sir.", False)
    try:
        import calendar_sync
    except Exception:
        return ("Calendar isn't available, Sir.", False)
    if not calendar_sync.google_available():
        return ("Google Calendar isn't connected yet, Sir. Run "
                "setup_calendar.py once and try again.", False)
    ev, ok = calendar_sync.add_event(title, start)
    if not ok:
        return ("I couldn't reach Google Calendar, Sir.", False)
    return (f"Added {title} to your calendar at {_clock(start)}.", True)


# --- todos -----------------------------------------------------------------

def todos_add(text):
    text = (text or "").strip()
    if not text:
        return "What should I add to your list, Sir?"
    with _LOCK:
        data = _load()
        data.setdefault("todos", []).append(text)
        _save(data)
    return f"Added {text} to your list, Sir."


def todos_list():
    with _LOCK:
        return _load().get("todos", [])


def todos_complete(needle):
    needle = (needle or "").strip().lower()
    if not needle:
        return "Which one should I complete, Sir?"
    with _LOCK:
        data = _load()
        todos = data.get("todos", [])
        matches = [t for t in todos if needle in t.lower()]
        if not matches:
            return f"No todo matching '{needle}', Sir."
        kept = [t for t in todos if t not in matches]
        data["todos"] = kept
        _save(data)
    if len(matches) == 1:
        return f"Done -- removed {matches[0]} from your list."
    return f"Removed {len(matches)} matching todos, Sir."


def todos_clear():
    with _LOCK:
        data = _load()
        n = len(data.get("todos", []))
        data["todos"] = []
        _save(data)
    return f"Cleared {n} todo{'s' if n != 1 else ''}, Sir."


# --- calendar passthroughs -------------------------------------------------

def upcoming_calendar(start=None, end=None):
    try:
        import calendar_sync
        return calendar_sync.list_events(start, end)
    except Exception:
        return []


def reschedule_relative(text_match, delta_phrase):
    """Shift an event by a relative delta -- 'push the dentist an hour
    later', 'move the meeting 30 minutes earlier'. Returns spoken reply."""
    try:
        import calendar_sync
        import reminders as r
    except Exception:
        return "Calendar isn't available, Sir."
    if not calendar_sync.google_available():
        return "Google Calendar isn't connected yet, Sir."
    ev = calendar_sync.find_event(text_match)
    if ev is None:
        return f"I couldn't find an event matching '{text_match}', Sir."
    delta = r.parse_delta(delta_phrase)
    if delta is None:
        return f"I couldn't parse '{delta_phrase}' as a shift, Sir."
    if ev.get("start") is None:
        return "That event has no time I can shift, Sir."
    new_start = ev["start"] + delta
    new_end = (ev["end"] + delta) if ev.get("end") else None
    ok = calendar_sync.update_event(ev["id"], start=new_start, end=new_end)
    if not ok:
        return "I couldn't update the event, Sir."
    direction = "later" if delta.total_seconds() >= 0 else "earlier"
    return (f"Moved {ev['title']} to {_clock(new_start)}, "
            f"{abs(delta.total_seconds()) / 60:.0f} minutes {direction}.")


def reschedule_event(text_match, new_when):
    """Move a calendar event to a new time. Returns spoken reply."""
    try:
        import calendar_sync
        import reminders as r
    except Exception:
        return "Calendar isn't available, Sir."
    if not calendar_sync.google_available():
        return ("Google Calendar isn't connected yet, Sir.")
    ev = calendar_sync.find_event(text_match)
    if ev is None:
        return f"I couldn't find an event matching '{text_match}', Sir."
    new_start, _ = r.parse_when(new_when)
    if new_start is None:
        new_start, _ = r._llm_parse(new_when)
    if new_start is None:
        return f"I couldn't parse '{new_when}' as a time, Sir."
    # Preserve duration if we know it.
    duration = None
    if ev.get("end") and ev.get("start"):
        duration = ev["end"] - ev["start"]
    new_end = (new_start + duration) if duration else None
    ok = calendar_sync.update_event(ev["id"], start=new_start, end=new_end)
    if not ok:
        return "I couldn't update the event, Sir."
    return f"Moved {ev['title']} to {_clock(new_start)}, Sir."


def cancel_event(text_match):
    try:
        import calendar_sync
    except Exception:
        return "Calendar isn't available, Sir."
    ev = calendar_sync.find_event(text_match)
    if ev is None:
        return f"I couldn't find an event matching '{text_match}', Sir."
    ok = calendar_sync.delete_event(ev["id"])
    if not ok:
        return "I couldn't remove the event, Sir."
    return f"Removed {ev['title']} from your calendar, Sir."
