"""Shared tmux compatibility checks for managed terminals."""

from __future__ import annotations

import re
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
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _TMUX_VERSION_RE.search(result.stdout) if result.returncode == 0 else None
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))
