"""Orion's smart-home control (Group 10).

Talks to a local Home Assistant instance over its REST API to control lights,
smart plugs, and a thermostat. Configure HOME_ASSISTANT_URL and
HOME_ASSISTANT_TOKEN in config.py / secrets_local.py.
"""

import json
import urllib.error
import urllib.request

import config

_TIMEOUT = 8

# Named colours -> RGB, for "set the lights blue" style commands.
COLORS = {
    "red": [255, 0, 0], "green": [0, 255, 0], "blue": [0, 0, 255],
    "white": [255, 255, 255], "warm": [255, 180, 107], "orange": [255, 140, 0],
    "purple": [160, 32, 240], "pink": [255, 105, 180], "yellow": [255, 255, 0],
    "cyan": [0, 255, 255],
}


def configured():
    """True if Home Assistant credentials are set."""
    return bool(config.HOME_ASSISTANT_URL and config.HOME_ASSISTANT_TOKEN)


def _call_service(domain, service, data):
    """POST a service call to Home Assistant. Returns True on success."""
    if not configured():
        return False
    url = (config.HOME_ASSISTANT_URL.rstrip("/")
           + f"/api/services/{domain}/{service}")
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {config.HOME_ASSISTANT_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _no_ha():
    return ("Smart home isn't set up. Add your Home Assistant URL and token "
            "to the config.")


def lights_on(entity=None, brightness=None, color=None):
    """Turn a light on, optionally with brightness (0-100) and a colour name."""
    if not configured():
        return _no_ha()
    data = {"entity_id": entity or config.HA_DEFAULT_LIGHT}
    if brightness is not None:
        data["brightness_pct"] = max(0, min(100, int(brightness)))
    if color and color in COLORS:
        data["rgb_color"] = COLORS[color]
    if _call_service("light", "turn_on", data):
        return "Lights on, Sir."
    return "I couldn't reach the lights."


def lights_off(entity=None):
    """Turn a light off."""
    if not configured():
        return _no_ha()
    if _call_service("light", "turn_off",
                     {"entity_id": entity or config.HA_DEFAULT_LIGHT}):
        return "Lights off."
    return "I couldn't reach the lights."


def set_brightness(brightness, entity=None):
    """Set a light's brightness as a percentage."""
    if not configured():
        return _no_ha()
    try:
        pct = max(0, min(100, int(brightness)))
    except (TypeError, ValueError):
        return "What brightness level, Sir?"
    if _call_service("light", "turn_on",
                     {"entity_id": entity or config.HA_DEFAULT_LIGHT,
                      "brightness_pct": pct}):
        return f"Brightness set to {pct} percent."
    return "I couldn't reach the lights."


def switch(on, entity=None):
    """Turn a smart plug / switch on or off."""
    if not configured():
        return _no_ha()
    service = "turn_on" if on else "turn_off"
    if _call_service("switch", service,
                     {"entity_id": entity or config.HA_DEFAULT_SWITCH}):
        return f"Plug {'on' if on else 'off'}, Sir."
    return "I couldn't reach the smart plug."


def set_temperature(degrees, entity=None):
    """Set the thermostat target temperature."""
    if not configured():
        return _no_ha()
    try:
        temp = float(degrees)
    except (TypeError, ValueError):
        return "What temperature should I set, Sir?"
    if _call_service("climate", "set_temperature",
                     {"entity_id": entity or config.HA_DEFAULT_CLIMATE,
                      "temperature": temp}):
        return f"Thermostat set to {round(temp)} degrees."
    return "I couldn't reach the thermostat."


def all_devices_off():
    """Turn off the default light and switch (used by sleep mode)."""
    if not configured():
        return _no_ha()
    lights_off()
    switch(False)
    return "All devices off."
