"""Orion's daily briefing.

Gathers live data -- weather, news headlines and today's calendar events --
and assembles a prompt for the AI to turn into a natural spoken morning
summary. Network access uses only the standard library so there are no extra
dependencies; every fetch fails soft so a missing source never blocks startup.

* Weather  -- Open-Meteo (free, no API key required).
* News     -- any RSS feed (default: BBC World).
* Calendar -- an optional .ics file or URL, parsed for today's events.
"""

import datetime
import json
import urllib.request
import xml.etree.ElementTree as ET

import config

_TIMEOUT = 10


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Orion/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# Open-Meteo WMO weather codes -> human-readable conditions.
_WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "dense drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "heavy freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "light rain showers",
    81: "rain showers", 82: "violent rain showers", 85: "light snow showers",
    86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def get_weather():
    """Return a one-line weather summary, or None if it can't be fetched."""
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={config.BRIEFING_LATITUDE}"
            f"&longitude={config.BRIEFING_LONGITUDE}"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min"
            "&timezone=auto&forecast_days=1"
        )
        data = json.loads(_http_get(url))
        cur = data.get("current", {})
        daily = data.get("daily", {})
        temp = round(cur["temperature_2m"])
        desc = _WEATHER_CODES.get(cur.get("weather_code"), "unknown conditions")
        hi = round(daily["temperature_2m_max"][0])
        lo = round(daily["temperature_2m_min"][0])
        return (f"{config.BRIEFING_LOCATION_NAME}: currently {temp} degrees, "
                f"{desc}. Today's high {hi}, low {lo}.")
    except Exception:
        return None


def _categorize(code):
    """Group a WMO weather code into a coarse category for change detection."""
    if code in (95, 96, 99):
        return "storm"
    if code in (71, 73, 75, 77, 85, 86, 56, 57, 66, 67):
        return "snow"
    if code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        return "rain"
    if code in (0, 1):
        return "clear"
    return "cloud"


def weather_category():
    """Return the current weather as a coarse category for change detection:
    'clear', 'cloud', 'rain', 'snow', 'storm', or None if it can't be fetched.
    """
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={config.BRIEFING_LATITUDE}"
            f"&longitude={config.BRIEFING_LONGITUDE}"
            "&current=weather_code&timezone=auto"
        )
        code = json.loads(_http_get(url)).get("current", {}).get("weather_code")
    except Exception:
        return None
    return _categorize(code) if code is not None else None


def get_news():
    """Return a list of recent headline strings (possibly empty)."""
    try:
        root = ET.fromstring(_http_get(config.NEWS_RSS_URL))
        titles = []
        for item in root.iter("item"):
            title = item.findtext("title")
            if title and title.strip():
                titles.append(title.strip())
            if len(titles) >= config.NEWS_HEADLINE_COUNT:
                break
        return titles
    except Exception:
        return []


def _parse_ics_date(line):
    """Pull (date, 'HH:MM') out of an ICS DTSTART line; time may be ''."""
    raw = line.split(":", 1)[-1].strip().replace("Z", "")
    try:
        date = datetime.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    except (ValueError, IndexError):
        return None, ""
    time_str = ""
    if "T" in raw:
        clock = raw.split("T", 1)[1]
        if len(clock) >= 4:
            time_str = f"{clock[0:2]}:{clock[2:4]}"
    return date, time_str


def _todays_events(text):
    """Parse an ICS document and return today's [(time, summary)] events."""
    # Unfold continuation lines (RFC 5545: a leading space/tab continues
    # the previous line).
    lines = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    today = datetime.date.today()
    events = []
    in_event = False
    summary = ""
    start_date, start_time = None, ""
    for line in lines:
        if line.startswith("BEGIN:VEVENT"):
            in_event, summary = True, ""
            start_date, start_time = None, ""
        elif line.startswith("END:VEVENT"):
            if in_event and start_date == today:
                events.append((start_time, summary or "Untitled event"))
            in_event = False
        elif in_event and line.startswith("SUMMARY"):
            summary = line.split(":", 1)[-1].strip()
        elif in_event and line.startswith("DTSTART"):
            start_date, start_time = _parse_ics_date(line)
    events.sort(key=lambda e: e[0] or "")
    return events


def get_calendar():
    """Return today's calendar events, or [] if no calendar is configured."""
    src = config.CALENDAR_ICS
    if not src:
        return []
    try:
        if src.lower().startswith("http"):
            text = _http_get(src)
        else:
            with open(src, "r", encoding="utf-8") as fh:
                text = fh.read()
        return _todays_events(text)
    except Exception:
        return []


def build_briefing_prompt():
    """Collect live data and build the prompt the AI turns into the summary."""
    weather = get_weather()
    news = get_news()
    events = get_calendar()

    from clock_words import spoken_time
    now = datetime.datetime.now()
    parts = ["[Daily briefing data]",
             f"Date and time: {now:%A, %B %d, %Y}, "
             f"{spoken_time(now, period=True)}"]

    parts.append(f"Weather: {weather}" if weather else "Weather: unavailable")

    if news:
        parts.append("Top headlines:")
        parts.extend(f"- {t}" for t in news)
    else:
        parts.append("News: unavailable")

    # Pull the unified agenda for today (events + reminders + todos)
    # so the morning brief mentions all three buckets, not just .ics events.
    agenda_lines = _agenda_briefing_lines()
    if agenda_lines:
        parts.append("Today's agenda:")
        parts.extend(agenda_lines)
    elif events:
        from clock_words import spoken_hm_string
        parts.append("Today's calendar:")
        for time_str, summary in events:
            label = f"{spoken_hm_string(time_str)} " if time_str else ""
            parts.append(f"- {label}{summary}")
    else:
        parts.append("Agenda: nothing scheduled today")

    parts.append("")
    parts.append(
        "You are Orion, a JARVIS-style voice assistant. Using only the data "
        "above, give a chatty spoken morning briefing. Greet me as Sir, "
        "summarize the weather, mention the most important headlines, then "
        "walk me through today's agenda in a natural conversational way -- "
        "lead with whatever I have earliest, group nearby items, and "
        "comment on the shape of the day (e.g. 'you've got a busy morning' "
        "if there are several morning items, or 'pretty light today' if "
        "there are few). Mention the pending todo count near the end if "
        "any. Keep it under 150 words. No markdown, no headings, no bullet "
        "points. Skip any section that is unavailable."
    )
    return "\n".join(parts)


def _agenda_briefing_lines():
    """Pull today's events, reminders, and todos from the unified agenda
    and format them as briefing input lines. Returns [] if nothing today."""
    try:
        import agenda
    except Exception:
        return []
    try:
        data = agenda.snapshot("today")
    except Exception:
        return []
    lines = []
    timed = []  # (datetime, label)
    for ev in data.get("events", []):
        dt = ev.get("start")
        if dt is None:
            continue
        if ev.get("all_day"):
            timed.append((dt, f"all-day: {ev['title']}"))
        else:
            timed.append((dt, f"{_briefing_clock(dt)} -- {ev['title']} "
                              "(calendar event)"))
    for r in data.get("reminders", []):
        dt = r.get("_dt")
        if dt is None:
            continue
        timed.append((dt, f"{_briefing_clock(dt)} -- reminder to {r['text']}"))
    timed.sort(key=lambda t: t[0])
    for _dt, label in timed:
        lines.append(f"- {label}")
    todos = data.get("todos", [])
    if todos:
        n = len(todos)
        lines.append(f"- pending todos ({n}): " + "; ".join(todos[:5]))
    return lines


def _briefing_clock(dt):
    h = dt.hour
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    if dt.minute == 0:
        return f"{h12}{suffix}"
    return f"{h12}:{dt.minute:02d}{suffix}"
