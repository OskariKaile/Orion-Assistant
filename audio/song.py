"""Song recognition for Orion.

Identifies whatever music is playing -- from a game, a video, a call, or out
loud in the room -- says what it is, and copies the result to the clipboard.

The actual capture and Shazam lookup happen in a separate helper process
(recognize_song.py). Those steps use native code (soundcard's WASAPI,
shazamio-core's Rust) that can hard-crash the interpreter with no catchable
Python exception -- running them in their own process means a crash there
only kills the helper, never Orion. This mirrors how tts.py isolates audio
playback in play_wav.py.

Nothing here runs in the background -- the helper is spawned only when the
user asks, so this adds no idle CPU load.
"""

import importlib.util
import json
import os
import subprocess
import sys

import config

_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "recognize_song.py")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _packages_ready():
    """True if the capture/recognition packages are installed.

    Uses find_spec rather than importing the packages: importing shazamio
    here would pull pydub into Orion's main process and print pydub's noisy
    'couldn't find ffmpeg' warning. The packages are only ever imported
    inside the recognize_song.py helper process.
    """
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("pyaudiowpatch", "shazamio"))
    except Exception:
        return False


def prewarm():
    """Spawn the helper in --warmup mode so its imports + first-run
    downloads (imageio-ffmpeg, shazamio rust binding) happen in the
    background after Orion boots. The first "what song is this" query
    then doesn't pay the ~30s cold-start cost.

    Fire-and-forget: returns immediately, the helper runs in its own
    process. Safe to call even if the deps aren't installed -- it just
    no-ops in that case.
    """
    if not _packages_ready():
        return
    try:
        subprocess.Popen(
            [sys.executable, _HELPER, "--warmup"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        pass


def identify(orion=None):
    """Listen to whatever is playing and name the song.

    Spawns the recognize_song.py helper, which tries system audio first
    (games, video, calls) then the microphone (music in the room). Returns a
    sentence for Orion to speak, and copies "Artist - Title" to the clipboard
    on a successful match.
    """
    if not _packages_ready():
        # Log which interpreter is missing the packages -- usually means the
        # desktop shortcut is pointing at a Python without song deps installed.
        try:
            sys.stderr.write(
                f"[song] missing pyaudiowpatch/shazamio in {sys.executable}\n")
        except Exception:
            pass
        return ("I'm not set up to recognise songs yet, Sir. The "
                "PyAudioWPatch and shazamio packages need to be installed "
                "first.")

    seconds = getattr(config, "SONG_LISTEN_SECONDS", 8)
    # Worst case is a loopback capture plus a mic capture plus two Shazam
    # lookups, so allow generous headroom before giving up on the helper.
    timeout = seconds * 2 + 30

    try:
        proc = subprocess.run(
            [sys.executable, _HELPER, str(seconds)],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "The song recogniser took too long, Sir. Please try again."
    except Exception:
        return "I couldn't start the song recogniser, Sir."

    # The helper prints a single JSON line. If it crashed natively there will
    # be no JSON -- Orion stays alive and just reports it couldn't identify.
    title, artist = None, ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                title = data.get("title")
                artist = data.get("artist") or ""
            except ValueError:
                pass

    if title:
        label = f"{artist} - {title}" if artist else title
        copied = ""
        try:
            import pyperclip
            pyperclip.copy(label)
            copied = " I've copied it to your clipboard."
        except Exception:
            copied = ""
        if artist:
            return f"That's {title} by {artist}.{copied}"
        return f"That's {title}.{copied}"

    return ("I couldn't make out a song, Sir. Make sure the music is playing "
            "clearly and try again.")
