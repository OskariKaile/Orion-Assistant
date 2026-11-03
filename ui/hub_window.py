"""Native window host for the Orion Hub.

Run as a standalone process: `python hub_window.py <url>`.
pywebview's event loop must own the main thread, so the parent
(`hub_launcher`) spawns this as a detached subprocess. That also
gives the window its own taskbar entry with our icon -- not Chrome's.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import threading
import time

import webview

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
_ICON = os.path.join(_HERE, "images", "orion-hub.ico")
_TITLE = "Orion Hub"


_AUMID = "Orion.Hub.1"


def _register_aumid() -> None:
    """Register our AUMID in HKCU so Windows knows the icon + name to use
    in the taskbar. Without this it falls back to pythonw.exe's icon."""
    try:
        import winreg
        key_path = r"Software\Classes\AppUserModelId" + "\\" + _AUMID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Orion Hub")
            winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, _ICON)
            winreg.SetValueEx(
                k, "IconResource", 0, winreg.REG_EXPAND_SZ, _ICON + ",0")
    except Exception:
        pass


def _set_app_user_model_id() -> None:
    _register_aumid()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_AUMID)
    except Exception:
        pass


def _find_own_top_window() -> int:
    """Return the HWND of our top-level visible window, or 0."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    own_pid = kernel32.GetCurrentProcessId()

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = ctypes.c_void_p(0)

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != own_pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        # Only top-level windows (no owner).
        if user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        found.value = hwnd
        return False  # stop

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found.value or 0


def _apply_window_icon() -> None:
    user32 = ctypes.windll.user32
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    WM_SETICON = 0x0080
    ICON_SMALL = 0
    ICON_BIG = 1
    SM_CXICON = 11
    SM_CYICON = 12
    SM_CXSMICON = 49
    SM_CYSMICON = 50

    if not os.path.exists(_ICON):
        return

    user32.LoadImageW.restype = wintypes.HANDLE
    big = user32.LoadImageW(
        0, _ICON, IMAGE_ICON,
        user32.GetSystemMetrics(SM_CXICON),
        user32.GetSystemMetrics(SM_CYICON),
        LR_LOADFROMFILE,
    )
    small = user32.LoadImageW(
        0, _ICON, IMAGE_ICON,
        user32.GetSystemMetrics(SM_CXSMICON),
        user32.GetSystemMetrics(SM_CYSMICON),
        LR_LOADFROMFILE,
    )
    if not big and not small:
        return

    deadline = time.time() + 15.0
    hwnd = 0
    while time.time() < deadline:
        hwnd = _find_own_top_window()
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return

    user32.SendMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    GCLP_HICON = -14
    GCLP_HICONSM = -34
    set_class = getattr(user32, "SetClassLongPtrW", user32.SetClassLongW)
    set_class.restype = ctypes.c_void_p
    set_class.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]

    def apply():
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
            set_class(hwnd, GCLP_HICONSM, small)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            set_class(hwnd, GCLP_HICON, big)

    apply()
    # Re-apply a moment later in case the framework overwrites the icon
    # during its own init.
    time.sleep(1.0)
    apply()


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    # Set AUMID before creating the window so Windows groups the taskbar
    # entry under our app (with our icon) instead of pythonw.exe.
    _set_app_user_model_id()
    window = webview.create_window(
        _TITLE,
        url=url,
        width=1280,
        height=820,
        min_size=(900, 600),
    )
    threading.Thread(target=_apply_window_icon, daemon=True).start()
    try:
        webview.start(icon=_ICON)
    except TypeError:
        webview.start()
    _ = window


if __name__ == "__main__":
    main()
