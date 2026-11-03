"""Project path shim.

After the 2026-05-25 reorg, source files live in subfolders (audio/, llm/,
features/, ui/, setup/) but all imports stayed flat (`import tts`, `import
agenda`, etc) instead of being rewritten as package imports. This module
puts every subfolder on sys.path so those flat imports keep resolving.

It also exposes `ROOT` — the absolute project root — so any moved file
that needs to anchor a data path (memory.json, agenda.json, voices/, etc)
can do `import _paths; _HERE = _paths.ROOT` instead of relying on
`os.path.dirname(__file__)`, which now points at the subfolder.

Import this once near the top of any entry-point script (orion.py and
anything spawned via subprocess). Module imports triggered later
inherit the patched sys.path automatically.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

_SUBDIRS = ("audio", "llm", "features", "ui", "setup")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

for _sub in _SUBDIRS:
    _p = os.path.join(ROOT, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
