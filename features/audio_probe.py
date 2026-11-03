"""Standalone peak-meter probe: is anything playing through the speakers?

Run as its own process:

    python features/audio_probe.py [interval_seconds]

It prints one line per interval -- the output device's peak level as a float,
or -1 when the meter can't be read -- and exits when its output is closed.

# Why this is a separate process

Presence detection wants to know whether sound is playing, and the only way
Windows offers is a COM interface. COM objects belong to the thread that made
them, and Python decides when to free them: an object caught in a reference
cycle is freed by the cyclic collector, running on whichever thread happened
to trigger it. Releasing a COM pointer from a foreign thread is an access
violation, and an access violation ends the process instantly with no
exception to catch and no traceback to read. Orion just vanished.

Keeping the object on one thread was not enough, because the collector still
chose the thread. Keeping it in one *process* is enough: the meter is created
here, freed here, and if this process ever does fault, Orion notices a dead
pipe and carries on.

This mirrors how song recognition is handled (recognize_song.py), which runs
out of process for the same reason.
"""

import sys
import time

REBUILD_SECONDS = 60  # remake the meter periodically so switching the
                      # default output device is picked up


def _build_meter():
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(
        IAudioMeterInformation._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioMeterInformation))


def _emit(value):
    """Print one reading. Returns False once the reader has gone away."""
    try:
        sys.stdout.write("%.6f\n" % value)
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False


def main(argv):
    # `--once` prints a single reading and exits. Handy for checking by hand
    # whether the meter works on this machine without leaving a probe running.
    once = "--once" in argv
    argv = [a for a in argv if not a.startswith("-")]
    try:
        interval = float(argv[0]) if argv else 2.0
    except ValueError:
        interval = 2.0

    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        _emit(-1.0)
        return 1

    meter = None
    built = 0.0
    while True:
        now = time.monotonic()
        try:
            if meter is None or (now - built) > REBUILD_SECONDS:
                meter = _build_meter()
                built = now
            peak = float(meter.GetPeakValue())
        except Exception:
            meter = None
            peak = -1.0
        if not _emit(peak) or once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
