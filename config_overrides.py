"""Read/write `config_overrides.json` — the Hub's edit layer over config.py.

config.py loads this once at import to apply persisted edits. The Hub's
write endpoints call `set_override()` to update both the file and the
live `config` module, so changes take effect for hot-applied keys without
needing a restart.

Keys with sensitive secrets are HARD-LOCKED and rejected here.

Layout of the JSON:
    { "MOOD_AWARENESS": false,
      "PIPER_LENGTH_SCALE": 0.9,
      "disabled_tools": ["set_reminder"] }
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PATH = _HERE / "config_overrides.json"
_LOCK = threading.Lock()

# Secrets and a few keys that don't make sense to edit through a UI.
LOCKED = {
    "GEMINI_API_KEY", "GROK_API_KEY", "DEEPSEEK_API_KEY",
    "HOME_ASSISTANT_TOKEN",
    "GEMINI_API_KEY".lower(),
}

# Keys whose values are read live (every use), so an override applies
# immediately without restarting Orion. Anything not in this set still
# updates in-memory, but the consumer cached it at boot — surface a
# "restart required" hint in the UI for those.
HOT_APPLY = {
    "TELEMETRY", "MOOD_AWARENESS", "CONTEXT_AWARENESS", "REPLAY_DEBUG",
    "TTS_DEBUG", "FUNCTION_CALLING", "WEB_SEARCH", "AUTO_MEMORY",
    "THINKING_FILLERS", "BARGE_IN", "TTS_VOLUME", "PIPER_LENGTH_SCALE",
    "PIPER_NOISE_SCALE", "PIPER_NOISE_W_SCALE",
    "VOICE_VERIFICATION", "VOICE_THRESHOLD",
    "END_OF_DAY_NUDGE", "END_OF_DAY_HOUR", "END_OF_DAY_EARLY_HOUR",
    "REMINDER_CALENDAR_SYNC",
    "GEMINI_MODEL", "GROK_MODEL", "DEEPSEEK_MODEL",
    "LLM_BACKEND", "LLM_IMAGE_BACKEND", "LLM_SEARCH_BACKEND", "LLM_FALLBACK",
    "HUB_OPEN_ON_START", "DAILY_BRIEFING",
    "PROACTIVE_ALERTS", "PROACTIVE_INTERVAL", "PROACTIVE_COOLDOWN",
    "WEB_SEARCH", "STT_TOLERANCE",
}


def load() -> dict:
    if not _PATH.exists():
        return {}
    try:
        with _PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_locked(data: dict) -> bool:
    tmp = _PATH.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _PATH)
        return True
    except OSError:
        return False


def apply_to_module(mod) -> None:
    """Apply persisted overrides to a config module's attributes."""
    data = load()
    for key, value in data.items():
        if key in LOCKED:
            continue
        # Don't create brand-new attributes -- only override known ones,
        # except for a tiny allowlist of hub-managed lists.
        if key in ("disabled_tools",) or hasattr(mod, key):
            setattr(mod, key, value)


def set_override(key: str, value) -> tuple[bool, str]:
    """Set or update one override. Returns (ok, message)."""
    if not isinstance(key, str) or not key:
        return False, "missing key"
    if key in LOCKED:
        return False, f"{key} is locked (edit secrets_local.py)"
    with _LOCK:
        data = load()
        data[key] = value
        if not _save_locked(data):
            return False, "could not write overrides file"
    # Live-update the in-memory config module.
    try:
        import config
        setattr(config, key, value)
    except Exception:
        pass
    return True, "ok"


def clear_override(key: str) -> tuple[bool, str]:
    """Remove one override and (best-effort) restore the file-level default."""
    with _LOCK:
        data = load()
        if key not in data:
            return True, "no override set"
        data.pop(key, None)
        if not _save_locked(data):
            return False, "could not write overrides file"
    # Re-import config without the override so the file-level default
    # is restored in memory. (importlib.reload preserves identity.)
    try:
        import importlib, config
        importlib.reload(config)
    except Exception:
        pass
    return True, "ok"


def disabled_tools() -> set[str]:
    data = load()
    items = data.get("disabled_tools") or []
    return {str(x) for x in items if isinstance(x, str)}


def set_tool_disabled(name: str, disabled: bool) -> tuple[bool, str]:
    if not name:
        return False, "missing tool name"
    with _LOCK:
        data = load()
        current = set(data.get("disabled_tools") or [])
        if disabled:
            current.add(name)
        else:
            current.discard(name)
        data["disabled_tools"] = sorted(current)
        if not _save_locked(data):
            return False, "could not write overrides file"
    return True, "ok"


def is_hot(key: str) -> bool:
    return key in HOT_APPLY


def overrides_file() -> Path:
    return _PATH
