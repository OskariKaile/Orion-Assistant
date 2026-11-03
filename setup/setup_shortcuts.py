"""Create / refresh the Orion desktop shortcut and autostart entry.

Run this once (`python setup_shortcuts.py`) to put a launcher on the
Desktop and in the Windows Startup folder. Both point to `pythonw.exe
orion.py` in this project, carry the Orion icon, and have an AUMID set
so the taskbar entry shows the right icon.

Re-run any time you want to refresh them (e.g. after moving the folder).
Pass `--remove` to delete them.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths  # noqa: F401

_HERE = _paths.ROOT
_ORION_PY = os.path.join(_HERE, "orion.py")
_ICON = os.path.join(_HERE, "images", "orion-logo-icon-bg.ico")
_AUMID = "Orion.Assistant.1"
_NAME = "Orion.lnk"


def _has_orion_deps(pyexe: str) -> bool:
    """Quick check: does this interpreter have Orion's main deps?

    Includes the song-recognition packages so we don't pick an interpreter
    that can launch Orion but crashes on "identify this song".
    """
    import subprocess
    try:
        r = subprocess.run(
            [pyexe, "-c",
             "import sounddevice, numpy, google.generativeai, "
             "pyaudiowpatch, shazamio"],
            capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _pythonw() -> str:
    """Pick a pythonw that has Orion's deps. Prefer the project venv (where
    deps are actually installed), then the current interpreter, then common
    system installs."""
    candidates = []
    # Project venv -- Orion's deps live here.
    venv_pyw = os.path.join(_HERE, "venv", "Scripts", "pythonw.exe")
    candidates.append(venv_pyw)
    # Current interpreter's pythonw counterpart.
    base, name = os.path.split(sys.executable)
    cur_pyw = os.path.join(
        base, "pythonw.exe" if name.lower() == "python.exe" else name)
    candidates.append(cur_pyw)
    # Microsoft Store Python (where Orion's deps were originally installed).
    store = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WindowsApps", "pythonw.exe")
    candidates.append(store)
    # Any python.org install under LOCALAPPDATA\Programs\Python.
    local_progs = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Python")
    if os.path.isdir(local_progs):
        for n in sorted(os.listdir(local_progs), reverse=True):
            candidates.append(os.path.join(local_progs, n, "pythonw.exe"))

    seen: set[str] = set()
    for c in candidates:
        if not c or c in seen or not os.path.exists(c):
            continue
        seen.add(c)
        # Replace pythonw.exe with python.exe for the dep check (pythonw
        # swallows stderr so import errors would be invisible).
        check_exe = c.replace("pythonw.exe", "python.exe")
        if not os.path.exists(check_exe):
            check_exe = c
        if _has_orion_deps(check_exe):
            return c
    # Last resort.
    return cur_pyw


def _desktop_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _startup_dir() -> str:
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(
        appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _write_shortcut(lnk_path: str) -> None:
    import pythoncom
    from win32com.shell import shell  # type: ignore
    from win32com.propsys import propsys, pscon  # type: ignore

    os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None,
        pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(_pythonw())
    link.SetArguments(f'"{_ORION_PY}"')
    link.SetWorkingDirectory(_HERE)
    link.SetDescription("Orion voice assistant")
    if os.path.exists(_ICON):
        link.SetIconLocation(_ICON, 0)

    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(_AUMID))
    store.Commit()

    persist = link.QueryInterface(pythoncom.IID_IPersistFile)
    persist.Save(lnk_path, 0)


def install() -> None:
    for folder, label in [(_desktop_dir(), "Desktop"),
                          (_startup_dir(), "Startup")]:
        path = os.path.join(folder, _NAME)
        _write_shortcut(path)
        print(f"  {label:<8} -> {path}")


def remove() -> None:
    for folder, label in [(_desktop_dir(), "Desktop"),
                          (_startup_dir(), "Startup")]:
        path = os.path.join(folder, _NAME)
        if os.path.exists(path):
            os.remove(path)
            print(f"  removed {label:<8} -> {path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--remove", action="store_true",
                   help="Delete the shortcuts instead of creating them.")
    args = p.parse_args()
    if args.remove:
        remove()
    else:
        install()
    print("Done.")


if __name__ == "__main__":
    main()
