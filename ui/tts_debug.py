"""Rolling capture of recent TTS WAVs for debugging playback bugs.

Built to chase the "speaks too fast / garbled / partial words" intermittent
bug: the synthesized WAV is normally deleted right after playback, so by the
time you notice the glitch the audio is gone. This module copies each WAV
into `tts_debug/` before deletion plus a JSONL sidecar (timestamp, spoken
text). A rolling cap keeps the directory bounded.

CLI:
    python tts_debug.py list
    python tts_debug.py play 3
    python tts_debug.py delete 3
    python tts_debug.py clear
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths  # noqa: F401

import config

_DIR = Path(getattr(config, "TTS_DEBUG_DIR", "tts_debug"))
_INDEX = _DIR / "index.jsonl"
_LOCK = threading.Lock()
_KEEP = int(getattr(config, "TTS_DEBUG_KEEP", 100))
_MAX_MB = float(getattr(config, "TTS_DEBUG_MAX_MB", 100))


def enabled() -> bool:
    return bool(getattr(config, "TTS_DEBUG", True))


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


def _delete_wav(name: str | None) -> None:
    if not name:
        return
    try:
        (_DIR / name).unlink()
    except OSError:
        pass


def _prune(entries: list[dict]) -> list[dict]:
    if len(entries) > _KEEP:
        for old in entries[:-_KEEP]:
            _delete_wav(old.get("file"))
        entries = entries[-_KEEP:]
    limit = _MAX_MB * 1024 * 1024
    sizes = []
    total = 0
    for e in entries:
        try:
            sz = (_DIR / e.get("file", "")).stat().st_size
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


def capture(src_path: str, text: str) -> None:
    """Copy a synthesized WAV into the debug folder. Never raises."""
    if not enabled() or not src_path:
        return
    try:
        with _LOCK:
            _ensure_dir()
            now = time.time()
            stamp = datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S")
            ms = int((now - int(now)) * 1000)
            name = f"tts_{stamp}_{ms:03d}.wav"
            shutil.copyfile(src_path, _DIR / name)
            entry = {
                "ts": now,
                "iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                "file": name,
                "text": (text or "")[:500],
            }
            entries = _read_entries()
            entries.append(entry)
            entries = _prune(entries)
            _write_entries(entries)
    except Exception:
        pass


# --- CLI ---

def _cli_list() -> None:
    entries = _read_entries()
    if not entries:
        print("No captured TTS WAVs yet.")
        return
    for i, e in enumerate(entries):
        text = (e.get("text") or "<empty>").replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        print(f"[{i:3d}] {e.get('iso','?')}  {text}")


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
        print(f"winsound failed ({ex}); opening with default app.")
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
        _cli_list(); return 0
    if cmd == "play" and len(argv) >= 2:
        _cli_play(int(argv[1])); return 0
    if cmd == "delete" and len(argv) >= 2:
        _cli_delete(int(argv[1])); return 0
    if cmd == "clear":
        _cli_clear(); return 0
    print(f"Unknown command: {' '.join(argv)}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
