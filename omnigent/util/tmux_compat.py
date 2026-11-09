"""Shared tmux compatibility checks for managed terminals."""

from __future__ import annotations

import re
import shutil
import subprocess

# Managed terminals enable ``allow-passthrough``, which tmux added in 3.3.
MIN_TMUX_VERSION = (3, 3)
MIN_TMUX_VERSION_HINT = ".".join(str(part) for part in MIN_TMUX_VERSION)
_TMUX_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def tmux_version(tmux_path: str) -> tuple[int, int] | None:
    """Return the installed tmux major/minor version, or ``None`` if unknown.

    :param tmux_path: Absolute path to tmux, as resolved by
        :func:`shutil.which`.
    :returns: The ``(major, minor)`` pair reported by ``tmux -V``. Release
        suffixes such as ``3.3a`` are ignored.
    """
    try:
        result = subprocess.run(
            [tmux_path, "-V"],
            capture_output=True,
            text=True,
            # Undecodable output must read as an unknown version, not raise.
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _TMUX_VERSION_RE.search(result.stdout) if result.returncode == 0 else None
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def tmux_unavailable_reason() -> str | None:
    """Return why managed terminals can't use this host's tmux, or ``None``.

    Resolves ``tmux`` on the current ``PATH`` each call, so a tmux installed
    after startup is picked up without a restart.

    :returns: A client-safe remedy, e.g. ``"tmux 3.2 is too old; ..."``, or
        ``None`` when tmux is on ``PATH`` at :data:`MIN_TMUX_VERSION` or newer.
    """
    tmux = shutil.which("tmux")
    if tmux is None:
        return (
            "tmux is not installed or not on PATH. Native terminals need tmux "
            f"{MIN_TMUX_VERSION_HINT} or newer: install it (macOS: `brew install tmux`) and "
            "check that `tmux -V` works in a terminal. If Omnigent still can't find it, "
            "quit and reopen the Omnigent app, or restart a CLI-started host from a shell "
            "where `tmux -V` works."
        )
    version = tmux_version(tmux)
    if version is None:
        return (
            "Could not determine the installed tmux version; managed terminals require "
            f"tmux {MIN_TMUX_VERSION_HINT} or newer"
        )
    if version < MIN_TMUX_VERSION:
        detected = f"{version[0]}.{version[1]}"
        return (
            f"tmux {detected} is too old; managed terminals require "
            f"tmux {MIN_TMUX_VERSION_HINT} or newer"
        )
    return None
