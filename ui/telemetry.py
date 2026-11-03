"""Per-turn telemetry. One JSONL row per handled command.

Schema (every field is in milliseconds unless noted):
    ts/iso         when the turn started
    query          truncated to 200 chars
    reply          what Orion spoke back, truncated to 1000 chars
    speaker        "owner" | "other"  (only "owner" is logged)
    source         "wake" | "follow-up" | "push-to-talk"
    category       "fast.agenda" | "ai.text" | "ai.tool:set_reminder" | ...
    tool           specific tool name when applicable
    mood           "neutral" | "stressed"
    listen_ms      mic capture: time spent recording the user's speech
    stt_ms         Whisper/Google transcribing the captured audio
    llm_ms         Gemini round-trip (sum across calls if a tool is called)
    action_ms      between-LLM-and-reply work: tool execution, song capture,
                   web lookups, etc. (anything the handler did itself)
    tts_synth_ms   voice rendering -- Piper generating audio bytes
    tts_play_ms    Orion actually speaking the reply out loud
    tts_ms         legacy combined field (tts_synth_ms + tts_play_ms)
    total_ms       end-to-end, listen through reply finished
    tokens_in      Gemini prompt tokens
    tokens_out     Gemini response tokens
    model          model id used for this turn

Render with `python orion_stats.py`.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config
try:
    import mood as mood_mod
except Exception:
    mood_mod = None

_FILE = Path(getattr(config, "TELEMETRY_FILE", "stats.jsonl"))
_LOCK = threading.Lock()
_local = threading.local()


def enabled() -> bool:
    return bool(getattr(config, "TELEMETRY", True))


def _current() -> dict | None:
    return getattr(_local, "turn", None)


def start_turn(query: str, speaker: str = "owner", source: str = "wake",
               listen_ms: int = 0, stt_ms: int = 0) -> None:
    """Begin a new turn record. Safe to call even when disabled."""
    if not enabled():
        return
    # Log every turn the assistant actually handles. Non-owner phrases used to
    # be dropped silently here, which made follow-up turns vanish from the hub
    # whenever voice verification misidentified the speaker. The row keeps the
    # `speaker` field so the UI can still filter if needed.
    now = time.time()
    mood_label = "neutral"
    if mood_mod is not None:
        try:
            mood_label = mood_mod.detect_mood(query or "") or "neutral"
        except Exception:
            pass
    _local.turn = {
        "_start": time.monotonic(),
        "ts": now,
        "iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "query": (query or "")[:200],
        "reply": "",
        "speaker": speaker,
        "source": source,
        "category": "unknown",
        "tool": None,
        "mood": mood_label,
        "listen_ms": int(listen_ms or 0),
        "stt_ms": int(stt_ms or 0),
        "llm_ms": 0,
        "action_ms": 0,
        "tts_synth_ms": 0,
        "tts_play_ms": 0,
        "tts_ms": 0,
        "total_ms": 0,
        "tokens_in": None,
        "tokens_out": None,
        "model": None,
    }


def set_category(name: str) -> None:
    t = _current()
    if t is not None and name:
        t["category"] = name


def set_tool(name: str | None) -> None:
    t = _current()
    if t is not None:
        t["tool"] = name
        if name and t.get("category") in (None, "unknown"):
            t["category"] = f"ai.tool:{name}"


def add(field: str, ms: int | float) -> None:
    """Add to a numeric field (e.g. accumulate llm_ms across two calls)."""
    t = _current()
    if t is not None and field in t and isinstance(t[field], (int, float)):
        t[field] = int(t[field]) + int(ms)


_REPLY_CAP = 1000


def append_reply(text: str) -> None:
    """Append spoken text to the current turn's `reply` (capped)."""
    t = _current()
    if t is None or not text:
        return
    cur = t.get("reply") or ""
    if len(cur) >= _REPLY_CAP:
        return
    sep = " " if cur and not cur.endswith((" ", "\n")) else ""
    t["reply"] = (cur + sep + text)[:_REPLY_CAP]


def set_field(**fields: Any) -> None:
    t = _current()
    if t is None:
        return
    for k, v in fields.items():
        t[k] = v


def end_turn() -> None:
    """Finalise the current turn and append it to the JSONL file."""
    t = _current()
    _local.turn = None
    if t is None:
        return
    total_ms = int((time.monotonic() - t.pop("_start")) * 1000)
    # total_ms includes everything from start_turn (after the user's phrase
    # was already captured) through end_turn. Add listen + stt back in so the
    # number on the dashboard reflects the full wake-to-reply time.
    t["total_ms"] = total_ms + int(t["listen_ms"]) + int(t["stt_ms"])
    # Combined tts_ms = synth + play (kept for the dashboard's "5. TTS"
    # legacy stage). When we measured the two halves directly, use them.
    measured_tts = int(t["tts_synth_ms"]) + int(t["tts_play_ms"])
    if measured_tts > 0:
        t["tts_ms"] = measured_tts
    else:
        # No measurement (turn ended without _speak firing -- e.g. muted).
        # Derive a best-effort number from the leftover.
        leftover = total_ms - int(t["llm_ms"]) - int(t["action_ms"])
        t["tts_ms"] = max(0, leftover)
    try:
        with _LOCK:
            with _FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
    except Exception:
        # Telemetry must never break the main loop.
        pass


def read_all() -> list[dict]:
    if not _FILE.exists():
        return []
    out: list[dict] = []
    with _FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
