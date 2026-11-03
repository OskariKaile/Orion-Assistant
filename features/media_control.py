"""Orion's media control (Group 8).

Sends the keyboard's media keys (play/pause, next, previous) so it works with
any music or video app, and opens common media services.
"""

import webbrowser

import browser_control

try:
    import keyboard
except Exception:  # pragma: no cover
    keyboard = None

# Media services Orion can open: spoken name -> (apps.json key, web fallback).
_SERVICES = {
    "spotify": ("spotify", "https://open.spotify.com"),
    "youtube": ("youtube", "https://www.youtube.com"),
    "netflix": ("netflix", "https://www.netflix.com"),
}


def _send(key):
    if keyboard is None:
        return False
    try:
        keyboard.send(key)
        return True
    except Exception:
        return False


def play_pause():
    """Toggle play/pause in whatever media app is active."""
    if _send("play/pause media"):
        return "Toggled playback, Sir."
    return "I couldn't reach the media keys."


def next_track():
    """Skip to the next track."""
    if _send("next track"):
        return "Skipping ahead."
    return "I couldn't reach the media keys."


def previous_track():
    """Go back to the previous track."""
    if _send("previous track"):
        return "Going back a track."
    return "I couldn't reach the media keys."


def open_media(name):
    """Open a media service as an app (if listed) or in the browser."""
    name = name.strip().lower()
    service = _SERVICES.get(name)
    if service is None:
        return None  # not a known media service
    app_key, web_url = service
    apps = browser_control._load_json(__import__("config").APPS_FILE, {})
    if isinstance(apps, dict) and any(app_key in k.lower() for k in apps):
        return browser_control.open_app(app_key)
    webbrowser.open(web_url)
    return f"Opening {name}."
