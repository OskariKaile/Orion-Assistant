"""Universal Hub server.

A small localhost-only HTTP server that powers Orion's dashboard UI.
Read-only in v1: every endpoint reads from existing modules (telemetry,
agenda, reminders, voice_id, health_check) or JSONL files (stats.jsonl,
debug_audio/, tts_debug/, conversation_archive.jsonl). No writes.

Built on stdlib `http.server` to avoid pulling FastAPI/uvicorn into
requirements.txt. The SPA in `hub/` connects to `/api/...` for data and
`/events` for live updates (Server-Sent Events).

Owner-only by design: the listener binds to 127.0.0.1, so nothing on the
LAN can reach it.
"""
from __future__ import annotations

import datetime as _dt
import http.server
import json
import os
import queue
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path

import _paths
import config


_HERE = Path(_paths.ROOT)  # project root: where stats.jsonl, hub/, images/, etc live
_HUB_DIR = _HERE / "hub"

_server = None
_server_thread = None
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()

# Set by orion.py once the Orion instance exists; used by /api/actions/*
# so they can drive mute, voice-lock, and clear-chat through the same
# methods the tray uses.
_orion_ref = None


def attach_orion(orion) -> None:
    global _orion_ref
    _orion_ref = orion


# ---------------------------------------------------------------------------
# SSE pub/sub. Anything in Orion can call publish() to push a live event.
# ---------------------------------------------------------------------------

def publish(event: str, data: dict) -> None:
    """Broadcast an event to every connected hub client. Safe to call from
    any thread; never raises."""
    payload = {"event": event, "data": data, "ts": time.time()}
    try:
        msg = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=256)
    with _subscribers_lock:
        _subscribers.append(q)
    return q


def _unsubscribe(q: queue.Queue) -> None:
    with _subscribers_lock:
        if q in _subscribers:
            _subscribers.remove(q)


# ---------------------------------------------------------------------------
# Data helpers -- thin wrappers that read what already exists.
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _safe_call(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def _active_model() -> str:
    """Model name of the backend currently answering questions."""
    try:
        import about
        return about.active_model()
    except Exception:
        return getattr(config, "GEMINI_MODEL", "")


def _telemetry_rows(days: int = 14) -> list[dict]:
    try:
        import telemetry
        rows = telemetry.read_all()
    except Exception:
        rows = _read_jsonl(_HERE / getattr(config, "TELEMETRY_FILE", "stats.jsonl"))
    if days <= 0:
        return rows
    cutoff = time.time() - days * 86400
    return [r for r in rows if (r.get("ts") or 0) >= cutoff]


def _dashboard() -> dict:
    rows = _telemetry_rows(days=1)
    total = len(rows)
    totals = [r.get("total_ms") or 0 for r in rows if r.get("total_ms")]
    avg = round(sum(totals) / len(totals)) if totals else 0
    actives = []
    for r in rows:
        synth = r.get("tts_synth_ms") or r.get("tts_ms") or 0
        a = (r.get("stt_ms") or 0) + (r.get("llm_ms") or 0) \
            + (r.get("action_ms") or 0) + synth
        if a:
            actives.append(a)
    avg_active = round(sum(actives) / len(actives)) if actives else 0
    cores = []
    for r in rows:
        synth = r.get("tts_synth_ms") or 0
        # Fall back to legacy combined tts_ms only when synth wasn't measured
        # separately. (tts_play_ms is reply playback length, not work.)
        if not synth:
            synth = r.get("tts_ms") or 0
        c = (r.get("stt_ms") or 0) + (r.get("llm_ms") or 0) + synth
        if c:
            cores.append(c)
    avg_core = round(sum(cores) / len(cores)) if cores else 0
    tokens_in = sum((r.get("tokens_in") or 0) for r in rows)
    tokens_out = sum((r.get("tokens_out") or 0) for r in rows)
    last = rows[-1] if rows else None

    voice_state = "unknown"
    voice_enrolled = False
    try:
        import voice_id
        voice_state = voice_id.get_mode()
        voice_enrolled = voice_id.is_enrolled()
    except Exception:
        pass

    return {
        "turns_today": total,
        "avg_latency_ms": avg,
        "avg_active_ms": avg_active,
        "avg_core_ms": avg_core,
        "tokens_in_today": tokens_in,
        "tokens_out_today": tokens_out,
        "voice_state": voice_state,
        "voice_enrolled": voice_enrolled,
        "last_turn": last,
        "model": _active_model(),
        "uptime_seconds": int(time.time() - _BOOT_TS),
    }


def _telemetry_summary(days: int) -> dict:
    rows = _telemetry_rows(days=days)
    rows_sorted = sorted(rows, key=lambda r: r.get("ts") or 0)
    return {
        "rows": rows_sorted,
        "count": len(rows_sorted),
        "range_days": days,
    }


def _agenda_payload(when: str) -> dict:
    try:
        import agenda
        snap = agenda.snapshot(when)
    except Exception:
        snap = {"events": [], "reminders": [], "todos": [], "start": None, "end": None}
    return {
        "when": when,
        "events": [_event_for_json(e) for e in snap.get("events") or []],
        "reminders": [_reminder_for_json(r) for r in snap.get("reminders") or []],
        "todos": list(snap.get("todos") or []),
        "spoken": _safe_call(lambda: __import__("agenda").spoken_summary(when),
                             default=""),
    }


def _event_for_json(ev: dict) -> dict:
    out = dict(ev)
    for k in ("start", "end"):
        v = out.get(k)
        if isinstance(v, _dt.datetime):
            out[k] = v.isoformat()
    return out


def _reminder_for_json(r: dict) -> dict:
    out = {k: v for k, v in r.items() if not k.startswith("_")}
    return out


def _health_payload() -> dict:
    try:
        import health_check as hc
    except Exception:
        return {"summary": "health module unavailable", "checks": []}
    checks = [
        ("Microphone", hc._check_mic()),
        ("Speakers", hc._check_speakers()),
        ("Wake model", hc._check_wake_model()),
        ("Voice profile", hc._check_voice_profile()),
        ("Gemini", hc._check_gemini()),
        ("Email", hc._check_email()),
        ("Calendar", _safe_call(
            lambda: __import__("calendar_sync").status(),
            default="unavailable")),
    ]
    pc = hc._check_pc()
    bad = ("missing", "error", "unreachable", "no microphone",
           "no output", "not installed", "unknown", "not enrolled",
           "not configured", "unavailable", "not set up", "expired",
           "reconnect")
    def status(v):
        low = (v or "").lower()
        return "bad" if any(m in low for m in bad) else "ok"
    return {
        "orion": [{"label": l, "value": v, "status": status(v)} for l, v in checks],
        "pc": [{"label": l, "value": v} for l, v in pc],
    }


def _debug_audio_list() -> list[dict]:
    path = _HERE / getattr(config, "REPLAY_DEBUG_DIR", "debug_audio") / "index.jsonl"
    rows = _read_jsonl(path)
    for i, r in enumerate(rows):
        r["idx"] = i
    return rows


def _tts_debug_list() -> list[dict]:
    path = _HERE / getattr(config, "TTS_DEBUG_DIR", "tts_debug") / "index.jsonl"
    rows = _read_jsonl(path)
    for i, r in enumerate(rows):
        r["idx"] = i
    return rows


def _conversation_archive() -> list[dict]:
    rows = _read_jsonl(_HERE / getattr(config, "ARCHIVE_FILE",
                                       "conversation_archive.jsonl"))
    # Strip the embedding vectors -- huge and useless to the UI.
    for r in rows:
        r.pop("embedding", None)
    return rows


def _conversation_recap() -> dict:
    path = _HERE / getattr(config, "RECAP_FILE", "conversation_recap.json")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _memory_payload() -> dict:
    """Orion's persistent facts about the user + the rolling recap.

    Reads through the live `memory` module so what the hub shows matches what
    Orion currently has in memory (its in-process cache), not just what's on
    disk. Falls back to the JSON files if the module can't be imported.
    """
    facts: list[str] = []
    recap = ""
    try:
        import memory
        facts = memory.facts()
        # facts() is empty until memory.load() runs; if Orion hasn't loaded
        # yet, pull straight from disk so the hub still shows them.
        if not facts:
            try:
                with (_HERE / getattr(config, "MEMORY_FILE", "memory.json")).open(
                        "r", encoding="utf-8") as f:
                    facts = [str(x).strip() for x in (json.load(f).get("facts") or [])
                             if str(x).strip()]
            except (OSError, ValueError):
                pass
        recap = memory.recap() or _conversation_recap().get("recap", "")
    except Exception:
        recap = _conversation_recap().get("recap", "")
    return {"facts": facts, "recap": recap}


_SETTINGS_KEYS = [
    # behavior
    "TELEMETRY", "MOOD_AWARENESS", "CONTEXT_AWARENESS", "REPLAY_DEBUG",
    "TTS_DEBUG", "FUNCTION_CALLING", "AI_FIRST", "HUD_TOOL_CHIPS",
    "WEB_SEARCH", "AUTO_MEMORY",
    "DAILY_BRIEFING", "PROACTIVE_ALERTS", "PUSH_TO_TALK",
    "PUSH_TO_TALK_HOTKEY", "THINKING_FILLERS", "BARGE_IN",
    # voice
    "TTS_ENGINE", "PIPER_VOICE", "PIPER_LENGTH_SCALE", "MOONSHINE_MODEL",
    "STT_ENGINE", "TTS_VOLUME",
    # security
    "VOICE_VERIFICATION", "VOICE_THRESHOLD",
    # agenda
    "END_OF_DAY_NUDGE", "END_OF_DAY_HOUR", "END_OF_DAY_EARLY_HOUR",
    "REMINDER_CALENDAR_SYNC",
    # model
    "LLM_BACKEND", "LLM_IMAGE_BACKEND", "LLM_SEARCH_BACKEND", "LLM_FALLBACK",
    "DEEPSEEK_MODEL", "GEMINI_MODEL", "GROK_MODEL",
    # hub
    "HUB_AUTOSTART", "HUB_PORT", "HUB_OPEN_ON_START",
]


def _piper_voices() -> list[str]:
    vdir = _HERE / getattr(config, "PIPER_VOICE_DIR", "voices")
    if not vdir.exists():
        return []
    return sorted(p.stem.replace(".onnx", "") for p in vdir.glob("*.onnx"))


_ENUMS = {
    "STT_ENGINE":   ["whisper", "google"],
    "TTS_ENGINE":   ["piper", "pyttsx3"],
    "MOONSHINE_MODEL": ["tiny", "base"],
    "MOONSHINE_PRECISION": ["float", "quantized"],
    "WHISPER_DEVICE": ["cpu", "cuda"],
    "WHISPER_COMPUTE_TYPE": ["int8", "float16", "float32"],
    # Pick "gemini" here for Gemini-only mode -- it covers images and web
    # search itself, so the two hand-off backends stop being used.
    "LLM_BACKEND": ["deepseek", "gemini", "grok"],
    "LLM_IMAGE_BACKEND": ["gemini", "grok"],
    "LLM_SEARCH_BACKEND": ["gemini", "grok"],
    "LLM_FALLBACK": ["", "gemini", "grok", "deepseek"],
}


def _settings_payload() -> dict:
    try:
        import config_overrides as _ov
        overrides = _ov.load()
        hot = lambda k: _ov.is_hot(k)
    except Exception:
        overrides = {}
        hot = lambda k: False
    voices = _piper_voices()
    items = []
    for key in _SETTINGS_KEYS:
        val = getattr(config, key, None)
        if not isinstance(val, (str, int, float, bool)) and val is not None:
            continue
        kind = ("bool" if isinstance(val, bool)
                else "number" if isinstance(val, (int, float))
                else "string")
        meta = {
            "key": key,
            "value": val,
            "type": kind,
            "hot": hot(key),
            "overridden": key in overrides,
        }
        # Attach option lists where we have them.
        if key == "PIPER_VOICE" and voices:
            meta["enum"] = voices
        elif key in _ENUMS:
            meta["enum"] = _ENUMS[key]
        items.append(meta)
    return {"settings": items, "overrides": overrides}


def _skills_payload() -> dict:
    """List built-in tools (with enabled state) and any user skills."""
    try:
        import tools as _tools
        import config_overrides as _ov
        disabled = _ov.disabled_tools()
        items = []
        for t in _tools.TOOLS:
            name = t.get("name") or ""
            items.append({
                "name": name,
                "description": t.get("description", ""),
                "builtin": True,
                "enabled": name not in disabled,
            })
    except Exception:
        items = []
    return {"tools": items, "skills_dir_present": (_HERE / "skills").exists()}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_BOOT_TS = time.time()


class _Handler(http.server.BaseHTTPRequestHandler):
    # Silence the default per-request access log -- noisy on the console.
    def log_message(self, fmt, *args):  # noqa: D401
        return

    # --- helpers ---
    def _send_json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, code: int = 200, ctype: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._send_text("not found", code=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Lightly cache static assets; index.html stays uncached so edits
        # show up on reload.
        if path.name == "index.html":
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    # --- write guard ---
    def _is_local(self) -> bool:
        host, _ = self.client_address[0], self.client_address[1]
        return host in ("127.0.0.1", "::1", "localhost")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    # --- routing ---
    def do_GET(self):  # noqa: N802
        try:
            url = urllib.parse.urlparse(self.path)
            path = url.path or "/"
            params = urllib.parse.parse_qs(url.query)

            if path == "/" or path == "/index.html":
                return self._send_file(_HUB_DIR / "index.html", "text/html")
            if path == "/app.js":
                return self._send_file(_HUB_DIR / "app.js", "application/javascript")
            if path == "/styles.css":
                return self._send_file(_HUB_DIR / "styles.css", "text/css")
            if path == "/favicon.ico":
                return self._send_file(_HERE / "images" / "orion-favicon.ico", "image/x-icon")
            if path == "/favicon.png":
                return self._send_file(_HERE / "images" / "orion-favicon-32.png", "image/png")
            if path.startswith("/favicon-") and path.endswith(".png"):
                size = path[len("/favicon-"):-len(".png")]
                if size in {"16", "32", "48", "64", "128", "180"}:
                    return self._send_file(_HERE / "images" / f"orion-favicon-{size}.png", "image/png")
            if path == "/logo.png":
                return self._send_file(_HERE / "images" / "orion-logo-icon.png", "image/png")
            if path == "/logo-text.png":
                return self._send_file(_HERE / "images" / "orion-logo-icon-text.png", "image/png")

            if path == "/api/dashboard":
                return self._send_json(_dashboard())
            if path == "/api/telemetry":
                days = int((params.get("days", ["14"])[0]) or 14)
                return self._send_json(_telemetry_summary(days))
            if path == "/api/agenda":
                when = (params.get("when", ["today"])[0]) or "today"
                return self._send_json(_agenda_payload(when))
            if path == "/api/health":
                return self._send_json(_health_payload())
            if path == "/api/debug/audio":
                return self._send_json({"entries": _debug_audio_list()})
            if path == "/api/debug/tts":
                return self._send_json({"entries": _tts_debug_list()})
            if path.startswith("/api/debug/audio/file/"):
                name = path.rsplit("/", 1)[-1]
                return self._serve_wav(
                    _HERE / getattr(config, "REPLAY_DEBUG_DIR", "debug_audio") / name)
            if path.startswith("/api/debug/tts/file/"):
                name = path.rsplit("/", 1)[-1]
                return self._serve_wav(
                    _HERE / getattr(config, "TTS_DEBUG_DIR", "tts_debug") / name)
            if path == "/api/settings":
                return self._send_json(_settings_payload())
            if path == "/api/skills":
                return self._send_json(_skills_payload())
            if path == "/api/conversation":
                return self._send_json({
                    "recap": _conversation_recap(),
                    "archive": _conversation_archive(),
                })
            if path == "/api/memory":
                return self._send_json(_memory_payload())
            if path == "/api/reminders":
                try:
                    import reminders
                    items = reminders.list_pending()
                except Exception:
                    items = []
                return self._send_json({"reminders": items})
            if path == "/api/logs":
                limit = int((params.get("limit", ["200"])[0]) or 200)
                try:
                    import logbuf
                    entries = logbuf.lines(limit)
                except Exception:
                    entries = []
                return self._send_json({"lines": entries})

            if path == "/events":
                return self._serve_sse()

            return self._send_text("not found", code=404)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected; nothing to do.
            return
        except Exception as exc:  # pragma: no cover -- safety net
            try:
                self._send_text(f"internal error: {exc}", code=500)
            except Exception:
                pass

    # --- write verbs ---
    def do_POST(self):    # noqa: N802
        return self._handle_write("POST")

    def do_PATCH(self):   # noqa: N802
        return self._handle_write("PATCH")

    def do_DELETE(self):  # noqa: N802
        return self._handle_write("DELETE")

    def _handle_write(self, method: str):
        if not self._is_local():
            return self._send_json({"ok": False, "error": "forbidden"}, code=403)
        try:
            url = urllib.parse.urlparse(self.path)
            path = url.path or "/"
            body = self._read_json()
            return self._dispatch_write(method, path, body)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover
            return self._send_json({"ok": False, "error": str(exc)}, code=500)

    def _dispatch_write(self, method: str, path: str, body: dict):
        # ----- settings -----
        if path == "/api/settings" and method == "PATCH":
            return self._set_setting(body)
        if path.startswith("/api/settings/") and method == "DELETE":
            key = path.rsplit("/", 1)[-1]
            return self._clear_setting(key)

        # ----- agenda -----
        if path == "/api/agenda/todo" and method == "POST":
            return self._agenda_add_todo(body)
        if path == "/api/agenda/todo/complete" and method == "POST":
            return self._agenda_complete_todo(body)
        if path == "/api/agenda/todo/clear" and method == "POST":
            try:
                import agenda
                msg = agenda.todos_clear()
                publish("agenda_changed", {})
                return self._send_json({"ok": True, "message": msg})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, code=500)
        if path.startswith("/api/reminders/") and method == "DELETE":
            rid_or_match = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            return self._reminder_cancel(rid_or_match)
        if path == "/api/reminders/clear" and method == "POST":
            try:
                import reminders
                reminders.clear_all()
                publish("agenda_changed", {})
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, code=500)
        if path == "/api/reminders/snooze" and method == "POST":
            try:
                import reminders
                msg = reminders.snooze(int(body.get("minutes") or 10))
                publish("agenda_changed", {})
                return self._send_json({"ok": True, "message": msg})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, code=500)
        if path == "/api/events/cancel" and method == "POST":
            try:
                import agenda
                msg = agenda.cancel_event(body.get("match") or "")
                publish("agenda_changed", {})
                return self._send_json({"ok": True, "message": msg})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, code=500)

        # ----- memory (persistent facts + recap) -----
        if path == "/api/memory/fact" and method == "POST":
            return self._memory_add_fact(body)
        if path == "/api/memory/fact" and method == "PATCH":
            return self._memory_update_fact(body)
        if path.startswith("/api/memory/fact/") and method == "DELETE":
            return self._memory_delete_fact(path.rsplit("/", 1)[-1])
        if path == "/api/memory/recap" and method == "PATCH":
            return self._memory_set_recap(body)

        # ----- debug audio -----
        if path == "/api/debug/audio/clear" and method == "POST":
            return self._debug_clear("audio")
        if path == "/api/debug/tts/clear" and method == "POST":
            return self._debug_clear("tts")
        if path.startswith("/api/debug/audio/") and method == "DELETE":
            return self._debug_delete("audio", path.rsplit("/", 1)[-1])
        if path.startswith("/api/debug/tts/") and method == "DELETE":
            return self._debug_delete("tts", path.rsplit("/", 1)[-1])

        # ----- skills -----
        if path.startswith("/api/skills/") and path.endswith("/toggle") and method == "POST":
            name = path[len("/api/skills/"):-len("/toggle")]
            return self._skill_toggle(name, body)

        # ----- actions -----
        if path == "/api/actions/mute" and method == "POST":
            return self._action_mute(body)
        if path == "/api/actions/voice_lock" and method == "POST":
            return self._action_voice_lock(body)
        if path == "/api/actions/new_chat" and method == "POST":
            return self._action_new_chat()
        if path == "/api/actions/mic_test" and method == "POST":
            return self._action_mic_test()
        if path == "/api/actions/enroll" and method == "POST":
            return self._action_enroll_voice()
        if path == "/api/actions/reconnect_calendar" and method == "POST":
            return self._action_reconnect_calendar()
        if path == "/api/actions/health" and method == "POST":
            return self._send_json({"ok": True, "data": _health_payload()})
        if path == "/api/actions/restart" and method == "POST":
            return self._action_restart()
        if path == "/api/logs/clear" and method == "POST":
            try:
                import logbuf
                logbuf.clear()
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, code=500)

        return self._send_json({"ok": False, "error": "not found"}, code=404)

    # ----- write handlers -----
    def _set_setting(self, body: dict):
        try:
            import config_overrides as _ov
            key = body.get("key")
            value = body.get("value")
            # Coerce numeric strings; the UI sometimes posts "0.85".
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.lower() in ("true", "false"):
                    value = stripped.lower() == "true"
                else:
                    try:
                        if "." in stripped:
                            value = float(stripped)
                        elif stripped.lstrip("-").isdigit():
                            value = int(stripped)
                    except ValueError:
                        pass
            ok, msg = _ov.set_override(key, value)
            if ok:
                publish("settings_changed", {"key": key})
            return self._send_json({"ok": ok, "message": msg})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _clear_setting(self, key: str):
        try:
            import config_overrides as _ov
            ok, msg = _ov.clear_override(key)
            if ok:
                publish("settings_changed", {"key": key})
            return self._send_json({"ok": ok, "message": msg})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _memory_add_fact(self, body: dict):
        text = (body.get("text") or "").strip()
        if not text:
            return self._send_json({"ok": False, "error": "missing text"}, code=400)
        try:
            import memory
            added = memory.remember(text)
            publish("memory_changed", {})
            return self._send_json({"ok": added,
                                    "message": "added" if added else "duplicate"})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _memory_update_fact(self, body: dict):
        try:
            index = int(body.get("index"))
        except (TypeError, ValueError):
            return self._send_json({"ok": False, "error": "bad index"}, code=400)
        text = (body.get("text") or "").strip()
        if not text:
            return self._send_json({"ok": False, "error": "missing text"}, code=400)
        try:
            import memory
            ok = memory.update_fact(index, text)
            if ok:
                publish("memory_changed", {})
            return self._send_json({"ok": ok,
                                    "error": None if ok else "bad index or duplicate"})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _memory_delete_fact(self, idx_str: str):
        try:
            index = int(idx_str)
        except (TypeError, ValueError):
            return self._send_json({"ok": False, "error": "bad index"}, code=400)
        try:
            import memory
            ok = memory.delete_fact(index)
            if ok:
                publish("memory_changed", {})
            return self._send_json({"ok": ok})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _memory_set_recap(self, body: dict):
        try:
            import memory
            memory.set_recap(body.get("recap") or "")
            publish("memory_changed", {})
            return self._send_json({"ok": True})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _agenda_add_todo(self, body: dict):
        text = (body.get("text") or "").strip()
        if not text:
            return self._send_json({"ok": False, "error": "missing text"}, code=400)
        try:
            import agenda
            msg = agenda.todos_add(text)
            publish("agenda_changed", {})
            return self._send_json({"ok": True, "message": msg})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _agenda_complete_todo(self, body: dict):
        match = (body.get("match") or "").strip()
        if not match:
            return self._send_json({"ok": False, "error": "missing match"}, code=400)
        try:
            import agenda
            msg = agenda.todos_complete(match)
            publish("agenda_changed", {})
            return self._send_json({"ok": True, "message": msg})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _reminder_cancel(self, identifier: str):
        try:
            import reminders
            # The Hub's delete button sends the row's own id. Route those
            # through the exact-id path so a click deletes that row and
            # nothing else, whatever its wording happens to share.
            if identifier.startswith("r_"):
                msg = reminders.cancel_matching("", rid=identifier)
            else:
                msg = reminders.cancel_matching(identifier)
            publish("agenda_changed", {})
            return self._send_json({"ok": True, "message": msg})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _debug_clear(self, kind: str):
        try:
            mod = __import__("replay_debug" if kind == "audio" else "tts_debug")
            mod._cli_clear() if False else None  # noop: _cli_clear prints
            # Use the module's internals directly to avoid the print.
            with mod._LOCK:
                entries = mod._read_entries()
                for e in entries:
                    mod._delete_wav(e.get("file"))
                if mod._INDEX.exists():
                    mod._INDEX.unlink()
            return self._send_json({"ok": True})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _debug_delete(self, kind: str, idx_str: str):
        try:
            idx = int(idx_str)
            mod = __import__("replay_debug" if kind == "audio" else "tts_debug")
            with mod._LOCK:
                entries = mod._read_entries()
                if not 0 <= idx < len(entries):
                    return self._send_json({"ok": False, "error": "bad index"}, code=400)
                e = entries.pop(idx)
                mod._delete_wav(e.get("file"))
                mod._write_entries(entries)
            return self._send_json({"ok": True})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _skill_toggle(self, name: str, body: dict):
        enabled = bool(body.get("enabled", True))
        try:
            import config_overrides as _ov
            ok, msg = _ov.set_tool_disabled(name, not enabled)
            return self._send_json({"ok": ok, "message": msg})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _action_mute(self, body: dict):
        if _orion_ref is None:
            return self._send_json({"ok": False, "error": "orion not attached"}, code=503)
        on = bool(body.get("on", True))
        try:
            _orion_ref.speaker.set_muted(on)
            if on:
                _orion_ref.set_state("muted")
            else:
                _orion_ref.set_state("idle")
            return self._send_json({"ok": True, "muted": on})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _action_voice_lock(self, body: dict):
        locked = bool(body.get("locked", True))
        try:
            import voice_id
            voice_id.set_mode("locked" if locked else "open")
            publish("voice_state", {"mode": voice_id.get_mode()})
            return self._send_json({"ok": True, "mode": voice_id.get_mode()})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _action_new_chat(self):
        try:
            if _orion_ref is not None and hasattr(_orion_ref, "speaker"):
                # Use the same handler the tray uses.
                import llm_router
                llm_router.clear_chat()
                return self._send_json({"ok": True})
            return self._send_json({"ok": False, "error": "orion not attached"}, code=503)
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _action_mic_test(self):
        """Capture ~2s of audio and report the RMS level so the user can
        confirm the mic is hearing them."""
        try:
            import speech_recognition as sr
            import audioop
            rec = sr.Recognizer()
            with sr.Microphone() as src:
                rec.adjust_for_ambient_noise(src, duration=0.3)
                audio = rec.record(src, duration=2.0)
            data = audio.get_raw_data()
            width = audio.sample_width
            rms = audioop.rms(data, width)
            return self._send_json({
                "ok": True, "rms": rms, "level": min(100, int(rms / 50)),
                "message": f"Captured 2s, RMS {rms}",
            })
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _action_enroll_voice(self):
        """Spawn the existing enroll_voice.py CLI in a new console."""
        try:
            import subprocess, sys
            script = _HERE / "setup" / "enroll_voice.py"
            if not script.exists():
                return self._send_json({"ok": False, "error": "enroll_voice.py missing"}, code=500)
            # Use the same Python that's running Orion.
            subprocess.Popen(
                [sys.executable, str(script)],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            return self._send_json({"ok": True, "message": "Enrollment opened in a new window."})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _action_restart(self):
        """Relaunch Orion: spawn a fresh process, then shut this one down.
        The response is sent first; the actual restart runs on a short
        timer so the HTTP reply isn't cut off mid-flight."""
        if _orion_ref is None or not hasattr(_orion_ref, "restart"):
            return self._send_json(
                {"ok": False, "error": "orion not attached"}, code=503)

        def _go():
            time.sleep(0.5)  # let the HTTP response flush to the client
            try:
                _orion_ref.restart()
            except Exception:
                pass
        threading.Thread(target=_go, daemon=True).start()
        return self._send_json(
            {"ok": True, "message": "Restarting -- back in a few seconds."})

    def _action_reconnect_calendar(self):
        """Spawn setup/setup_calendar.py in a new console. It opens the
        Google consent screen in the browser and saves a fresh token.json
        when the user clicks Allow -- the easy 'my calendar dropped again'
        button."""
        try:
            import subprocess, sys
            script = _HERE / "setup" / "setup_calendar.py"
            if not script.exists():
                return self._send_json(
                    {"ok": False, "error": "setup_calendar.py missing"}, code=500)
            subprocess.Popen(
                [sys.executable, str(script)],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            return self._send_json({
                "ok": True,
                "message": "A browser window will open -- click Allow to "
                           "reconnect Google Calendar.",
            })
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, code=500)

    def _serve_wav(self, path: Path) -> None:
        # Guard against `..` traversal: resolve and check the parent.
        try:
            resolved = path.resolve()
        except OSError:
            return self._send_text("not found", code=404)
        allowed_parents = {
            (_HERE / getattr(config, "REPLAY_DEBUG_DIR", "debug_audio")).resolve(),
            (_HERE / getattr(config, "TTS_DEBUG_DIR", "tts_debug")).resolve(),
        }
        if resolved.parent not in allowed_parents:
            return self._send_text("forbidden", code=403)
        if not resolved.exists():
            return self._send_text("not found", code=404)
        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(data)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = _subscribe()
        try:
            # Initial hello so the client knows it's connected.
            self.wfile.write(b"event: ready\ndata: {}\n\n")
            self.wfile.flush()
            last_ping = time.monotonic()
            while True:
                try:
                    msg = q.get(timeout=1.0)
                except queue.Empty:
                    msg = None
                if msg is not None:
                    payload = f"data: {msg}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                # Heartbeat every 15s so proxies / sleeping tabs don't
                # silently drop the stream.
                if time.monotonic() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            _unsubscribe(q)


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def start(port: int | None = None) -> bool:
    """Start the hub server in a background thread. Idempotent. Returns
    True if the server is running (whether already or just started)."""
    global _server, _server_thread
    if _server is not None:
        return True
    port = int(port or getattr(config, "HUB_PORT", 8765))
    try:
        _server = _ThreadingServer(("127.0.0.1", port), _Handler)
    except OSError:
        _server = None
        return False
    _server_thread = threading.Thread(
        target=_server.serve_forever, name="hub-server", daemon=True)
    _server_thread.start()
    return True


def stop() -> None:
    global _server, _server_thread
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
    _server = None
    _server_thread = None


def url() -> str:
    return f"http://127.0.0.1:{getattr(config, 'HUB_PORT', 8765)}/"


def running() -> bool:
    return _server is not None
