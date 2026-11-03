"""Open the Orion Hub in a window.

Prefers a native pywebview window (its own taskbar entry, our icon).
Falls back to Chrome/Edge `--app=URL`, then to the default browser.
Never blocks the caller.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import webbrowser

import _paths
import config
import hub_server


_HERE = _paths.ROOT  # project root: .launcher/, images/, working dir for shortcut
_HUB_WINDOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_window.py")
_HUB_ICON = os.path.join(_HERE, "images", "orion-hub.ico")
_AUMID = "Orion.Hub.1"


def _shortcut_path() -> str:
    # Keep the .lnk inside the project folder. %LOCALAPPDATA% is redirected
    # by the Microsoft Store Python sandbox into a per-package LocalCache,
    # which Explorer/taskbar can't see -- so the shortcut would be invisible.
    folder = os.path.join(_HERE, ".launcher")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "Orion Hub.lnk")


def _write_shortcut(pyw: str, url: str) -> str | None:
    """Create/refresh a .lnk that launches the hub with our icon + AUMID.
    Windows uses the shortcut's icon and AppUserModelID for the taskbar
    entry, which is the only fully reliable way to override pythonw.exe's
    icon without bundling a custom exe."""
    try:
        import pythoncom  # type: ignore
        from win32com.shell import shell, shellcon  # type: ignore
        from win32com.propsys import propsys, pscon  # type: ignore
    except Exception:
        return None
    try:
        lnk_path = _shortcut_path()
        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.SetPath(pyw)
        link.SetArguments(f'"{_HUB_WINDOW}" "{url}"')
        link.SetWorkingDirectory(_HERE)
        if os.path.exists(_HUB_ICON):
            link.SetIconLocation(_HUB_ICON, 0)
        link.SetDescription("Orion Hub")

        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID,
                       propsys.PROPVARIANTType(_AUMID))
        store.Commit()

        persist = link.QueryInterface(pythoncom.IID_IPersistFile)
        persist.Save(lnk_path, 0)
        return lnk_path
    except Exception:
        return None

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find(candidates: list[str]) -> str | None:
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _resolve_browser() -> str | None:
    if not getattr(config, "HUB_BROWSER_APP_MODE", True):
        return None
    chrome = _find(_CHROME_CANDIDATES) or shutil.which("chrome.exe")
    if chrome:
        return chrome
    edge = _find(_EDGE_CANDIDATES) or shutil.which("msedge.exe")
    return edge


def _pythonw() -> str:
    """Windowless Python, so spawning the hub doesn't flash a console.

    Prefer a non-Store python.org install: Microsoft Store Python is a
    packaged app whose taskbar identity is locked by Windows, so the Hub
    window always groups under the generic Python icon. A standard
    python.org install (under %LOCALAPPDATA%\\Programs\\Python) lets our
    AUMID + shortcut icon override take effect.
    """
    candidates = []
    local_progs = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Python")
    if os.path.isdir(local_progs):
        for name in sorted(os.listdir(local_progs), reverse=True):
            candidates.append(os.path.join(local_progs, name, "pythonw.exe"))
    candidates.extend([
        r"C:\Program Files\Python312\pythonw.exe",
        r"C:\Program Files\Python311\pythonw.exe",
    ])
    for c in candidates:
        if c and os.path.exists(c) and "WindowsApps" not in c:
            return c
    # Fallback to current interpreter (may be Store Python -> generic icon).
    exe = sys.executable
    base, name = os.path.split(exe)
    if name.lower() == "python.exe":
        candidate = os.path.join(base, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def _spawn_native_window(url: str) -> bool:
    if not getattr(config, "HUB_NATIVE_WINDOW", True):
        return False
    if not os.path.exists(_HUB_WINDOW):
        return False
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    pyw = _pythonw()
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    lnk = _write_shortcut(pyw, url)
    if lnk:
        # Launching via the .lnk gives the taskbar entry the shortcut's
        # icon + AUMID instead of pythonw.exe's.
        threading.Thread(
            target=lambda: os.startfile(lnk),  # type: ignore[attr-defined]
            daemon=True,
        ).start()
        return True
    cmd = [pyw, _HUB_WINDOW, url]
    threading.Thread(
        target=lambda: subprocess.Popen(cmd, creationflags=flags),
        daemon=True,
    ).start()
    return True


def open_hub() -> bool:
    """Open the hub window. Starts the server first if it isn't running.
    Returns True if launch was dispatched. Never raises."""
    try:
        if not hub_server.running():
            hub_server.start()
        url = hub_server.url()
        if _spawn_native_window(url):
            return True
        browser = _resolve_browser()
        if browser:
            cmd = [browser, f"--app={url}", "--new-window"]
            threading.Thread(
                target=lambda: subprocess.Popen(
                    cmd, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)),
                daemon=True,
            ).start()
            return True
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
        return True
    except Exception:
        return False
