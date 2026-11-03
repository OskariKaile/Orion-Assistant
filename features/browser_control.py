"""Orion's browser and app control (Group 6).

Opens work tabs, launches apps from a custom list, runs Google searches,
navigates to sites, closes Chrome tabs, and saves the current tab.

Each function returns a short spoken-style status string.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse
import webbrowser

import _paths
import config

try:
    import pyautogui
except Exception:  # pragma: no cover - headless environments
    pyautogui = None

try:
    import pygetwindow as gw
except Exception:
    gw = None

try:
    import pyperclip
except Exception:
    pyperclip = None


def _orion_path(name):
    """Resolve a config file name relative to the Orion folder."""
    return os.path.join(_paths.ROOT, name)


def _load_json(name, default):
    path = _orion_path(name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(name, data):
    path = _orion_path(name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    except OSError:
        return False


def _normalize_url(url):
    """Make a spoken site name into something webbrowser can open."""
    url = url.strip().replace(" dot ", ".").replace(" ", "")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        if "." not in url:
            url += ".com"
        url = "https://" + url
    return url


def open_work_tabs():
    """Open every URL listed in work_tabs.json in the browser."""
    tabs = _load_json(config.WORK_TABS_FILE, [])
    if not isinstance(tabs, list) or not tabs:
        return ("Your work tabs list is empty. Add URLs to "
                f"{config.WORK_TABS_FILE} first.")
    for url in tabs:
        webbrowser.open(_normalize_url(str(url)))
        time.sleep(0.4)
    return f"Yes sir, opening your {len(tabs)} work tabs."


def _cache_app(name, path):
    """Persist a newly-found app's path into apps.json for next time."""
    apps = _load_json(config.APPS_FILE, {})
    if not isinstance(apps, dict):
        apps = {}
    apps[name.lower()] = path
    _save_json(config.APPS_FILE, apps)


def open_app(name, orion=None):
    """Launch an app, finding its .exe automatically when needed.

    1. Look in apps.json (instant).
    2. Try smart discovery (Start Menu / registry / game launchers / FS walk).
       A confident match is launched AND cached into apps.json so the next
       call is instant.
    3. If nothing confident is found and we have an `orion` handle, set a
       "teach me" pending flag so the next thing the user says is captured
       as a path and stored. Returns a JARVIS-style prompt the user hears.
    """
    name = name.strip()
    if not name:
        return "Which app should I open, Sir?"
    low = name.lower()
    apps = _load_json(config.APPS_FILE, {})
    apps = {k.lower(): v for k, v in apps.items()} if isinstance(apps, dict) else {}

    path = apps.get(low)
    matched_key = low if path else None
    if path is None:  # tolerate partial spoken names
        for key, value in apps.items():
            if low in key or key in low:
                path, matched_key = value, key
                break

    if path is None:
        # Smart discovery -- search the system, cache anything we find.
        try:
            import app_finder
            results = app_finder.find(low)
        except Exception:
            results = []
        confident = [r for r in results if r[0] >= app_finder.HIGH_CONFIDENCE] \
            if results else []
        if confident:
            score, display, exe = confident[0]
            _cache_app(low, exe)
            if app_finder.launch(exe):
                return f"Opening {display}."
            return f"I found {display} but couldn't launch it, Sir."
        # Nothing confident -- ask the user to teach us, if possible.
        if orion is not None and hasattr(orion, "_pending_app_teach"):
            orion._pending_app_teach = {"name": low, "expires": _now() + 60}
            return (f"I couldn't find {name} on the usual paths, Sir. "
                    "Tell me where it lives and I'll remember.")
        return (f"I don't have {name} in my apps list, Sir, and I couldn't "
                "find it on the usual paths.")

    expanded = os.path.expandvars(path)
    try:
        subprocess.Popen([expanded])
        return f"Opening {matched_key or name}."
    except OSError:
        return (f"I couldn't launch {matched_key or name}. "
                f"Check its path in {config.APPS_FILE}.")


def _now():
    import time as _t
    return _t.monotonic()


# Words people might use to bail out of the "teach me a path" prompt.
_TEACH_CANCEL = (
    "never mind", "nevermind", "cancel", "forget it", "stop", "no thanks",
    "no thank you",
)

# A Windows path: drive letter + colon + optional separator + the rest, OR
# any token containing a backslash. We accept both for STT robustness.
_PATH_RE = re.compile(r'([A-Za-z]:[\\/][^"\n\r]*?\.exe)', re.I)
_FOLDER_RE = re.compile(r'([A-Za-z]:[\\/][^"\n\r]*)', re.I)


def receive_taught_path(orion, query):
    """Handle the user's reply to a "where is it installed" prompt.

    Returns True if the reply was consumed (path stored or cancelled),
    False if it wasn't a teaching response and normal dispatch should
    continue.
    """
    pending = getattr(orion, "_pending_app_teach", None)
    if not pending:
        return False
    name = pending.get("name", "")
    if pending.get("expires", 0) < _now():
        orion._pending_app_teach = None
        return False

    low = query.lower().strip()
    if any(c in low for c in _TEACH_CANCEL):
        orion._pending_app_teach = None
        orion.speaker.say(f"As you wish, Sir. I'll leave {name} for now.")
        return True

    # Try to pull a path out of the spoken reply. Exact .exe first, then any
    # folder; expand env vars; verify existence.
    m = _PATH_RE.search(query)
    if m:
        candidate = os.path.expandvars(m.group(1).strip().strip('"'))
        if os.path.isfile(candidate):
            _cache_app(name, candidate)
            orion._pending_app_teach = None
            try:
                subprocess.Popen([candidate])
            except OSError:
                pass
            orion.speaker.say(
                f"Got it. Opening {name} now -- I'll remember the path.")
            return True

    m = _FOLDER_RE.search(query)
    if m:
        candidate = os.path.expandvars(m.group(1).strip().strip('"'))
        if os.path.isdir(candidate):
            try:
                import app_finder
                exe = app_finder._best_exe_in_folder(candidate, name)
            except Exception:
                exe = None
            if exe:
                _cache_app(name, exe)
                orion._pending_app_teach = None
                try:
                    subprocess.Popen([exe])
                except OSError:
                    pass
                orion.speaker.say(
                    f"Found it inside {candidate}. Opening {name} -- "
                    "I'll remember.")
                return True
            orion.speaker.say(
                f"I couldn't see an executable for {name} inside that "
                "folder, Sir.")
            orion._pending_app_teach = None
            return True

    # Couldn't parse a path -- give up gracefully (one shot, no badgering).
    orion._pending_app_teach = None
    orion.speaker.say(
        f"I didn't catch a path I could use, Sir. You can add {name} "
        f"to {config.APPS_FILE} directly if you want.")
    return True


def google_search(query):
    """Open a Google search for `query` in the browser."""
    query = query.strip()
    if not query or query == "this":
        if pyperclip is not None:
            query = (pyperclip.paste() or "").strip()
    if not query:
        return "What should I search for, Sir?"
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    webbrowser.open(url)
    return f"Searching Google for {query}."


def go_to_site(site):
    """Open a spoken site name or URL in the browser."""
    url = _normalize_url(site)
    if not url:
        return "Which site should I open, Sir?"
    webbrowser.open(url)
    return f"Opening {site.strip()}."


def _focus_chrome():
    """Bring the Chrome window to the foreground. Returns True on success."""
    if gw is None:
        return False
    try:
        wins = gw.getWindowsWithTitle(config.CHROME_WINDOW_HINT)
    except Exception:
        return False
    for win in wins:
        if config.CHROME_WINDOW_HINT in (win.title or "").lower():
            try:
                if win.isMinimized:
                    win.restore()
                win.activate()
                time.sleep(0.4)
                return True
            except Exception:
                continue
    return False


def close_all_tabs():
    """Focus Chrome and close its tabs one by one with Ctrl+W."""
    if pyautogui is None:
        return "I can't control the keyboard right now."
    if not _focus_chrome():
        return "I couldn't find a Chrome window to close."
    for _ in range(40):
        pyautogui.hotkey("ctrl", "w")
        time.sleep(0.08)
    return "Closed your Chrome tabs, Sir."


def _current_chrome_url():
    """Grab the active Chrome tab's URL via Ctrl+L, Ctrl+C."""
    if pyautogui is None or pyperclip is None:
        return ""
    if not _focus_chrome():
        return ""
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.2)
    pyautogui.press("escape")
    return (pyperclip.paste() or "").strip()


def save_work_tab():
    """Append the current Chrome tab's URL to work_tabs.json."""
    url = _current_chrome_url()
    if not url or "." not in url:
        return "I couldn't read the current tab's address."
    tabs = _load_json(config.WORK_TABS_FILE, [])
    if not isinstance(tabs, list):
        tabs = []
    if url in tabs:
        return "That tab is already saved."
    tabs.append(url)
    if _save_json(config.WORK_TABS_FILE, tabs):
        return "Saved this page as a work tab, Sir."
    return "I couldn't save the work tab."
