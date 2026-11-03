"""Smart Windows app discovery for Orion.

When the user says "open Overwatch" and the app isn't in apps.json, Orion
searches the system for it across four progressively more expensive sources:

  1. **Start Menu shortcuts** -- .lnk files in the user + all-users Programs
     folders. Catches anything that installed itself properly (a huge
     majority of normal apps). Fast and reliable.
  2. **Registry uninstall keys** -- every Windows installer writes a
     DisplayName + install location here. Catches things Start Menu missed.
  3. **Game launchers** -- Steam library folders (across all drives, read
     from `libraryfolders.vdf`), Battle.net install path (registry), and
     Epic Games install path (registry).
  4. **Filesystem walk** -- LocalAppData\\Programs, both Program Files dirs.
     Time-budgeted (5 s), used only when 1-3 all miss.

Matches are scored with difflib; the best confident match is opened and the
result is cached in apps.json so the next call is instant. If nothing is
confident enough, the caller is told to ask the user to teach a path.
"""
import difflib
import os
import re
import subprocess
import time
import urllib.parse

# pylnk3 resolves Start-Menu .lnk targets without needing the Shell COM API.
try:
    import pylnk3
except Exception:
    pylnk3 = None

try:
    import winreg
except Exception:
    winreg = None


# --- public match score thresholds ---
HIGH_CONFIDENCE = 0.80   # open without asking
LOW_CONFIDENCE = 0.60    # ambiguous; tell caller to disambiguate
# Below LOW_CONFIDENCE -> "not found".

# Words people say around an app name that aren't part of the name itself.
_NOISE_TOKENS = {
    "the", "app", "application", "program", "game", "open", "launch",
    "start", "run", "for", "me", "please", "now",
}

# File-extension noise we strip from .lnk/.exe display names.
_EXT_RE = re.compile(r"\.(lnk|exe|appref-ms|url)$", re.I)


def _normalize_name(name):
    """Lowercase + drop punctuation + drop filler words. For matching only."""
    name = name.lower()
    name = _EXT_RE.sub("", name)
    name = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", name)  # drop parenthesised bits
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    tokens = [t for t in name.split() if t and t not in _NOISE_TOKENS]
    return " ".join(tokens)


def _score(query, candidate_name):
    """Difflib similarity in 0..1 between the spoken query and a candidate."""
    q = _normalize_name(query)
    c = _normalize_name(candidate_name)
    if not q or not c:
        return 0.0
    base = difflib.SequenceMatcher(None, q, c).ratio()
    # Boost if the query appears as a whole word inside the candidate (or
    # vice versa). Catches "overwatch" vs "Overwatch 2" cleanly.
    if q in c.split() or c in q.split():
        base = max(base, 0.85)
    if (" " + q + " ") in (" " + c + " "):
        base = max(base, 0.9)
    return base


# --- source 1: Start Menu shortcuts ---
def _start_menu_dirs():
    """Folders Windows scans for installed-app shortcuts."""
    out = []
    for env in ("APPDATA", "ProgramData"):
        base = os.environ.get(env)
        if not base:
            continue
        path = os.path.join(base, "Microsoft", "Windows",
                            "Start Menu", "Programs")
        if os.path.isdir(path):
            out.append(path)
    return out


def _resolve_lnk(path):
    """Return the .exe target of a Windows .lnk, or None."""
    if pylnk3 is None:
        return None
    try:
        lnk = pylnk3.parse(path)
        target = lnk.path or ""
        target = os.path.expandvars(target or "")
        if target and target.lower().endswith(".exe") and os.path.exists(target):
            return target
    except Exception:
        return None
    return None


def _scan_start_menu(query):
    """Yield (score, display_name, exe_path) for every .lnk worth a look."""
    for root in _start_menu_dirs():
        for dirpath, _dirnames, files in os.walk(root):
            for name in files:
                if not name.lower().endswith(".lnk"):
                    continue
                display = _EXT_RE.sub("", name)
                s = _score(query, display)
                if s < LOW_CONFIDENCE:
                    continue
                target = _resolve_lnk(os.path.join(dirpath, name))
                if not target:
                    continue
                yield s, display, target


# --- source 2: Registry uninstall keys ---
_UNINSTALL_PATHS = (
    (r"HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (r"HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (r"HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def _open_hkey(root):
    if winreg is None:
        return None
    return {"HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER}.get(root)


def _iter_uninstall_entries():
    """Yield (display_name, display_icon_or_install_location) from the
    Windows uninstall registry across HKLM/HKCU and Wow6432Node."""
    if winreg is None:
        return
    for root_name, subkey in _UNINSTALL_PATHS:
        root = _open_hkey(root_name)
        if root is None:
            continue
        try:
            with winreg.OpenKey(root, subkey) as base:
                i = 0
                while True:
                    try:
                        child = winreg.EnumKey(base, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(base, child) as k:
                            name = _reg_str(k, "DisplayName")
                            icon = _reg_str(k, "DisplayIcon")
                            loc = _reg_str(k, "InstallLocation")
                            if not name:
                                continue
                            yield name, (icon or ""), (loc or "")
                    except OSError:
                        continue
        except OSError:
            continue


def _reg_str(key, name):
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return str(val) if val else ""
    except OSError:
        return ""


def _exe_from_uninstall(display_icon, install_location, app_name):
    """Pick the most likely .exe from an uninstall entry."""
    # DisplayIcon is often "C:\foo\app.exe,0" or just the exe path.
    icon = (display_icon or "").split(",")[0].strip().strip('"')
    icon = os.path.expandvars(icon)
    if icon.lower().endswith(".exe") and os.path.exists(icon):
        return icon
    # Try InstallLocation + look for an exe matching the app name.
    loc = os.path.expandvars((install_location or "").strip().strip('"'))
    if loc and os.path.isdir(loc):
        cand = _best_exe_in_folder(loc, app_name)
        if cand:
            return cand
    return None


def _scan_uninstall(query):
    for display, icon, loc in _iter_uninstall_entries():
        s = _score(query, display)
        if s < LOW_CONFIDENCE:
            continue
        exe = _exe_from_uninstall(icon, loc, query)
        if exe:
            yield s, display, exe


# --- source 3: game launchers ---
def _steam_libraries():
    """Find every Steam library root (handles multi-drive setups)."""
    out = []
    # Steam's own install path from registry.
    steam_path = None
    if winreg is not None:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (r"Software\Valve\Steam",
                        r"Software\WOW6432Node\Valve\Steam"):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        steam_path = _reg_str(k, "InstallPath") or steam_path
                        if not steam_path:
                            steam_path = _reg_str(k, "SteamPath") or None
                except OSError:
                    continue
                if steam_path:
                    break
            if steam_path:
                break
    if not steam_path:
        for guess in (r"C:\Program Files (x86)\Steam",
                      r"C:\Program Files\Steam"):
            if os.path.isdir(guess):
                steam_path = guess
                break
    if not steam_path:
        return out
    out.append(os.path.join(steam_path, "steamapps", "common"))
    # Read libraryfolders.vdf for additional libraries on other drives.
    vdf = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        for m in re.finditer(r'"path"\s+"([^"]+)"', text, re.I):
            extra = os.path.join(m.group(1).replace("\\\\", "\\"),
                                 "steamapps", "common")
            if os.path.isdir(extra) and extra not in out:
                out.append(extra)
    except OSError:
        pass
    return out


def _battle_net_path():
    """Battle.net install dir from registry (None if Battle.net isn't installed)."""
    if winreg is None:
        return None
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Blizzard Entertainment\Battle.net",
                    r"SOFTWARE\WOW6432Node\Blizzard Entertainment\Battle.net"):
            try:
                with winreg.OpenKey(root, sub) as k:
                    p = _reg_str(k, "InstallLocation") or _reg_str(k, "InstallPath")
                    if p and os.path.isdir(p):
                        return p
            except OSError:
                continue
    for guess in (r"C:\Program Files (x86)\Battle.net",
                  r"C:\Program Files\Battle.net"):
        if os.path.isdir(guess):
            return guess
    return None


def _epic_manifests():
    """Read Epic Games launcher manifests for installed games."""
    base = os.path.join(os.environ.get("PROGRAMDATA", ""),
                        "Epic", "EpicGamesLauncher", "Data", "Manifests")
    if not os.path.isdir(base):
        return
    import json
    for name in os.listdir(base):
        if not name.lower().endswith(".item"):
            continue
        try:
            with open(os.path.join(base, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        display = data.get("DisplayName") or data.get("AppName") or ""
        install = data.get("InstallLocation") or ""
        exe = data.get("LaunchExecutable") or ""
        if not (display and install and exe):
            continue
        full = os.path.join(install, exe)
        if os.path.exists(full):
            yield display, full


def _best_exe_in_folder(folder, app_name, max_depth=2):
    """Find the .exe inside `folder` whose name best matches `app_name`."""
    if not os.path.isdir(folder):
        return None
    best = None
    best_score = 0.0
    base_depth = folder.rstrip("\\/").count(os.sep)
    for dirpath, _dirnames, files in os.walk(folder):
        depth = dirpath.count(os.sep) - base_depth
        if depth > max_depth:
            continue
        for name in files:
            if not name.lower().endswith(".exe"):
                continue
            # Skip obvious helper exes (uninstallers, crash reporters, etc.)
            if any(skip in name.lower() for skip in
                   ("unins", "setup", "crash", "redist", "updater",
                    "helper", "service")):
                continue
            s = _score(app_name, name)
            if s > best_score:
                best, best_score = os.path.join(dirpath, name), s
    if best and best_score >= LOW_CONFIDENCE:
        return best
    # Fallback: if exactly one non-helper exe lives at the root, take it.
    direct = [n for n in os.listdir(folder)
              if n.lower().endswith(".exe")
              and not any(skip in n.lower() for skip in
                          ("unins", "setup", "crash", "redist", "updater",
                           "helper", "service"))]
    if len(direct) == 1:
        return os.path.join(folder, direct[0])
    return best


def _scan_launchers(query):
    """Steam / Battle.net / Epic-installed games."""
    # Steam: each game lives in <library>/<GameFolder>/.
    for lib in _steam_libraries():
        try:
            for entry in os.listdir(lib):
                full = os.path.join(lib, entry)
                if not os.path.isdir(full):
                    continue
                s = _score(query, entry)
                if s < LOW_CONFIDENCE:
                    continue
                exe = _best_exe_in_folder(full, query)
                if exe:
                    yield s, entry, exe
        except OSError:
            continue
    # Battle.net: scan its parent for the game subfolders.
    bn = _battle_net_path()
    if bn:
        parent = os.path.dirname(bn)
        try:
            for entry in os.listdir(parent):
                full = os.path.join(parent, entry)
                if not os.path.isdir(full) or entry.lower() == "battle.net":
                    continue
                s = _score(query, entry)
                if s < LOW_CONFIDENCE:
                    continue
                exe = _best_exe_in_folder(full, query)
                if exe:
                    yield s, entry, exe
        except OSError:
            pass
    # Epic.
    for display, full in _epic_manifests():
        s = _score(query, display)
        if s >= LOW_CONFIDENCE:
            yield s, display, full


# --- source 4: filesystem walk (last resort) ---
_FS_ROOTS = (
    "%LOCALAPPDATA%\\Programs",
    "%PROGRAMFILES%",
    "%PROGRAMFILES(X86)%",
)


def _scan_filesystem(query, time_budget=5.0):
    """Walk common Program-Files-style roots, budget capped."""
    deadline = time.monotonic() + time_budget
    for raw in _FS_ROOTS:
        root = os.path.expandvars(raw)
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            if time.monotonic() > deadline:
                return
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            s = _score(query, entry)
            if s < LOW_CONFIDENCE:
                continue
            exe = _best_exe_in_folder(full, query)
            if exe:
                yield s, entry, exe


# --- top-level find() ---
def find(query, time_budget=5.0):
    """Search all sources and return the best candidates.

    Returns a list of `(score, display_name, exe_path)` sorted high to low,
    or an empty list if nothing crossed LOW_CONFIDENCE in any source.
    """
    if not query or not query.strip():
        return []
    results = []
    # Source 1: Start Menu (fast)
    results.extend(_scan_start_menu(query))
    # Source 2: registry (fast)
    results.extend(_scan_uninstall(query))
    # Source 3: launchers (medium)
    results.extend(_scan_launchers(query))
    # Source 4: filesystem (slow) -- only if nothing confident so far.
    if not any(s >= HIGH_CONFIDENCE for s, *_ in results):
        results.extend(_scan_filesystem(query, time_budget=time_budget))

    # Deduplicate by exe path: keep the highest-scoring entry per path.
    by_path = {}
    for s, display, exe in results:
        key = os.path.normcase(os.path.normpath(exe))
        prev = by_path.get(key)
        if prev is None or s > prev[0]:
            by_path[key] = (s, display, exe)
    out = sorted(by_path.values(), key=lambda r: r[0], reverse=True)
    return out


def launch(exe_path):
    """Spawn the resolved .exe in its own process."""
    try:
        subprocess.Popen([os.path.expandvars(exe_path)])
        return True
    except OSError:
        return False
