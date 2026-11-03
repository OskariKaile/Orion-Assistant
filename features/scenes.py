"""Orion's scenes and routines (Group 11).

A scene is a named list of actions defined in scenes.json. Orion runs each
action in sequence. "Orion panic" instantly closes every window matching the
PANIC_APPS keyword list.

scenes.json format:
    {
      "work mode": [
        {"action": "lights_on", "brightness": 100},
        {"action": "open_work_tabs"},
        {"action": "open_url", "value": "https://music.example/focus"},
        {"action": "open_app", "value": "discord"}
      ]
    }

Supported actions: open_app, open_url, open_work_tabs, google, lights_on,
lights_off, brightness, switch, set_temperature, all_devices_off, volume,
media, lock_pc, wait.
"""

import json
import os
import time
import webbrowser

import _paths
import browser_control
import config
import media_control
import pc_control
import smart_home

try:
    import pygetwindow as gw
except Exception:  # pragma: no cover
    gw = None


def _orion_path(name):
    return os.path.join(_paths.ROOT, name)


def _load_scenes():
    try:
        with open(_orion_path(config.SCENES_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_scenes():
    """Return the configured scene names."""
    return list(_load_scenes().keys())


def _run_action(step):
    """Execute one scene action described by a dict."""
    action = str(step.get("action", "")).lower()
    value = step.get("value")

    if action == "open_app":
        browser_control.open_app(str(value or ""))
    elif action == "open_url":
        webbrowser.open(browser_control._normalize_url(str(value or "")))
    elif action == "open_work_tabs":
        browser_control.open_work_tabs()
    elif action == "google":
        browser_control.google_search(str(value or ""))
    elif action == "lights_on":
        smart_home.lights_on(brightness=step.get("brightness"),
                             color=step.get("color"))
    elif action == "lights_off":
        smart_home.lights_off()
    elif action == "brightness":
        smart_home.set_brightness(step.get("brightness", value))
    elif action == "switch":
        smart_home.switch(bool(step.get("on", value)))
    elif action == "set_temperature":
        smart_home.set_temperature(step.get("temperature", value))
    elif action == "all_devices_off":
        smart_home.all_devices_off()
    elif action == "volume":
        pc_control.set_volume(value)
    elif action == "media":
        media_control.open_media(str(value or ""))
    elif action == "lock_pc":
        pc_control.lock_pc()
    elif action == "wait":
        time.sleep(float(value or 1))


def run_scene(name):
    """Run the named scene from scenes.json. Returns a spoken status string."""
    name = (name or "").strip().lower()
    scenes = {k.lower(): v for k, v in _load_scenes().items()}
    steps = scenes.get(name)
    if steps is None and not name.endswith("mode"):
        steps = scenes.get(name + " mode")
        if steps is not None:
            name = name + " mode"
    if steps is None:
        return (f"I don't have a scene called {name}. Add it to "
                f"{config.SCENES_FILE}.")
    if not isinstance(steps, list):
        return f"The scene {name} is not formatted correctly."

    for step in steps:
        if isinstance(step, dict):
            try:
                _run_action(step)
            except Exception:
                pass  # one bad action shouldn't abort the whole scene
            time.sleep(0.3)
    return f"{name.capitalize()} activated, Sir."


def panic():
    """Instantly close every window matching the PANIC_APPS keyword list."""
    if gw is None:
        return "I can't control windows right now."
    keywords = [k.lower() for k in config.PANIC_APPS]
    closed = 0
    try:
        windows = gw.getAllWindows()
    except Exception:
        return "I couldn't reach the window list."
    for win in windows:
        title = (getattr(win, "title", "") or "").lower()
        if title and any(k in title for k in keywords):
            try:
                win.close()
                closed += 1
            except Exception:
                continue
    if closed:
        return f"Panic. Closed {closed} windows, Sir."
    return "Nothing to hide right now, Sir."
