"""Locate an interpreter capable of running setup.py for packaging tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def python_with_setuptools() -> str | None:
    """Return a python interpreter able to run setup.py, or None.

    The test venv may not carry setuptools, so packaging tests drive
    ``setup.py`` through whichever available interpreter can import it.
    """
    candidates = [sys.executable, shutil.which("python3"), "/usr/bin/python3"]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen or not Path(candidate).exists():
            continue
        seen.add(candidate)
        probe = subprocess.run(
            [candidate, "-c", "import setuptools"],
            capture_output=True,
            timeout=60,
        )
        if probe.returncode == 0:
            return candidate
    return None
