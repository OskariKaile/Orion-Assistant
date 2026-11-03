"""System self-check for Orion.

Reports the status of Orion's own subsystems (mic, speakers, wake model,
voice profile, LLM backend reachability, email config) and the PC's basic
health (disk, RAM, CPU, battery). Designed to be spoken in one go --
short status lines, no markdown.
"""

import os
import socket

import _paths
import config


def _check_mic():
    try:
        import pyaudio
    except Exception:
        return "mic library not installed"
    try:
        pa = pyaudio.PyAudio()
        try:
            count = 0
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    count += 1
        finally:
            pa.terminate()
        if count == 0:
            return "no microphone detected"
        return f"OK ({count} input device{'s' if count != 1 else ''})"
    except Exception as e:
        return f"error: {e}"


def _check_speakers():
    try:
        import pyaudio
    except Exception:
        return "audio library not installed"
    try:
        pa = pyaudio.PyAudio()
        try:
            count = 0
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("maxOutputChannels", 0) > 0:
                    count += 1
        finally:
            pa.terminate()
        if count == 0:
            return "no output device"
        return f"OK ({count} output device{'s' if count != 1 else ''})"
    except Exception as e:
        return f"error: {e}"


def _check_wake_model():
    here = _paths.ROOT
    path = getattr(config, "WAKE_MODEL", "models/orion.onnx")
    full = path if os.path.isabs(path) else os.path.join(here, path)
    if os.path.exists(full):
        size_kb = os.path.getsize(full) // 1024
        return f"OK ({size_kb} KB)"
    return f"missing at {path}"


def _check_voice_profile():
    here = _paths.ROOT
    path = getattr(config, "VOICE_PROFILE_FILE", "voice_profile.json")
    full = path if os.path.isabs(path) else os.path.join(here, path)
    if os.path.exists(full):
        return "enrolled"
    return "not enrolled (run enroll_voice.py)"


# config.LLM_BACKEND value -> (spoken label, API host to ping).
_BRAIN_HOSTS = {
    "deepseek": ("DeepSeek", "api.deepseek.com"),
    "gemini":   ("Gemini", "generativelanguage.googleapis.com"),
    "grok":     ("Grok", "api.x.ai"),
}

# Which config setting holds each backend's API key, so the check can tell
# "the server is up" apart from "we have no way to talk to it".
_BRAIN_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "grok": "GROK_API_KEY",
}


def _has_key(name):
    """True when `name`'s API key is set, in config or the environment."""
    attr = _BRAIN_KEYS.get(name)
    if attr is None:
        return True  # unknown backend: nothing to check
    return bool((getattr(config, attr, "")
                 or os.environ.get(attr, "")).strip())


def _reachable(host):
    try:
        s = socket.create_connection((host, 443), timeout=3)
        s.close()
        return "reachable"
    except Exception as e:
        return f"unreachable ({e.__class__.__name__})"


def _check_brains():
    """(label, status) for the active brain, plus the image/search backend
    behind it when that's a different service."""
    backend = (getattr(config, "LLM_BACKEND", "deepseek") or "").strip().lower()
    out = []
    seen = set()
    for name in (backend,
                 getattr(config, "LLM_IMAGE_BACKEND", "gemini"),
                 getattr(config, "LLM_SEARCH_BACKEND", "gemini")):
        name = (name or "").strip().lower()
        entry = _BRAIN_HOSTS.get(name)
        if entry is None or entry[0] in seen:
            continue
        seen.add(entry[0])
        out.append((entry[0], _reachable(entry[1])))
    # A keyless main backend fails on every single turn and is invisible:
    # llm_router quietly hands the work to LLM_FALLBACK, so Orion keeps
    # answering and nothing says it isn't running on the brain you picked.
    if backend in _BRAIN_KEYS and not _has_key(backend):
        label = _BRAIN_HOSTS[backend][0]
        fallback = (getattr(config, "LLM_FALLBACK", "") or "").strip().lower()
        note = (f"no API key -- every request is falling back to "
                f"{_BRAIN_HOSTS[fallback][0]}"
                if fallback in _BRAIN_HOSTS and _has_key(fallback)
                else "no API key set")
        out.append((f"{label} (LLM_BACKEND)", note))
    return out or [("Brain", "no cloud backend configured")]


def _check_email():
    try:
        import email_reader
        return "configured" if email_reader.available() else "not configured"
    except Exception:
        return "unavailable"


def _check_pc():
    """Returns a list of (label, value) tuples for disk/RAM/CPU/battery."""
    out = []
    try:
        import psutil
    except Exception:
        return [("PC stats", "psutil not installed")]

    try:
        du = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
        free_gb = du.free / (1024 ** 3)
        pct_used = du.percent
        out.append(("disk",
                    f"{free_gb:.0f} GB free ({pct_used:.0f}% used)"))
    except Exception:
        out.append(("disk", "unknown"))

    try:
        vm = psutil.virtual_memory()
        used_gb = (vm.total - vm.available) / (1024 ** 3)
        total_gb = vm.total / (1024 ** 3)
        out.append(("RAM",
                    f"{used_gb:.1f} of {total_gb:.0f} GB ({vm.percent:.0f}%)"))
    except Exception:
        out.append(("RAM", "unknown"))

    try:
        cpu = psutil.cpu_percent(interval=0.3)
        out.append(("CPU", f"{cpu:.0f}%"))
    except Exception:
        out.append(("CPU", "unknown"))

    try:
        bat = psutil.sensors_battery()
        if bat is None:
            out.append(("battery", "no battery"))
        else:
            plug = "plugged in" if bat.power_plugged else "on battery"
            out.append(("battery", f"{bat.percent:.0f}% ({plug})"))
    except Exception:
        pass

    return out


def run_health_check():
    """Run every check and return a single spoken-style report string."""
    orion_side = [
        ("Microphone", _check_mic()),
        ("Speakers", _check_speakers()),
        ("Wake model", _check_wake_model()),
        ("Voice profile", _check_voice_profile()),
        *_check_brains(),
        ("Email", _check_email()),
    ]
    pc_side = _check_pc()

    lines = ["System check, Sir."]
    for label, value in orion_side:
        lines.append(f"{label}: {value}.")
    for label, value in pc_side:
        lines.append(f"{label.capitalize()}: {value}.")
    lines.append("All clear." if _all_ok(orion_side, pc_side)
                 else "That's the full report.")
    return " ".join(lines)


def _all_ok(orion_side, pc_side):
    bad_markers = ("missing", "error", "unreachable", "no microphone",
                   "no output", "not installed", "unknown", "not enrolled",
                   "not configured", "unavailable")
    for _, v in list(orion_side) + list(pc_side):
        low = v.lower()
        if any(m in low for m in bad_markers):
            return False
    return True
