"""Native-Windows compatibility bootstrap.

On Windows, Python defaults its text IO to the platform code page (cp1252), so
writing a non-cp1252 glyph (e.g. the "✓" status char) raises
``UnicodeEncodeError``. That crashes ``config list`` and, worse, tears down the
host<->server WebSocket tunnel so the daemon never comes online.

This module is imported at the package boundary (from ``omnigent/__init__``) so
every process — the CLI, the local server, the host daemon, the runner, and
harness subprocesses spawned via ``-m omnigent`` — forces UTF-8 IO before any
output happens. It is a no-op on non-Windows hosts.

POSIX ``os.getuid()`` calls in the ``*_native_bridge.py`` modules are handled at
their call sites via :func:`omnigent._platform.stable_user_id`, not by
monkeypatching ``os`` here — a global ``os.getuid``/``geteuid`` shim would make
Windows look like POSIX root and perturb stdlib behaviour such as ``tarfile``
ownership handling.
"""

from __future__ import annotations

import contextlib
import os
import sys


def _force_utf8_io() -> None:
    # Propagate to child processes: the daemon/runner env is scrubbed, so an
    # inherited value is the only thing that reaches them reliably.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # Raises when the stream is detached/closed (e.g. under capture).
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def apply() -> None:
    if os.name != "nt":
        return
    _force_utf8_io()
