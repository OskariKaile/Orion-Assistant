"""Persistent reminders with absolute times, listing, cancel, and snooze.

Replaces the old `threading.Timer` reminders that died on restart. Reminders
live in `reminders.json` as ISO timestamps. A single background daemon thread
wakes every 15s and fires anything overdue.

Features
--------
- Absolute time parsing ("at 3pm", "tomorrow morning", "in 2 hours").
- Recurring reminders ("every weekday at 8am").
- Persistence across restarts -- on boot, anything missed while Orion was
  off is delivered as a single "you missed N reminders" summary.
- Snooze the most-recently-fired reminder.
- Owner-gated list / cancel / clear (callers enforce, gate is here too).

- Google Calendar mirror for reminders landing on a LATER day, so they show
  up on your phone (see `_should_mirror`).

File shape (`reminders.json`)
-----------------------------
    [
      {"id": "r_a3f2",
       "fires_at": "2026-05-22T15:00:00",
       "text": "call mom",
       "created": "2026-05-22T11:14:00",
       "recur": null,     # or {"kind": "daily"} / {"kind": "weekdays"}
                          # / {"kind": "weekly", "weekday": 0}  (0=Mon)
                          # / {"kind": "interval", "minutes": 30}
       "gcal_id": "..."}  # set only when mirrored to Google Calendar
    ]
"""

import datetime as _dt
import json
import os
import re
import secrets
import threading
import time

import _paths
import config

try:
    import presence
except Exception:  # presence detection is optional
    presence = None


_HERE = _paths.ROOT
_PATH = os.path.join(_HERE, getattr(config, "REMINDERS_FILE", "reminders.json"))
_LOCK = threading.Lock()
_LAST_FIRED = None  # most-recent reminder dict, used by snooze
_SPEAK = None       # injected by start()
_THREAD = None

# Reminders that came due while nobody was at the computer. Each entry is
# (item, monotonic time it was held) so a hold can eventually time out. See
# _deliver_or_hold.
_HELD = []
_PRESENT_SINCE = None  # monotonic time presence last stopped being "away"


# --- storage ---------------------------------------------------------------

def _load():
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(items):
    tmp = _PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)
        os.replace(tmp, _PATH)
        return True
    except OSError:
        return False


def _new_id():
    return "r_" + secrets.token_hex(2)


def _now():
    return _dt.datetime.now().replace(microsecond=0)


# --- parsing ---------------------------------------------------------------

_DAY_WORDS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_PART_OF_DAY = {
    "morning": 8, "noon": 12, "afternoon": 14,
    "evening": 18, "tonight": 21, "night": 21, "midnight": 0,
}


def _next_weekday(base, target):
    """Date of the next `target` weekday strictly after base.date() (or today
    if base time is in the past on the same weekday and target==today)."""
    days = (target - base.weekday()) % 7
    if days == 0:
        days = 7
    return (base + _dt.timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _parse_clock(text, smart_pm=True):
    """Parse '3', '3pm', '3:30', '15:00', '3:30 pm' -> (hour, minute) or None.

    Smart-PM default (on by default): a bare hour 1-7 with no am/pm suffix
    is interpreted as PM, because human appointments at 1am-7am are rare.
    8-11 stay as AM. 12+ is unambiguous. This is the JARVIS-y "do what I
    obviously meant" default. Callers that need a literal parse can pass
    smart_pm=False.
    """
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", text.strip(),
                 re.IGNORECASE)
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower()
    if suffix == "pm" and h < 12:
        h += 12
    elif suffix == "am" and h == 12:
        h = 0
    elif not suffix and smart_pm and 1 <= h <= 7:
        h += 12  # 3 -> 15, 5 -> 17, etc.
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return (h, mn)


def parse_delta(phrase):
    """Parse a relative time delta -- 'an hour later', '30 minutes earlier',
    'two days later', 'half an hour back'. Returns a timedelta or None.

    Used by reschedule_relative: move an event from its current time by
    this delta. Positive = later, negative = earlier.
    """
    if not phrase:
        return None
    p = phrase.strip().lower()
    p = re.sub(r"\s+", " ", p)
    # Strip leading "by " ("by an hour later").
    p = re.sub(r"^by\s+", "", p)
    # Direction.
    direction = 1
    if re.search(r"\b(earlier|back|sooner|forward|ahead)\b", p):
        if re.search(r"\b(earlier|back|sooner)\b", p):
            direction = -1
    # Word-number swaps so "an hour" / "half an hour" parse.
    # Order matters: "half an hour" must run BEFORE "an hour" -> "1 hour",
    # otherwise the "half" prefix is left dangling.
    p = p.replace("half an hour", "30 minutes").replace("half hour",
                                                          "30 minutes")
    p = p.replace("an hour", "1 hour").replace("a hour", "1 hour")
    p = p.replace("a day", "1 day").replace("a minute", "1 minute")
    word_nums = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    for word, num in word_nums.items():
        p = re.sub(rf"\b{word}\b", str(num), p)
    # Now look for "<n> <unit>".
    m = re.search(r"(\d+(?:\.\d+)?)\s*(minute|min|hour|hr|day)s?", p)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)
    if unit.startswith("min"):
        td = _dt.timedelta(minutes=n)
    elif unit.startswith(("hour", "hr")):
        td = _dt.timedelta(hours=n)
    else:
        td = _dt.timedelta(days=n)
    return td * direction


def parse_when(phrase, now=None):
    """Parse a natural time phrase into an absolute datetime + recurrence.

    Returns (datetime, recur_dict_or_None) or (None, None) if it can't be
    parsed locally. Caller may then fall back to a Gemini conversion.
    """
    if not phrase:
        return (None, None)
    now = now or _now()
    p = phrase.strip().lower()
    p = re.sub(r"\s+", " ", p)
    # Normalise common STT/spelling variants of "o'clock" so they don't
    # poison clock parsing ("9 o'clock", "9 oclock", "9 a clock", "9 aclokc").
    p = re.sub(r"\b(o\s*'?\s*clock|oclock|a\s*clock|aclo\w*)\b", "", p)
    p = re.sub(r"\s+", " ", p).strip()
    # Strip leading filler verbs that creep in from STT/Gemini extraction
    # ("on today at 9", "for tomorrow morning").
    p = re.sub(r"^(on|for)\s+", "", p)

    # Recurrence triggers ---------------------------------------------------
    recur = None
    if p.startswith("every "):
        rest = p[len("every "):].strip()
        # "every weekday at 8am"
        m = re.match(r"^weekday(?:s)?\s+(?:at\s+)?(.+)$", rest)
        if m:
            clk = _parse_clock(m.group(1))
            if clk:
                target = _resolve_today_clock(now, clk)
                # If today is a weekend or time already passed, push forward.
                while target <= now or target.weekday() >= 5:
                    target += _dt.timedelta(days=1)
                return (target, {"kind": "weekdays"})
        # "every day at 9"
        m = re.match(r"^day\s+(?:at\s+)?(.+)$", rest)
        if m:
            clk = _parse_clock(m.group(1))
            if clk:
                target = _resolve_today_clock(now, clk)
                if target <= now:
                    target += _dt.timedelta(days=1)
                return (target, {"kind": "daily"})
        # "every monday at 9"
        for word, wd in _DAY_WORDS.items():
            m = re.match(rf"^{word}\s+(?:at\s+)?(.+)$", rest)
            if m:
                clk = _parse_clock(m.group(1))
                if clk:
                    base = _next_weekday(now, wd)
                    target = base.replace(hour=clk[0], minute=clk[1])
                    return (target, {"kind": "weekly", "weekday": wd})
        # "every 30 minutes" / "every 2 hours"
        m = re.match(r"^(\d+)\s*(minute|min|hour|hr)s?$", rest)
        if m:
            n = int(m.group(1))
            mins = n if m.group(2).startswith(("min",)) else n * 60
            return (now + _dt.timedelta(minutes=mins),
                    {"kind": "interval", "minutes": mins})

    # "in N minutes/hours/seconds/days" ------------------------------------
    m = re.match(r"^in\s+(\d+(?:\.\d+)?)\s*(second|sec|minute|min|hour|hr|day)s?$", p)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        if unit.startswith("sec"):
            delta = _dt.timedelta(seconds=n)
        elif unit.startswith(("min",)):
            delta = _dt.timedelta(minutes=n)
        elif unit.startswith(("hour", "hr")):
            delta = _dt.timedelta(hours=n)
        else:
            delta = _dt.timedelta(days=n)
        return (now + delta, None)

    # "tomorrow morning" / "tonight" / "tomorrow at 8" ---------------------
    if p in _PART_OF_DAY:
        h = _PART_OF_DAY[p]
        target = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if target <= now:
            target += _dt.timedelta(days=1)
        return (target, None)

    # Bare "today" is too ambiguous -- refuse so caller asks for a time.
    if p == "today":
        return (None, None)

    m = re.match(r"^(today|tonight|tomorrow|tomorrow night|tomorrow morning|"
                 r"tomorrow evening|tomorrow afternoon|today morning|"
                 r"today evening|today afternoon)(?:\s+at\s+(.+))?$", p)
    if m:
        when_word = m.group(1)
        clk_text = m.group(2)
        is_today = when_word in ("today", "tonight", "today morning",
                                  "today evening", "today afternoon")
        # Words that imply PM -- so "tonight at 9" / "tomorrow night at 11"
        # resolve to 9PM / 11PM instead of AM.
        is_evening_word = when_word in ("tonight", "today evening",
                                         "today afternoon",
                                         "tomorrow night",
                                         "tomorrow evening",
                                         "tomorrow afternoon")
        base_day = now if is_today else now + _dt.timedelta(days=1)
        if clk_text:
            clk = _parse_clock(clk_text)
            if clk is None:
                return (None, None)
            h, mn = clk
            # If the user said an evening/night word and gave a bare hour
            # under 12 with no am/pm, bump it to PM.
            if is_evening_word and h < 12 and "am" not in clk_text.lower() \
                    and "pm" not in clk_text.lower():
                h += 12
        else:
            stripped = when_word.replace("tomorrow ", "").replace(
                "today ", "").strip()
            if when_word in ("tomorrow", "today"):
                key = "morning"
            else:
                key = stripped or "morning"
            h = _PART_OF_DAY.get(key, 8)
            mn = 0
        target = base_day.replace(hour=h, minute=mn, second=0, microsecond=0)
        # If the user explicitly said "today" and the time has passed,
        # prefer flipping AM->PM (so "today at 9" at 11:33 -> 9PM today)
        # over silently jumping to tomorrow.
        if is_today and target <= now and clk_text:
            if h < 12 and "am" not in clk_text.lower() \
                    and "pm" not in clk_text.lower():
                target = target.replace(hour=h + 12)
        # Only push to tomorrow as a last resort, and only if NOT explicitly
        # "today" (for "today", past times stay flagged in-the-past so the
        # caller can warn the user instead of silently shifting a day).
        if target <= now and not is_today:
            target += _dt.timedelta(days=1)
        return (target, None)

    # "at 3pm" / "at 15:00" ------------------------------------------------
    m = re.match(r"^at\s+(.+)$", p)
    if m:
        clk = _parse_clock(m.group(1))
        if clk:
            return (_resolve_today_clock(now, clk, push_if_past=True), None)

    # Reverse order: "3pm friday", "3 on friday", "3pm on monday".
    for word, wd in _DAY_WORDS.items():
        m = re.match(rf"^(.+?)\s+(?:on\s+)?(?:next\s+)?{word}$", p)
        if m:
            clk = _parse_clock(m.group(1))
            if clk:
                base = _next_weekday(now, wd)
                return (base.replace(hour=clk[0], minute=clk[1]), None)

    # Bare clock ("9", "3pm", "15:00") -- treat like "at <clock>".
    clk = _parse_clock(p)
    if clk:
        return (_resolve_today_clock(now, clk, push_if_past=True), None)

    # "<weekday> [morning|at 9]" ------------------------------------------
    for word, wd in _DAY_WORDS.items():
        m = re.match(rf"^(?:next\s+)?{word}(?:\s+(.+))?$", p)
        if m:
            tail = (m.group(1) or "").strip()
            base = _next_weekday(now, wd)
            if not tail:
                return (base.replace(hour=8), None)
            if tail in _PART_OF_DAY:
                return (base.replace(hour=_PART_OF_DAY[tail]), None)
            m2 = re.match(r"^at\s+(.+)$", tail)
            if m2:
                clk = _parse_clock(m2.group(1))
                if clk:
                    return (base.replace(hour=clk[0], minute=clk[1]), None)

    return (None, None)


def _resolve_today_clock(now, clk, push_if_past=True):
    target = now.replace(hour=clk[0], minute=clk[1], second=0, microsecond=0)
    if push_if_past and target <= now:
        target += _dt.timedelta(days=1)
    return target


def _llm_parse(phrase, now=None):
    """Last-resort: ask the model to convert weird phrasings to an ISO timestamp.
    Returns (datetime, None) or (None, None). Recurrence not inferred here --
    user has to phrase recurring with the 'every ...' pattern."""
    try:
        import llm_router
    except Exception:
        return (None, None)
    now = now or _now()
    prompt = (
        f"Current local time is {now.isoformat()}. The user said: "
        f"\"{phrase}\". If this refers to a single future moment, reply "
        f"with ONLY that moment as an ISO 8601 local datetime "
        f"(YYYY-MM-DDTHH:MM:SS), nothing else. If it isn't a time, reply NONE."
    )
    try:
        reply = (llm_router.ask_once(prompt) or "").strip()
    except Exception:
        return (None, None)
    if not reply or reply.upper().startswith("NONE"):
        return (None, None)
    # Take the first ISO-looking substring.
    m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", reply)
    if not m:
        return (None, None)
    try:
        dt = _dt.datetime.fromisoformat(m.group(0))
    except ValueError:
        return (None, None)
    return (dt, None)


# --- public API ------------------------------------------------------------

_TIME_WORDS = {
    "today", "tomorrow", "tonight", "morning", "afternoon", "evening",
    "night", "noon", "midnight", "now", "later", "soon",
} | set(_DAY_WORDS.keys())


def _clean_task_text(text):
    """Strip leading 'to '/'that '/'about ' fillers and trailing time hints."""
    if not text:
        return ""
    t = text.strip().rstrip(".!?")
    # Drop leading "to "/"that "/"about " ("to watch a starship flight" ->
    # "watch a starship flight").
    t = re.sub(r"^(to|that|about)\s+", "", t, flags=re.IGNORECASE)
    return t.strip()


def _looks_like_time_only(text):
    """True if `text` is just a time word/phrase with no actual subject --
    catches Gemini mis-extractions like task='today' / task='at 3pm'."""
    if not text:
        return True
    t = text.strip().lower().rstrip(".!?")
    if t in _TIME_WORDS:
        return True
    # "at 3pm", "in 10 minutes", "9 o'clock"
    if re.match(r"^(at|in|on|by)\s+", t):
        return True
    if re.match(r"^\d{1,2}(:\d{2})?\s*(am|pm)?$", t):
        return True
    return False


# --- Google Calendar mirror ------------------------------------------------
# Reminders live here, not on Google Calendar. But a reminder for a LATER DAY
# is one you'd want to see on your phone, so it also gets written to the
# calendar. Same-day reminders ("in 20 minutes", "at 5pm") stay local -- they
# fire long before you'd think to look at a calendar, and mirroring them would
# bury the calendar in entries that are already stale by the time you glance.
#
# agenda.snapshot() filters the mirrored copies back out by `gcal_id`, so a
# reminder is never listed twice in "what's on for tomorrow".

# How long the mirrored calendar entry blocks out. A reminder is a moment,
# not a meeting, so keep it short.
_MIRROR_MINUTES = 15


def _should_mirror(fires_at, recur):
    """True if this reminder belongs on Google Calendar too.

    Recurring reminders are skipped: calendar_sync.add_event() writes a
    single fixed event with no RRULE, so mirroring "every weekday at 8am"
    would put one misleading entry on the calendar instead of the series.
    They stay local and fire normally.
    """
    if not getattr(config, "REMINDER_CALENDAR_SYNC", True):
        return False
    if recur:
        return False
    return fires_at.date() > _now().date()


def _mirror_add(item_id, text, fires_at):
    """Create the calendar event, then staple its id onto the stored reminder.

    Runs on a worker thread -- the Google round-trip must not sit in the
    voice path between "remind me to..." and Orion's confirmation.
    """
    try:
        import calendar_sync
        if not calendar_sync.google_available():
            return
        ev, ok = calendar_sync.add_event(
            f"Reminder: {text}", fires_at,
            end=fires_at + _dt.timedelta(minutes=_MIRROR_MINUTES))
    except Exception:
        return
    if not ok or not ev or not ev.get("id"):
        return
    with _LOCK:
        items = _load()
        for stored in items:
            if stored.get("id") == item_id:
                stored["gcal_id"] = ev["id"]
                _save(items)
                break


def _mirror_delete(items):
    """Best-effort removal of the calendar copies of `items`."""
    ids = [r.get("gcal_id") for r in items if r.get("gcal_id")]
    if not ids:
        return

    def work():
        try:
            import calendar_sync
        except Exception:
            return
        for gid in ids:
            try:
                calendar_sync.delete_event(gid)
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()


def backfill_calendar():
    """Mirror already-stored reminders that predate the calendar sync.

    Reminders added before REMINDER_CALENDAR_SYNC existed have no `gcal_id`,
    so they're stranded on this PC. This walks the pending list and mirrors
    the ones that would be mirrored today anyway -- later day, not recurring,
    not already mirrored. Safe to run repeatedly: anything with a `gcal_id`
    is skipped, so it can't create duplicates.

    Runs synchronously (unlike add(), which mirrors on a worker thread) --
    it's a one-off migration, not part of the voice path. Returns a
    spoken-style summary.
    """
    with _LOCK:
        items = _load()

    pending = []
    for item in items:
        if item.get("gcal_id"):
            continue
        try:
            fires_at = _dt.datetime.fromisoformat(item["fires_at"])
        except (KeyError, ValueError):
            continue
        if _should_mirror(fires_at, item.get("recur")):
            pending.append((item["id"], item["text"], fires_at))

    if not pending:
        return "No reminders needed adding to your calendar, Sir."

    for item_id, text, fires_at in pending:
        _mirror_add(item_id, text, fires_at)

    # _mirror_add only writes gcal_id when Google actually accepted it, so
    # re-reading the file tells us what really landed.
    with _LOCK:
        stored = {i.get("id"): i for i in _load()}
    done = [t for t in pending if stored.get(t[0], {}).get("gcal_id")]
    n, total = len(done), len(pending)
    if n == 0:
        return (f"I couldn't reach Google Calendar, Sir -- {total} "
                f"reminder{'s' if total != 1 else ''} still local only.")
    word = "reminder" if n == 1 else "reminders"
    if n < total:
        return (f"Added {n} of {total} {word} to your calendar, Sir. "
                "The rest didn't go through.")
    return f"Added {n} {word} to your calendar, Sir."


def add(text, when_phrase):
    """Add a reminder. Returns (spoken_confirmation, ok_bool)."""
    text = _clean_task_text(text)
    # Catch the Gemini mis-extraction pattern where the time word ends up
    # in `task` (and the real subject is lost). Refuse rather than guess --
    # silently swapping fields produces nonsense reminders.
    if not text or _looks_like_time_only(text):
        return ("I caught the time but not what to remind you about, Sir. "
                "Try 'remind me to <thing> at <time>'.", False)
    fires_at, recur = parse_when(when_phrase)
    if fires_at is None:
        fires_at, recur = _llm_parse(when_phrase)
    if fires_at is None:
        return (f"I couldn't work out when '{when_phrase}' is, Sir.", False)
    if fires_at <= _now() and not recur:
        return ("That time is already in the past, Sir.", False)

    item = {
        "id": _new_id(),
        "fires_at": fires_at.isoformat(timespec="seconds"),
        "text": text,
        "created": _now().isoformat(timespec="seconds"),
        "recur": recur,
    }
    with _LOCK:
        items = _load()
        items.append(item)
        _save(items)
    if _should_mirror(fires_at, recur):
        threading.Thread(target=_mirror_add,
                         args=(item["id"], text, fires_at),
                         daemon=True).start()
    return (f"I'll remind you to {text} {_pretty_when(fires_at, recur)}.", True)


def list_pending():
    """Return the current pending reminders (sorted soonest first)."""
    with _LOCK:
        items = _load()
    items.sort(key=lambda r: r.get("fires_at", ""))
    return items


def cancel_matching(text, rid=""):
    """Cancel a pending reminder. Returns the spoken reply.

    `rid` is an exact reminder id and wins outright when given -- that is
    the unambiguous path, and the one the model takes when the pending list
    (ids included) is in its prompt. `text` is the spoken fallback and
    fuzzy-matches on the reminder's own words.
    """
    wanted = (rid or "").strip().lower()
    if wanted:
        with _LOCK:
            items = _load()
            matches = [r for r in items if r["id"].lower() == wanted]
            if not matches:
                return "That reminder is already gone, Sir."
            _save([r for r in items if r["id"].lower() != wanted])
        _mirror_delete(matches)
        return f"Cancelled the reminder to {matches[0]['text']}, Sir."

    needle = (text or "").strip().lower()
    if not needle:
        return "Which reminder should I cancel, Sir?"
    with _LOCK:
        items = _load()
        matches = [r for r in items
                   if needle in r["text"].lower() or needle == r["id"].lower()]
        if not matches:
            return f"No reminder matching '{text}', Sir."
        # A needle this short matched on sheer chance, not intent -- "a"
        # would take out everything. Make the user (or the model) be
        # specific rather than silently deleting the lot.
        if len(matches) > 1 and len(needle) < 4:
            return (f"'{text}' matches {len(matches)} reminders, Sir. "
                    "Which one did you mean?")
        kept = [r for r in items if r not in matches]
        _save(kept)
    _mirror_delete(matches)
    if len(matches) == 1:
        return f"Cancelled the reminder to {matches[0]['text']}, Sir."
    return f"Cancelled {len(matches)} matching reminders, Sir."


def clear_all():
    with _LOCK:
        items = _load()
        n = len(items)
        _save([])
    _mirror_delete(items)
    return f"Cleared {n} reminder{'s' if n != 1 else ''}, Sir."


def snooze(minutes=10):
    """Snooze the most-recently-fired reminder by `minutes`."""
    global _LAST_FIRED
    if _LAST_FIRED is None:
        return "Nothing to snooze, Sir."
    fires_at = _now() + _dt.timedelta(minutes=int(minutes))
    item = {
        "id": _new_id(),
        "fires_at": fires_at.isoformat(timespec="seconds"),
        "text": _LAST_FIRED["text"],
        "created": _now().isoformat(timespec="seconds"),
        "recur": None,
    }
    with _LOCK:
        items = _load()
        items.append(item)
        _save(items)
    return f"Snoozed for {int(minutes)} minutes, Sir."


def spoken_list():
    items = list_pending()
    if not items:
        return "Nothing pending, Sir."
    parts = []
    for r in items[:8]:
        try:
            dt = _dt.datetime.fromisoformat(r["fires_at"])
        except ValueError:
            continue
        parts.append(f"{r['text']} {_pretty_when(dt, r.get('recur'))}")
    n = len(items)
    word = "reminder" if n == 1 else "reminders"
    return f"{n} {word}, Sir: " + "; ".join(parts) + "."


# --- formatting helpers ----------------------------------------------------

def _pretty_when(dt, recur=None):
    now = _now()
    delta = dt - now
    if recur:
        kind = recur.get("kind")
        if kind == "daily":
            return f"every day at {_clock_words(dt)}"
        if kind == "weekdays":
            return f"every weekday at {_clock_words(dt)}"
        if kind == "weekly":
            day_name = list(_DAY_WORDS)[recur.get("weekday", 0) * 2]
            return f"every {day_name} at {_clock_words(dt)}"
        if kind == "interval":
            return f"every {recur.get('minutes')} minutes"
    if delta.total_seconds() < 60:
        return "now"
    if delta.total_seconds() < 3600:
        mins = max(1, int(delta.total_seconds() // 60))
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    if dt.date() == now.date():
        return f"at {_clock_words(dt)}"
    if dt.date() == (now + _dt.timedelta(days=1)).date():
        return f"tomorrow at {_clock_words(dt)}"
    if delta.days < 7:
        day_name = dt.strftime("%A")
        return f"on {day_name} at {_clock_words(dt)}"
    return f"on {dt.strftime('%b %d')} at {_clock_words(dt)}"


def _clock_words(dt):
    from clock_words import spoken_time
    return spoken_time(dt)


# --- scheduler -------------------------------------------------------------

def _fire(item):
    """Speak + toast a reminder. Stores it as the snooze candidate."""
    global _LAST_FIRED
    _LAST_FIRED = item
    text = item["text"]
    try:
        from plyer import notification
        notification.notify(title="Orion Reminder", message=text,
                             app_name="Orion", timeout=20)
    except Exception:
        pass
    if _SPEAK is not None:
        try:
            _SPEAK(f"Sir, reminder: {text}.")
        except Exception:
            pass


def _advance_recurring(item, now):
    """Return next fires_at ISO for a recurring item, or None to drop it."""
    recur = item.get("recur") or {}
    kind = recur.get("kind")
    try:
        dt = _dt.datetime.fromisoformat(item["fires_at"])
    except ValueError:
        return None
    if kind == "daily":
        dt = dt + _dt.timedelta(days=1)
    elif kind == "weekdays":
        dt = dt + _dt.timedelta(days=1)
        while dt.weekday() >= 5:
            dt += _dt.timedelta(days=1)
    elif kind == "weekly":
        dt = dt + _dt.timedelta(days=7)
    elif kind == "interval":
        mins = int(recur.get("minutes", 60))
        dt = now + _dt.timedelta(minutes=mins)
    else:
        return None
    # Catch up if we missed several cycles while off.
    while dt <= now:
        if kind == "daily":
            dt += _dt.timedelta(days=1)
        elif kind == "weekdays":
            dt += _dt.timedelta(days=1)
            while dt.weekday() >= 5:
                dt += _dt.timedelta(days=1)
        elif kind == "weekly":
            dt += _dt.timedelta(days=7)
        elif kind == "interval":
            dt += _dt.timedelta(minutes=int(recur.get("minutes", 60)))
        else:
            return None
    return dt.isoformat(timespec="seconds")


def _drain_overdue(items, now):
    """Split items into (still_pending, fired_now). Recurring items advance."""
    pending = []
    fired = []
    for r in items:
        try:
            dt = _dt.datetime.fromisoformat(r["fires_at"])
        except ValueError:
            continue
        if dt <= now:
            fired.append(r)
            if r.get("recur"):
                nxt = _advance_recurring(r, now)
                if nxt:
                    new = dict(r)
                    new["fires_at"] = nxt
                    new["id"] = _new_id()
                    pending.append(new)
        else:
            pending.append(r)
    return pending, fired


def _at_computer():
    """True unless we are confident the chair is empty.

    Anything that can't be determined counts as present, so a broken or
    missing presence check can only ever make Orion speak, never silence it.
    """
    if presence is None:
        return True
    if not getattr(config, "HOLD_REMINDERS_WHEN_AWAY", True):
        return True
    try:
        return presence.state() != presence.AWAY
    except Exception:
        return True


def _speak_held(entries):
    """Announce held reminders as one catch-up summary and clear them."""
    global _LAST_FIRED
    if not entries:
        return
    texts = [item["text"] for item, _ in entries]
    if len(texts) == 1:
        msg = f"Welcome back, Sir. Reminder: {texts[0]}."
    else:
        msg = (f"Welcome back, Sir. {len(texts)} reminders came up while you "
               f"were away: " + "; ".join(texts) + ".")
    _LAST_FIRED = entries[-1][0]
    try:
        from plyer import notification
        notification.notify(title="Orion Reminder", message="; ".join(texts),
                            app_name="Orion", timeout=20)
    except Exception:
        pass
    if _SPEAK is not None:
        try:
            _SPEAK(msg)
        except Exception:
            pass


def _deliver_or_hold(fired, present, now_mono):
    """Speak what's due, or park it until the user is back at the computer.

    A reminder announced to an empty room is a reminder missed, so when
    presence says away the item waits instead. It is released a few seconds
    after the user comes back -- long enough not to ambush them the instant
    they touch the mouse -- or once the hold has run long enough that
    delivering it late beats not delivering it at all.
    """
    global _PRESENT_SINCE
    if present:
        if _PRESENT_SINCE is None:
            _PRESENT_SINCE = now_mono
    else:
        _PRESENT_SINCE = None

    for item in fired:
        if present:
            _fire(item)
        else:
            _HELD.append((item, now_mono))

    if not _HELD:
        return

    settle = float(getattr(config, "REMINDER_RETURN_DELAY", 4))
    if present and _PRESENT_SINCE is not None and (
            now_mono - _PRESENT_SINCE) >= settle:
        due = list(_HELD)
        _HELD.clear()
        _speak_held(due)
        return

    # Still away. Don't hold anything forever -- past the cap, say it anyway
    # so a reminder can never be silently swallowed by a long absence.
    cap = float(getattr(config, "REMINDER_MAX_HOLD_MINUTES", 240)) * 60
    expired = [e for e in _HELD if (now_mono - e[1]) >= cap]
    if expired:
        for entry in expired:
            _HELD.remove(entry)
        _speak_held(expired)


def held_count():
    """How many reminders are waiting for the user to come back."""
    return len(_HELD)


def _scheduler_loop():
    """Background thread: every 15s, fire anything due (or hold it)."""
    while True:
        try:
            now = _now()
            with _LOCK:
                items = _load()
                pending, fired = _drain_overdue(items, now)
                if fired:
                    _save(pending)
            _deliver_or_hold(fired, _at_computer(), time.monotonic())
        except Exception:
            pass
        time.sleep(15)


def boot_catchup(speak):
    """Speak a single summary of reminders that fired while Orion was off.
    Removes them from storage. Returns the spoken string (or empty)."""
    now = _now()
    with _LOCK:
        items = _load()
        pending, missed = _drain_overdue(items, now)
        if missed:
            _save(pending)
    if not missed:
        return ""
    if len(missed) == 1:
        msg = f"While you were away, Sir -- reminder: {missed[0]['text']}."
    else:
        joined = "; ".join(r["text"] for r in missed)
        msg = (f"While you were away, Sir, you missed {len(missed)} "
                f"reminders: {joined}.")
    # Remember the last one for snooze.
    global _LAST_FIRED
    _LAST_FIRED = missed[-1]
    if speak is not None:
        try:
            speak(msg)
        except Exception:
            pass
    return msg


def start(speak):
    """Wire the scheduler. `speak` is Orion's speaker.say callable."""
    global _SPEAK, _THREAD
    _SPEAK = speak
    if _THREAD is not None and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=_scheduler_loop, daemon=True,
                                name="reminders")
    _THREAD.start()
