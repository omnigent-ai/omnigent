"""Native-Windows compatibility bootstrap.

omnigent grew up on Linux/macOS and bakes in a few POSIX assumptions that
crash on native Windows before any feature code runs. This module is imported
at the package boundary (from ``omnigent/__init__``) so every process — the
CLI, the local server, the host daemon, the runner, and harness subprocesses
spawned via ``-m omnigent`` — installs these shims before anything else runs.
It is a no-op on non-Windows hosts.

Two classes of breakage are handled:

1. POSIX-only ``os.getuid`` / ``os.getgid`` are called at *module import time*
   in the ``*_native_bridge.py`` modules (to name a per-user temp bridge root).
   On Windows these attributes don't exist, so importing those modules — which
   the server does transitively while registering the default Claude agent —
   raises ``AttributeError`` and aborts server startup.

2. Python's default text encoding on Windows is the legacy code page (cp1252),
   so writing a non-cp1252 glyph (e.g. the ``✓`` status char) raises
   ``UnicodeEncodeError``. That crashes ``config list`` and, worse, tears down
   the host<->server WebSocket tunnel so the daemon never comes online.
"""

from __future__ import annotations

import os
import sys


def _install_posix_id_shims() -> None:
    # The native bridges only use these to name a per-user temp dir, and the
    # native (tmux/PTY) harnesses that consume it don't run on Windows, so a
    # stable constant is enough to make those modules import cleanly.
    for _name in ("getuid", "getgid", "geteuid", "getegid"):
        if not hasattr(os, _name):
            setattr(os, _name, lambda: 0)


def _force_utf8_io() -> None:
    # Propagate to child processes (the daemon/runner env is scrubbed, so an
    # inherited value is the only thing that reaches them reliably).
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def apply() -> None:
    if os.name != "nt":
        return
    _install_posix_id_shims()
    _force_utf8_io()
