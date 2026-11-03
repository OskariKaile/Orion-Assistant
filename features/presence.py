"""Is the user actually at the computer?

Windows will happily tell you how long it has been since the last mouse or
keyboard event, and that single number is the naive answer to this question.
It is also wrong most of the time: watching a film, listening to an album and
reading a long page all look identical to an empty chair.

So the raw idle timer is only one of four signals, and they resolve into three
states rather than a boolean:

  active   Recent input, or Orion heard you speak. You are here, no doubt.
  passive  No input, but something is playing through the speakers, or a
           window is running fullscreen. You are here, watching or listening.
  away     No input, nothing playing, nothing fullscreen -- or the
           workstation is locked, which settles it instantly and with
           certainty regardless of every other signal.

`away` is deliberately the hardest state to reach, because the cost of getting
it wrong is asymmetric: holding a reminder back from someone who is sitting
right there is far more annoying than announcing one a minute late.

Everything here degrades to "active" when a signal can't be read, so a missing
dependency or a non-Windows host makes Orion behave exactly as it did before
presence detection existed.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time

import config

ACTIVE = "active"
PASSIVE = "passive"
AWAY = "away"

_lock = threading.Lock()
_last_voice = 0.0       # monotonic, when Orion last heard the user speak
_suspend_audio = False  # true while Orion itself is talking


# --- raw signals -----------------------------------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """Seconds since the last mouse or keyboard event. 0.0 if unreadable.

    Note this is system-wide and reports 0 while the workstation is locked on
    some configurations, which is exactly why `locked()` is consulted first.
    """
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)
    except Exception:
        return 0.0


def locked():
    """True if the workstation is locked or the screensaver is up.

    The input desktop can only be opened while the interactive desktop is the
    one receiving input; when Windows switches to the secure Winlogon desktop
    the call fails, which is a far more reliable lock signal than any timer.
    """
    try:
        user32 = ctypes.windll.user32
        DESKTOP_SWITCHDESKTOP = 0x0100
        handle = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not handle:
            return True
        user32.CloseDesktop(handle)
        return False
    except Exception:
        return False


# --- output meter ----------------------------------------------------------
#
# Whether sound is playing can only be read from Windows through COM, and no
# COM object may exist in Orion's process for it. The reason is worth stating
# plainly, because the obvious fixes do not work:
#
#   * A COM pointer must be released on the thread that created it. Releasing
#     it elsewhere is an access violation, which ends the process instantly
#     with nothing to catch and no traceback. Orion simply vanished.
#   * Creating it on a dedicated thread is NOT enough. Python decides when to
#     free it, and an object caught in a reference cycle is freed by the
#     cyclic collector, running on whichever thread happened to trigger it.
#     The creating thread does not get to choose.
#
# So the meter lives in a separate process (audio_probe.py) that prints a
# reading every couple of seconds. This thread reads those lines and publishes
# a plain boolean. Nothing here is COM, so nothing here can fault; if the
# probe itself ever dies, only the probe dies, and Orion reports no audio and
# tries again later. Song recognition is out of process for the same reason.

_audio_flag = False
_audio_thread = None
_audio_asked = 0.0


def _probe_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "audio_probe.py")


def _spawn_probe(interval):
    """Start the meter process. Returns the Popen, or None."""
    # No console window: Orion normally runs windowless, and a probe
    # flashing up a terminal every few minutes would be worse than the bug.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(
            [sys.executable, _probe_path(), str(interval)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True, bufsize=1,
            creationflags=creationflags)
    except Exception:
        return None


def _audio_sampler():
    """Read the probe's readings until nobody is asking any more."""
    global _audio_flag, _audio_thread
    floor = float(getattr(config, "PRESENCE_AUDIO_FLOOR", 0.005))
    interval = float(getattr(config, "PRESENCE_AUDIO_POLL", 2.0))
    idle_after = float(getattr(config, "PRESENCE_AUDIO_IDLE", 300))
    proc = _spawn_probe(interval)
    if proc is None or proc.stdout is None:
        with _lock:
            _audio_thread = None
        return
    try:
        for line in proc.stdout:
            with _lock:
                asked = _audio_asked
            if time.monotonic() - asked > idle_after:
                break  # nobody has asked in a while; stop paying for this
            try:
                peak = float(line.strip())
            except ValueError:
                continue
            with _lock:
                # A negative reading means the probe could not read the
                # meter, which is not the same as silence, but "unknown"
                # has to resolve to something and present is the safe way
                # to be wrong.
                _audio_flag = peak > floor
    except Exception:
        pass
    finally:
        with _lock:
            _audio_flag = False
            _audio_thread = None
        try:
            proc.terminate()
        except Exception:
            pass


def audio_playing():
    """True if anything is currently rendering sound to the default device.

    Reads a value sampled by a separate process, so no COM object is ever
    touched from Orion. Returns False while Orion is speaking, since its own
    voice would otherwise count as the user being entertained.
    """
    global _audio_thread, _audio_asked
    with _lock:
        if _suspend_audio:
            return False
        _audio_asked = time.monotonic()
        alive = _audio_thread is not None and _audio_thread.is_alive()
        if not alive:
            _audio_thread = threading.Thread(
                target=_audio_sampler, daemon=True, name="presence-audio")
            _audio_thread.start()
            # The first reading isn't in yet. Report no audio rather than
            # blocking; the next check a few seconds later will be accurate.
            return False
        return _audio_flag


def fullscreen():
    """True if the foreground window covers a whole monitor.

    A fullscreen video or game means someone is watching it. The desktop and
    the shell are excluded so an empty desktop doesn't read as fullscreen.
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        shell = user32.GetShellWindow()
        desktop = user32.GetDesktopWindow()
        if hwnd in (shell, desktop):
            return False

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        return ((rect.right - rect.left) >= screen_w
                and (rect.bottom - rect.top) >= screen_h)
    except Exception:
        return False


# --- signals Orion feeds in ------------------------------------------------

def note_voice():
    """Record that Orion just heard the user speak -- the strongest signal
    there is that someone is in the room, and one Windows knows nothing
    about."""
    global _last_voice
    with _lock:
        _last_voice = time.monotonic()


def suspend_audio_signal(suspended):
    """Ignore the output meter while Orion is talking, so its own voice can't
    masquerade as the user watching something."""
    global _suspend_audio
    with _lock:
        _suspend_audio = bool(suspended)


# --- the answer ------------------------------------------------------------

def state():
    """Current presence as one of ACTIVE / PASSIVE / AWAY."""
    if not getattr(config, "PRESENCE_ENABLED", True):
        return ACTIVE
    if locked():
        return AWAY

    with _lock:
        voice_age = time.monotonic() - _last_voice if _last_voice else None
    voice_window = float(getattr(config, "PRESENCE_VOICE_WINDOW", 180))
    if voice_age is not None and voice_age < voice_window:
        return ACTIVE

    idle = idle_seconds()
    if idle < float(getattr(config, "PRESENCE_ACTIVE_SECONDS", 180)):
        return ACTIVE

    # No input for a while. Before calling it an empty chair, check the two
    # things that look idle but aren't.
    if audio_playing() or fullscreen():
        return PASSIVE

    if idle < float(getattr(config, "PRESENCE_AWAY_SECONDS", 600)):
        # In between: quiet, but not long enough to assume the chair is empty.
        return PASSIVE
    return AWAY


def is_present():
    """True unless we are confident nobody is at the computer."""
    return state() != AWAY


def describe():
    """A short human-readable breakdown, for the Hub and for debugging."""
    return {
        "state": state(),
        "idle_seconds": round(idle_seconds(), 1),
        "locked": locked(),
        "audio_playing": audio_playing(),
        "fullscreen": fullscreen(),
    }
