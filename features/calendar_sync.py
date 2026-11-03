"""Google Calendar + .ics read/write for Orion.

Tries Google Calendar API first (OAuth 2.0 -- needs credentials.json +
token.json in the Orion folder). Falls back to the local .ics file at
config.CALENDAR_ICS for read-only access if Google isn't set up.

Setup (one time):
    python setup_calendar.py
The helper opens a browser, you click "Allow" on the Google consent
screen, and `token.json` is saved. After that the token auto-refreshes.

Why OAuth, not an app password
------------------------------
Gmail app passwords only work for IMAP/SMTP (basic auth). The Google
Calendar API requires OAuth 2.0 with a refresh token. Different auth
path entirely.

Public API
----------
* available()              -> bool, True if Google or .ics is usable.
* list_events(start, end)  -> [Event, ...]  sorted by start.
* add_event(title, start, end=None, location="") -> (Event, ok_bool)
* delete_event(event_id)   -> bool
* update_event(event_id, **fields) -> bool

Each Event is a dict with: id, title, start (datetime), end (datetime
or None), location, source ('google' | 'ics'), all_day (bool).
"""

import datetime as _dt
import os
import re

import _paths
import config


_HERE = _paths.ROOT
_CRED_PATH = os.path.join(_HERE, "credentials.json")
_TOKEN_PATH = os.path.join(_HERE, "token.json")
_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_CAL_ID = getattr(config, "GOOGLE_CALENDAR_ID", "primary")

_service = None     # lazy Google API client
_service_tried = False


# --- Google client ---------------------------------------------------------

def _get_service():
    """Return a Google Calendar API service object, or None if unavailable.
    Cached after first call so we don't re-auth on every query."""
    global _service, _service_tried
    if _service is not None or _service_tried:
        return _service
    _service_tried = True
    if not os.path.exists(_TOKEN_PATH):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except Exception:
        return None
    try:
        creds = Credentials.from_authorized_user_file(_TOKEN_PATH, _SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(_TOKEN_PATH, "w", encoding="utf-8") as fh:
                    fh.write(creds.to_json())
            else:
                return None
        _service = build("calendar", "v3", credentials=creds,
                          cache_discovery=False)
    except Exception:
        _service = None
    return _service


def google_available():
    return _get_service() is not None


def ics_available():
    src = getattr(config, "CALENDAR_ICS", "")
    return bool(src)


def available():
    return google_available() or ics_available()


def status():
    """Human-readable connection status for the hub / health check.

    Unlike google_available(), this distinguishes the cases that matter to
    the user -- especially 'token expired' (needs a reconnect) vs. 'never
    set up'. Does its own lightweight auth probe so it doesn't depend on
    the cached _get_service() state.
    """
    if not os.path.exists(_TOKEN_PATH):
        if ics_available():
            return "local .ics only (Google not set up)"
        return "not set up"
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except Exception:
        return "Google libraries not installed"
    try:
        creds = Credentials.from_authorized_user_file(_TOKEN_PATH, _SCOPES)
        if creds.valid:
            return "connected (Google)"
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(_TOKEN_PATH, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
            return "connected (Google)"
        return "token expired -- reconnect"
    except Exception:
        return "token expired -- reconnect"


# --- helpers ---------------------------------------------------------------

def _to_dt(value):
    """Coerce a Google API datetime/date string or datetime to datetime."""
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time(0, 0))
    if isinstance(value, str):
        # 'YYYY-MM-DD' (all-day) or full ISO with TZ.
        try:
            if len(value) == 10:
                return _dt.datetime.fromisoformat(value)
            # Strip trailing 'Z' for older Pythons.
            v = value.replace("Z", "+00:00")
            return _dt.datetime.fromisoformat(v).astimezone().replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _google_event_to_dict(ev):
    start_raw = ev["start"].get("dateTime") or ev["start"].get("date")
    end_raw = ev["end"].get("dateTime") or ev["end"].get("date") \
        if ev.get("end") else None
    all_day = "date" in ev["start"]
    return {
        "id": ev.get("id", ""),
        "title": ev.get("summary", "(no title)"),
        "start": _to_dt(start_raw),
        "end": _to_dt(end_raw) if end_raw else None,
        "location": ev.get("location", ""),
        "source": "google",
        "all_day": all_day,
    }


# --- read ------------------------------------------------------------------

def list_events(start=None, end=None, max_results=50):
    """Return events between `start` and `end` (datetimes). Defaults to now
    -> +24h. Pulls from Google first; if unavailable, parses .ics."""
    now = _dt.datetime.now()
    start = start or now
    end = end or (start + _dt.timedelta(days=1))

    svc = _get_service()
    if svc is not None:
        try:
            tmin = start.astimezone().isoformat()
            tmax = end.astimezone().isoformat()
            resp = svc.events().list(
                calendarId=_CAL_ID, timeMin=tmin, timeMax=tmax,
                maxResults=max_results, singleEvents=True,
                orderBy="startTime").execute()
            items = resp.get("items", [])
            return sorted(
                (_google_event_to_dict(e) for e in items),
                key=lambda e: e["start"] or _dt.datetime.max)
        except Exception:
            pass

    # ICS fallback (read-only).
    if ics_available():
        return _ics_events_between(start, end)
    return []


def _ics_events_between(start, end):
    """Minimal local .ics parser. Returns events overlapping [start, end]."""
    src = getattr(config, "CALENDAR_ICS", "")
    if not src:
        return []
    try:
        if src.lower().startswith("http"):
            import urllib.request
            with urllib.request.urlopen(src, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        else:
            with open(src, "r", encoding="utf-8") as fh:
                text = fh.read()
    except Exception:
        return []
    events = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            cur = {"source": "ics", "id": "", "title": "", "start": None,
                   "end": None, "location": "", "all_day": False}
        elif line == "END:VEVENT" and cur is not None:
            if cur["start"] and start <= cur["start"] < end:
                events.append(cur)
            cur = None
        elif cur is not None:
            if line.startswith("SUMMARY:"):
                cur["title"] = line[len("SUMMARY:"):]
            elif line.startswith("LOCATION:"):
                cur["location"] = line[len("LOCATION:"):]
            elif line.startswith("UID:"):
                cur["id"] = line[len("UID:"):]
            elif line.startswith("DTSTART"):
                cur["start"], cur["all_day"] = _parse_ics_dt(line)
            elif line.startswith("DTEND"):
                cur["end"], _ = _parse_ics_dt(line)
    return sorted(events, key=lambda e: e["start"] or _dt.datetime.max)


def _parse_ics_dt(line):
    """Parse 'DTSTART:20260522T150000' or 'DTSTART;VALUE=DATE:20260522'."""
    value = line.split(":", 1)[1] if ":" in line else ""
    all_day = "VALUE=DATE" in line and "T" not in value
    try:
        if all_day:
            return (_dt.datetime.strptime(value, "%Y%m%d"), True)
        v = value.rstrip("Z")
        return (_dt.datetime.strptime(v, "%Y%m%dT%H%M%S"), False)
    except ValueError:
        return (None, False)


# --- write -----------------------------------------------------------------

def add_event(title, start, end=None, location=""):
    """Create a calendar event on Google. Returns (event_dict, ok_bool).
    Falls back to (None, False) if Google isn't set up -- the agenda
    layer should refuse to "add" without a writable backend."""
    svc = _get_service()
    if svc is None:
        return (None, False)
    if end is None:
        end = start + _dt.timedelta(hours=1)
    body = {
        "summary": title,
        "start": {"dateTime": start.astimezone().isoformat()},
        "end": {"dateTime": end.astimezone().isoformat()},
    }
    if location:
        body["location"] = location
    try:
        created = svc.events().insert(
            calendarId=_CAL_ID, body=body).execute()
        return (_google_event_to_dict(created), True)
    except Exception:
        return (None, False)


def delete_event(event_id):
    svc = _get_service()
    if svc is None or not event_id:
        return False
    try:
        svc.events().delete(calendarId=_CAL_ID, eventId=event_id).execute()
        return True
    except Exception:
        return False


def update_event(event_id, **fields):
    """Patch an event. Accepts title=, start=, end=, location=."""
    svc = _get_service()
    if svc is None or not event_id:
        return False
    body = {}
    if "title" in fields:
        body["summary"] = fields["title"]
    if "start" in fields and fields["start"]:
        body["start"] = {"dateTime": fields["start"].astimezone().isoformat()}
    if "end" in fields and fields["end"]:
        body["end"] = {"dateTime": fields["end"].astimezone().isoformat()}
    if "location" in fields:
        body["location"] = fields["location"]
    if not body:
        return False
    try:
        svc.events().patch(
            calendarId=_CAL_ID, eventId=event_id, body=body).execute()
        return True
    except Exception:
        return False


def find_event(text, start=None, end=None):
    """Fuzzy-match an event by title in [start, end]. Returns the first
    match or None. Used by 'cancel the dentist' / 'reschedule X' flows."""
    needle = (text or "").strip().lower()
    if not needle:
        return None
    now = _dt.datetime.now()
    events = list_events(start or now,
                          end or (now + _dt.timedelta(days=14)))
    for ev in events:
        if needle in ev["title"].lower():
            return ev
    return None
