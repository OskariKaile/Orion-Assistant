"""YouTube music/video playback for Orion.

The user says "play some music" or "play something motivational" and Orion
opens a YouTube watch URL in a Chrome tab without stealing focus. The video
autoplays on its own (YouTube does that for watch URLs by default). Skip /
pause / next still go through the existing media-key controls in
media_control.py -- those work cross-app and don't need YouTube-specific
plumbing.

Search: uses `youtube-search-python` (free, no API key) to look up the first
matching video for the spoken query, so we open a real video URL instead of
just a search results page. If the package isn't installed, we fall back
to the search-results page (one extra click for the user, but it still
works).

Background open: we try to use Chrome's `--new-tab` so the new tab lands in
an already-open window without bringing Chrome to the foreground. Falls
back to plain webbrowser.open if Chrome isn't found.
"""
import os
import re
import subprocess
import urllib.parse
import webbrowser

import _paths
import config

_CHROME_PATH_HINTS = (
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _chrome_exe():
    """Return the Chrome .exe path, or None if Chrome isn't installed."""
    # Prefer apps.json's "chrome" entry if the user has it pointing somewhere.
    try:
        import json
        path = os.path.join(_paths.ROOT, config.APPS_FILE)
        with open(path, "r", encoding="utf-8") as fh:
            apps = json.load(fh)
        if isinstance(apps, dict):
            for key, value in apps.items():
                if "chrome" in key.lower():
                    expanded = os.path.expandvars(str(value))
                    if os.path.isfile(expanded):
                        return expanded
    except (OSError, ValueError):
        pass
    for raw in _CHROME_PATH_HINTS:
        expanded = os.path.expandvars(raw)
        if os.path.isfile(expanded):
            return expanded
    return None


_VIDEO_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')

_YT_HEADERS = {
    # A real-browser user-agent is required; YouTube serves a JS shell to
    # bot-looking UAs and the ytInitialData blob never appears.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _first_video_id(query):
    """First YouTube video id for `query`, or None.

    Direct HTML scrape: hit the search results page, find the first
    `"videoId":"..."` in the embedded ytInitialData JSON blob. No API key,
    no maintained third-party package -- those break every few months when
    YouTube tweaks things; the videoId field has been stable for years.
    """
    try:
        import requests
    except ImportError:
        return None
    try:
        url = ("https://www.youtube.com/results?search_query="
               + urllib.parse.quote(query))
        r = requests.get(url, headers=_YT_HEADERS, timeout=4)
        if r.status_code != 200:
            return None
        m = _VIDEO_ID_RE.search(r.text)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def _open_in_background_chrome(url):
    """Open `url` in Chrome as a NEW TAB without stealing focus.

    Returns True if Chrome was used; False if we had to fall through to the
    default browser path.
    """
    exe = _chrome_exe()
    if not exe:
        return False
    try:
        # --new-tab: lands in an existing Chrome window if one is open and
        # doesn't push it to the foreground. If no Chrome is running it
        # opens one (one-time focus event, unavoidable).
        subprocess.Popen([exe, "--new-tab", url],
                         creationflags=_NO_WINDOW)
        return True
    except OSError:
        return False


# Some pleasant defaults so "play music" with no further hint still does
# something sensible. Gemini usually picks a much better query itself; this
# is the bare-knuckle fallback.
_DEFAULT_QUERY = "lofi hip hop radio"


def _clean_query(query):
    """Strip filler phrases the user is likely to wrap around the request."""
    if not query:
        return ""
    q = query.strip().lower()
    # Pull off common command preambles so the search query is just the
    # subject. "Play some motivational music" -> "motivational music".
    leads = [
        "play some", "play something", "play me", "play a", "play",
        "put on some", "put on", "queue up", "start playing", "find me",
    ]
    for lead in leads:
        if q.startswith(lead + " "):
            q = q[len(lead) + 1:]
            break
    # Drop trailing "for me" / "please" / "sir" tails. Loop because the
    # user can stack them ("for me please").
    tails = (" for me", " please", " sir", " thanks", " thank you")
    changed = True
    while changed:
        changed = False
        for tail in tails:
            if q.endswith(tail):
                q = q[: -len(tail)]
                changed = True
    return q.strip()


def play(query):
    """Play whatever `query` resolves to on YouTube.

    Empty or extremely vague queries fall back to a default. Returns a
    short spoken-style status string for Orion.
    """
    q = _clean_query(query) or _DEFAULT_QUERY
    video_id = _first_video_id(q)
    if video_id:
        url = (f"https://www.youtube.com/watch?v={video_id}"
               "&autoplay=1")
    else:
        # No id (package missing or search failed) -- open the results page
        # and let the user click. Better than failing.
        url = ("https://www.youtube.com/results?search_query="
               + urllib.parse.quote(q))

    if _open_in_background_chrome(url):
        return f"Playing {q} on YouTube, Sir."
    # Last resort: default browser. May steal focus once.
    webbrowser.open(url)
    return f"Playing {q} on YouTube, Sir."


def play_url(url):
    """Open a specific YouTube URL (e.g. a playlist) in the background."""
    if _open_in_background_chrome(url):
        return True
    webbrowser.open(url)
    return True
