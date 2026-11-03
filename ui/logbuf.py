"""In-process log ring buffer for Orion.

Orion normally runs under pythonw.exe, where sys.stdout / sys.stderr are
None -- every stray print, library warning, and traceback vanishes. This
module tees both streams into a small in-memory ring buffer so the Hub's
Logs tab can show them, while still forwarding to the original stream when
one exists (console runs keep printing normally).

Also hooks sys.excepthook and threading.excepthook so an uncaught
exception on any thread lands in the buffer instead of dying silently.

Usage:
    import logbuf
    logbuf.install()          # once, early in startup
    logbuf.lines(200)         # newest-last list of {ts, stream, line}
    logbuf.log("message")     # explicit app-level entry
"""

import os
import sys
import threading
import time
import traceback
from collections import deque

_LOCK = threading.Lock()
_BUF: deque = deque(maxlen=600)
_installed = False

# Optional on-disk mirror. The ring buffer dies with the process, which is
# useless for the one question you most want answered after Orion vanishes:
# why did it stop? Everything that reaches the buffer is also appended here
# and flushed immediately, so the record survives even a hard crash.
_FILE = None
_FILE_PATH = None
_MAX_BYTES = 2 * 1024 * 1024


def _write_line(stream: str, line: str, ts: float) -> None:
    """Mirror one entry to the log file. Never raises."""
    if _FILE is None:
        return
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        _FILE.write(f"{stamp} [{stream}] {line}\n")
        _FILE.flush()
    except Exception:
        pass


def _append(stream: str, line: str) -> None:
    line = line.rstrip()
    if not line:
        return
    ts = time.time()
    _BUF.append({"ts": ts, "stream": stream, "line": line[:500]})
    _write_line(stream, line[:500], ts)


class _Tee:
    """File-like wrapper that mirrors writes into the ring buffer."""

    def __init__(self, name, orig):
        self._name = name
        self._orig = orig
        self._frag = ""

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        if self._orig is not None:
            try:
                self._orig.write(s)
            except Exception:
                pass
        with _LOCK:
            self._frag += s
            while "\n" in self._frag:
                line, self._frag = self._frag.split("\n", 1)
                _append(self._name, line)
        return len(s)

    def flush(self):
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def writable(self):
        return True

    def fileno(self):
        if self._orig is not None and hasattr(self._orig, "fileno"):
            return self._orig.fileno()
        import io
        raise io.UnsupportedOperation("fileno")

    @property
    def encoding(self):
        return getattr(self._orig, "encoding", "utf-8") or "utf-8"


def _open_logfile(path: str) -> None:
    """Start mirroring the buffer to `path`, rotating it if it's grown large."""
    global _FILE, _FILE_PATH
    try:
        if os.path.exists(path) and os.path.getsize(path) > _MAX_BYTES:
            backup = path + ".1"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(path, backup)
            except Exception:
                pass
        _FILE = open(path, "a", encoding="utf-8", errors="replace")
        _FILE_PATH = path
    except Exception:
        _FILE = None
        _FILE_PATH = None


def logfile_path():
    """Where the durable log is being written, or None."""
    return _FILE_PATH


def install(logfile: str = None) -> None:
    """Tee stdout/stderr into the buffer and hook exception reporting.

    Pass `logfile` to also mirror everything to disk, and to arm faulthandler
    against that same file. A native crash inside an audio or ONNX library
    kills the process without touching any Python `except`, so faulthandler's
    dump is the only trace such a death leaves behind.

    Idempotent; never raises.
    """
    global _installed
    if _installed:
        return
    _installed = True
    if logfile:
        _open_logfile(logfile)
        try:
            import faulthandler
            if _FILE is not None:
                faulthandler.enable(file=_FILE, all_threads=True)
        except Exception:
            pass
    try:
        sys.stdout = _Tee("stdout", sys.stdout)
        sys.stderr = _Tee("stderr", sys.stderr)
    except Exception:
        pass

    def _log_exc(prefix, exc_type, exc, tb):
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            with _LOCK:
                for line in text.splitlines():
                    _append("error", f"{prefix}{line}")
        except Exception:
            pass

    try:
        _prev_hook = sys.excepthook

        def _hook(exc_type, exc, tb):
            _log_exc("", exc_type, exc, tb)
            try:
                if _prev_hook is not None:
                    _prev_hook(exc_type, exc, tb)
            except Exception:
                pass
        sys.excepthook = _hook
    except Exception:
        pass

    try:
        _prev_thook = threading.excepthook

        def _thook(args):
            name = getattr(args.thread, "name", "?")
            _log_exc(f"[thread {name}] ", args.exc_type, args.exc_value,
                     args.exc_traceback)
            try:
                if _prev_thook is not None:
                    _prev_thook(args)
            except Exception:
                pass
        threading.excepthook = _thook
    except Exception:
        pass

    # Anything that unwinds the interpreter normally passes through here, so
    # a quiet disappearance can be told apart from a crash: an exit with no
    # preceding "shutting down" entry was not Orion's own decision.
    try:
        import atexit
        atexit.register(lambda: log("process exiting"))
    except Exception:
        pass

    log("log capture started")


def log(message: str) -> None:
    """Add an explicit app-level entry (shown as stream 'orion')."""
    with _LOCK:
        for line in str(message).splitlines():
            _append("orion", line)


def lines(limit: int = 200) -> list:
    """Newest-last list of the most recent `limit` entries."""
    with _LOCK:
        items = list(_BUF)
    if limit and limit > 0:
        items = items[-limit:]
    return items


def clear() -> None:
    with _LOCK:
        _BUF.clear()
