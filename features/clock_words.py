"""Convert clock times into natural spoken English.

Piper TTS reads "10:30" as "one zero three zero" -- digits, not a time. This
module produces strings like "half past ten" / "ten thirty in the morning"
that Piper pronounces naturally. Used by anything Orion speaks aloud that
mentions a time of day.
"""

_NUM_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
    19: "nineteen", 20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
}


def _minute_words(m):
    if m < 10:
        return f"oh {_NUM_WORDS[m]}"
    if m < 20:
        return _NUM_WORDS[m]
    tens, ones = divmod(m, 10)
    base = _NUM_WORDS[tens * 10]
    return base if ones == 0 else f"{base} {_NUM_WORDS[ones]}"


def _period_suffix(hour):
    if 5 <= hour < 12:
        return "in the morning"
    if 12 <= hour < 17:
        return "in the afternoon"
    if 17 <= hour < 21:
        return "in the evening"
    return "at night"


def spoken_time(dt, period=False):
    """Render a datetime/time as natural spoken English.

    period=True appends "in the morning/afternoon/evening" / "at night"
    for absolute references where context might be unclear.
    """
    h, m = dt.hour, dt.minute
    if h == 0 and m == 0:
        return "midnight"
    if h == 12 and m == 0:
        return "noon"
    h12 = h % 12 or 12
    h_word = _NUM_WORDS[h12]

    if m == 0:
        out = f"{h_word} o'clock"
    elif m == 15:
        out = f"quarter past {h_word}"
    elif m == 30:
        out = f"half past {h_word}"
    elif m == 45:
        next_h = (h + 1) % 24
        next_word = _NUM_WORDS[next_h % 12 or 12]
        if period:
            return f"quarter to {next_word} {_period_suffix(next_h)}"
        return f"quarter to {next_word}"
    else:
        out = f"{h_word} {_minute_words(m)}"

    if period:
        out = f"{out} {_period_suffix(h)}"
    return out


def spoken_hm(hour, minute, period=False):
    """Same as spoken_time() but takes raw hour/minute ints."""
    class _T:
        pass
    t = _T()
    t.hour, t.minute = hour, minute
    return spoken_time(t, period=period)


def spoken_hm_string(s, period=False):
    """Convert an 'HH:MM' string to spoken form. Returns input unchanged
    if it doesn't look like a clock string."""
    if not s or ":" not in s:
        return s
    try:
        h, m = s.split(":", 1)
        return spoken_hm(int(h), int(m[:2]), period=period)
    except (ValueError, KeyError):
        return s
