#!/usr/bin/env python3
"""Cross-platform launcher for the analysis layer.

    python rbi.py chunk --all-master-directions
    python rbi.py extract
    python rbi.py scaffold
    python rbi.py enrich
    python rbi.py validate

Use this instead of `PYTHONPATH=python python3 -m rbi_intel ...`.

That form is POSIX shell syntax: `VAR=value command` sets an environment
variable for one command in bash and zsh, but Windows `cmd.exe` reads it as a
program name and fails with

    'PYTHONPATH' is not recognized as an internal or external command

`python3` is also usually absent on Windows, where the interpreter is `python`.
Both of those are real papercuts on the machine this actually runs on, so the
launcher removes the need for either: it puts `python/` on `sys.path` itself
and hands off to the same CLI.

Everything after the script name is passed straight through, so every flag
documented for `python -m rbi_intel` works here unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE_DIR = ROOT / "python"

if not (PACKAGE_DIR / "rbi_intel" / "__main__.py").exists():
    sys.exit(
        f"Cannot find the rbi_intel package under {PACKAGE_DIR}.\n"
        "Run this script from inside the rbi-intel folder — the one containing "
        "package.json, python/ and seed/."
    )

sys.path.insert(0, str(PACKAGE_DIR))

try:
    from rbi_intel.__main__ import main
except ImportError as e:  # pragma: no cover - import-time environment problem
    sys.exit(
        f"Failed to import rbi_intel: {e}\n"
        f"Python {sys.version.split()[0]} at {sys.executable}\n"
        "This package needs Python 3.10 or later."
    )

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
