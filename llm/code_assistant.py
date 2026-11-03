"""Orion's Code Assistant: reads the file you have open in your editor.

Instead of screenshotting the screen, this finds the file currently focused in
VS Code (or a similar editor) by parsing its title bar, locates that file on
disk, and reads the raw source. The full code -- not just the visible portion
-- is then sent to the AI for review, fixes, explanation, refactoring, tests,
or comments.
"""

import os

try:
    import pygetwindow as gw
except Exception:  # pygetwindow unavailable on this platform
    gw = None

# Title-bar separators used by common editors (em dash, en dash, hyphen).
_TITLE_SEPARATORS = (" — ", " – ", " - ")

# Editors whose title bar shows the open file name.
_EDITOR_HINTS = (
    "visual studio code", "vscode", "- code", "sublime text", "pycharm",
    "notepad++", "atom", "cursor",
)

# Folders skipped while searching the disk for the open file.
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "venv", ".venv", "env",
    "site-packages", "dist", "build", ".idea", ".vscode", "AppData",
}

# Where to look for the open file, in priority order.
_SEARCH_ROOTS = [
    os.getcwd(),
    os.path.join(os.path.expanduser("~"), "Desktop"),
    os.path.join(os.path.expanduser("~"), "Documents"),
    os.path.join(os.path.expanduser("~"), "source"),
    os.path.join(os.path.expanduser("~"), "Projects"),
    os.path.join(os.path.expanduser("~"), "Code"),
]

# Don't paste enormous files into the prompt.
_MAX_FILE_BYTES = 200_000


def _parse_title(title):
    """Pull (filename, project_hint) out of an editor title bar string.

    A VS Code title looks like "app.py - myproject - Visual Studio Code".
    Returns (None, None) if the title isn't from a recognised editor.
    """
    if not title:
        return None, None
    low = title.lower()
    if not any(hint in low for hint in _EDITOR_HINTS):
        return None, None

    parts = None
    for sep in _TITLE_SEPARATORS:
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            break
    if not parts:
        return None, None

    # First segment is the file name; VS Code prefixes "● " for unsaved files.
    filename = parts[0].lstrip("●* ").strip()
    if not filename or "." not in filename:
        return None, None

    # A middle segment, if present, is usually the project/folder name.
    project = parts[1] if len(parts) >= 3 else None
    return filename, project


def detect_open_file():
    """Return (filename, project_hint) for the focused editor, or (None, None)."""
    if gw is None:
        return None, None
    try:
        win = gw.getActiveWindow()
    except Exception:
        return None, None
    if win is None:
        return None, None
    return _parse_title(getattr(win, "title", "") or "")


def _find_on_disk(filename, project_hint=None):
    """Search the common code roots for `filename`; return its full path or None.

    When several files share the name, a path containing `project_hint` wins.
    """
    matches = []
    seen_roots = set()
    for root in _SEARCH_ROOTS:
        root = os.path.abspath(root)
        if root in seen_roots or not os.path.isdir(root):
            continue
        seen_roots.add(root)
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if filename in files:
                matches.append(os.path.join(dirpath, filename))
        if matches:
            break

    if not matches:
        return None
    if project_hint:
        hint = project_hint.lower()
        for path in matches:
            if hint in path.lower():
                return path
    return matches[0]


def read_open_file():
    """Read the file currently open in the editor.

    Returns (path, source) on success, or (None, error_message) on failure.
    """
    filename, project = detect_open_file()
    if not filename:
        return None, ("I couldn't tell which file you have open. Make sure "
                      "your code editor is the active window.")

    path = _find_on_disk(filename, project)
    if not path:
        return None, f"I found the file name {filename}, but not the file on disk."

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > _MAX_FILE_BYTES:
        return None, f"The file {filename} is too large for me to send to the AI."

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return None, f"I couldn't read {filename} from disk."

    if not source.strip():
        return None, f"The file {filename} looks empty."
    return path, source


def build_prompt(instruction, path, source):
    """Wrap the file's source in an instruction for the AI.

    `instruction` describes the task (review, fix, explain, etc.).
    """
    name = os.path.basename(path)
    numbered = "\n".join(
        f"{i:>4}  {line}" for i, line in enumerate(source.splitlines(), 1)
    )
    return (
        f"{instruction}\n\n"
        f"The file is {name}. Here is its full source, with line numbers:\n\n"
        f"{numbered}"
    )


# Spoken instructions for each code task. Phrased so the AI's reply works as
# speech while still naming the relevant line numbers.
INSTRUCTIONS = {
    "code_review": (
        "Review this code for bugs, errors, and likely problems. For each "
        "issue, say what is wrong and which line number it is on. If the code "
        "looks correct, say so."
    ),
    "code_fix": (
        "Find the bugs in this code and give me the corrected version. "
        "Explain what you changed and on which line numbers."
    ),
    "code_explain": (
        "Explain what this code does, walking through it clearly."
    ),
    "code_refactor": (
        "Refactor this code to make it cleaner and easier to read without "
        "changing what it does. Explain the key changes and their line numbers."
    ),
    "code_tests": (
        "Write unit tests for this code. Briefly describe what each test "
        "covers."
    ),
    "code_comments": (
        "Add clear, useful comments to this code and give me the commented "
        "version. Do not over-comment obvious lines."
    ),
}
