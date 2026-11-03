"""Orion's PC control (Group 7).

Opens folders, finds files, locks the PC, empties the Recycle Bin, schedules
shutdowns, and controls system volume. Returns spoken-style status strings.
"""

import ctypes
import os
import subprocess
import time

import config

# Folder names Orion understands without a configured path.
_KNOWN_FOLDERS = {
    "downloads": os.path.join(os.path.expanduser("~"), "Downloads"),
    "documents": os.path.join(os.path.expanduser("~"), "Documents"),
    "desktop": os.path.join(os.path.expanduser("~"), "Desktop"),
    "pictures": os.path.join(os.path.expanduser("~"), "Pictures"),
    "music": os.path.join(os.path.expanduser("~"), "Music"),
    "videos": os.path.join(os.path.expanduser("~"), "Videos"),
}

_SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "node_modules",
    "__pycache__", ".git", "appdata", "program files", "program files (x86)",
    "programdata", "venv", ".venv",
}


def open_folder(name):
    """Open a Windows Explorer window for the named folder."""
    name = name.strip().lower().replace("folder", "").strip()
    path = _KNOWN_FOLDERS.get(name)
    if path is None:
        cand = os.path.expandvars(os.path.expanduser(name))
        if os.path.isdir(cand):
            path = cand
    if path is None or not os.path.isdir(path):
        return f"I couldn't find a folder called {name}."
    try:
        os.startfile(path)
        return f"Opening your {name} folder."
    except OSError:
        return f"I couldn't open the {name} folder."


def _search_roots():
    """Return the directories to crawl when looking for a file."""
    if config.FILE_SEARCH_ROOTS:
        return list(config.FILE_SEARCH_ROOTS)
    roots = [os.path.expanduser("~")]
    for letter in "CDEFGH":
        drive = f"{letter}:\\"
        if os.path.isdir(drive):
            roots.append(drive)
    return roots


def find_file(name):
    """Crawl the disk for files matching `name`; return a spoken result."""
    name = name.strip().lower()
    if not name:
        return "Which file should I look for, Sir?"

    deadline = time.time() + config.FILE_SEARCH_TIME_BUDGET
    matches = []
    seen = set()
    for root in _search_roots():
        for dirpath, dirnames, files in os.walk(root):
            if time.time() > deadline:
                break
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in _SKIP_DIRS]
            for f in files:
                if name in f.lower():
                    full = os.path.join(dirpath, f)
                    if full not in seen:
                        seen.add(full)
                        matches.append(full)
            if len(matches) >= config.FILE_SEARCH_MAX_RESULTS:
                break
        if len(matches) >= config.FILE_SEARCH_MAX_RESULTS or time.time() > deadline:
            break

    if not matches:
        return f"I couldn't find any file matching {name}."
    if len(matches) == 1:
        return f"I found it at {matches[0]}."
    spoken = "; ".join(os.path.basename(m) + " in " + os.path.dirname(m)
                       for m in matches)
    return f"I found {len(matches)} matches: {spoken}."


def lock_pc():
    """Lock the Windows session."""
    try:
        ctypes.windll.user32.LockWorkStation()
        return "Locking your PC, Sir."
    except Exception:
        return "I couldn't lock the PC."


def empty_trash():
    """Empty the Windows Recycle Bin."""
    try:
        import winshell
    except ImportError:
        return "I need the winshell package installed to empty the trash."
    try:
        winshell.recycle_bin().empty(confirm=False, show_progress=False,
                                     sound=False)
        return "Recycle Bin emptied, Sir."
    except Exception:
        return "The Recycle Bin was already empty."


def shutdown_in(minutes):
    """Schedule a Windows shutdown `minutes` from now."""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "How many minutes until shutdown, Sir?"
    if minutes < 0:
        return "I can't schedule a shutdown in the past."
    seconds = minutes * 60
    os.system(f"shutdown /s /t {seconds}")
    if minutes == 0:
        return "Shutting down now."
    return f"Shutting down in {minutes} minutes. Say cancel shutdown to stop it."


def cancel_shutdown():
    """Abort a pending scheduled shutdown."""
    os.system("shutdown /a")
    return "Shutdown cancelled, Sir."


# --- Volume control via the Windows audio API (pycaw) ---

def _volume_endpoint():
    """Return the master-volume COM interface, or None if pycaw is missing."""
    try:
        from ctypes import POINTER, cast
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception:
        return None
    try:
        # Initialize COM for this (worker) thread before creating any audio
        # COM objects. Without it, the interface is released on a thread with
        # no live COM apartment at garbage-collection time -- which throws the
        # "access violation in Release()" noise during a chat. CoInitialize
        # is reference-counted, so calling it again here is safe and cheap.
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_,
                                     CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))
    except Exception:
        return None


def _release_endpoint(vol):
    """Free a volume interface on the thread that created it.

    Left to itself, a COM pointer caught in a reference cycle is freed by
    Python's cyclic collector on whatever thread happens to trigger it, and
    releasing a COM pointer from a foreign thread is an access violation --
    which ends Orion outright, with no exception and no traceback. Dropping
    the reference and collecting here means the release happens on this
    thread, while this thread still owns the apartment.
    """
    del vol
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def set_volume(level):
    """Set the system volume to `level` (0-100)."""
    try:
        level = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return "What volume level should I set, Sir?"
    vol = _volume_endpoint()
    if vol is None:
        return "I need the pycaw package installed to control the volume."
    try:
        vol.SetMute(0, None)
        vol.SetMasterVolumeLevelScalar(level / 100.0, None)
    finally:
        _release_endpoint(vol)
    return f"Volume set to {level} percent."


def nudge_volume(up):
    """Raise (up=True) or lower the system volume by 10 percent."""
    vol = _volume_endpoint()
    if vol is None:
        return "I need the pycaw package installed to control the volume."
    try:
        current = vol.GetMasterVolumeLevelScalar()
        target = current + 0.1 if up else current - 0.1
        target = max(0.0, min(1.0, target))
        vol.SetMute(0, None)
        vol.SetMasterVolumeLevelScalar(target, None)
    finally:
        _release_endpoint(vol)
    return f"Volume {'up' if up else 'down'}, now {round(target * 100)} percent."
