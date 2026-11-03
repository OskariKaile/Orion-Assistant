"""Rolling capture of recent voice phrases for debugging STT.

Each captured phrase is saved as a WAV plus a JSONL sidecar entry
(timestamp, transcript, source). A rolling cap keeps the directory
small. Use the CLI to list and replay phrases:

    python replay_debug.py list
    python replay_debug.py play 3
    python replay_debug.py delete 3
    python replay_debug.py clear
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths  # noqa: F401

import config

_DIR = Path(getattr(config, "REPLAY_DEBUG_DIR", "debug_audio"))
_INDEX = _DIR / "index.jsonl"
_LOCK = threading.Lock()
_KEEP = int(getattr(config, "REPLAY_DEBUG_KEEP", 50))
_MAX_MB = float(getattr(config, "REPLAY_DEBUG_MAX_MB", 50))


def enabled() -> bool:
    return bool(getattr(config, "REPLAY_DEBUG", True))


def _ensure_dir() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)


def _read_entries() -> list[dict]:
    if not _INDEX.exists():
        return []
    out: list[dict] = []
    with _INDEX.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_entries(entries: list[dict]) -> None:
    tmp = _INDEX.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, _INDEX)


def _prune(entries: list[dict]) -> list[dict]:
    # Cap by count first.
    if len(entries) > _KEEP:
        for old in entries[:-_KEEP]:
            _delete_wav(old.get("file"))
        entries = entries[-_KEEP:]
    # Then cap by total bytes (drop oldest until under limit).
    limit = _MAX_MB * 1024 * 1024
    total = 0
    sizes: list[int] = []
    for e in entries:
        p = _DIR / e.get("file", "")
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        sizes.append(sz)
        total += sz
    i = 0
    while total > limit and i < len(entries):
        _delete_wav(entries[i].get("file"))
        total -= sizes[i]
        i += 1
    if i:
        entries = entries[i:]
    return entries


def _delete_wav(name: str | None) -> None:
    if not name:
        return
    try:
        (_DIR / name).unlink()
    except OSError:
        pass


def record(audio, text: str, source: str = "listen") -> None:
    """Save one captured phrase. Never raises.

    `audio` is a speech_recognition.AudioData. `text` is the transcript
    (may be empty if STT returned nothing -- still worth saving since
    "didn't understand me" is the bug we're chasing).
    """
    if not enabled():
        return
    try:
        wav_bytes = audio.get_wav_data()
    except Exception:
        return
    try:
        with _LOCK:
            _ensure_dir()
            now = time.time()
            stamp = datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S")
            # Add a millisecond suffix so back-to-back captures don't collide.
            ms = int((now - int(now)) * 1000)
            name = f"phrase_{stamp}_{ms:03d}.wav"
            (_DIR / name).write_bytes(wav_bytes)
            entry = {
                "ts": now,
                "iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                "file": name,
                "text": text or "",
                "source": source,
            }
            entries = _read_entries()
            entries.append(entry)
            entries = _prune(entries)
            _write_entries(entries)
    except Exception:
        # Debug capture must never break the main loop.
        pass


# --- CLI ---

def _cli_list() -> None:
    entries = _read_entries()
    if not entries:
        print("No captured phrases yet.")
        return
    for i, e in enumerate(entries):
        text = (e.get("text") or "<empty>").replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        print(f"[{i:3d}] {e.get('iso','?')}  {e.get('source','?'):>8}  {text}")


def _cli_play(idx: int) -> None:
    entries = _read_entries()
    if not 0 <= idx < len(entries):
        print(f"No entry {idx}. Range: 0..{len(entries)-1}")
        return
    e = entries[idx]
    path = _DIR / e.get("file", "")
    if not path.exists():
        print(f"WAV missing: {path}")
        return
    print(f"Playing [{idx}] {e.get('iso')}  text={e.get('text')!r}")
    try:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    except Exception as ex:
        print(f"Playback failed via winsound ({ex}); opening with default app.")
        os.startfile(str(path))


def _cli_delete(idx: int) -> None:
    with _LOCK:
        entries = _read_entries()
        if not 0 <= idx < len(entries):
            print(f"No entry {idx}.")
            return
        e = entries.pop(idx)
        _delete_wav(e.get("file"))
        _write_entries(entries)
        print(f"Deleted [{idx}] {e.get('file')}")


def _cli_clear() -> None:
    with _LOCK:
        entries = _read_entries()
        for e in entries:
            _delete_wav(e.get("file"))
        if _INDEX.exists():
            _INDEX.unlink()
        print(f"Cleared {len(entries)} entries.")


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "list":
        _cli_list()
        return 0
    if cmd == "play" and len(argv) >= 2:
        _cli_play(int(argv[1]))
        return 0
    if cmd == "delete" and len(argv) >= 2:
        _cli_delete(int(argv[1]))
        return 0
    if cmd == "clear":
        _cli_clear()
        return 0
    print(f"Unknown command: {' '.join(argv)}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
