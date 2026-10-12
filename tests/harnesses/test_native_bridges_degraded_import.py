"""Native harness bridges must import on a host without ``os.getuid``.

A module-level ``os.getuid`` lookup in a bridge would crash host/server startup
on Windows, where that attribute does not exist.
"""

from __future__ import annotations

import subprocess
import sys


def test_native_bridges_import_without_os_getuid() -> None:
    """Every native bridge must import when os.getuid is unavailable."""
    # Discover the bridges dynamically so a new harness is covered automatically.
    child = (
        "import os\n"
        "if hasattr(os, 'getuid'): delattr(os, 'getuid')\n"
        "import importlib, pkgutil\n"
        "import omnigent.harnesses as h\n"
        "mods = [m.name for m in pkgutil.iter_modules(h.__path__, 'omnigent.harnesses.')\n"
        "        if m.name.endswith('_native')]\n"
        "assert mods, 'no native harness bridges discovered'\n"
        "for m in mods:\n"
        "    importlib.import_module(m + '.bridge')\n"
        "print('BRIDGES_IMPORT_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"native bridge import requires os.getuid (server startup would crash "
        f"on Windows):\n{proc.stderr}"
    )
    assert "BRIDGES_IMPORT_OK" in proc.stdout
