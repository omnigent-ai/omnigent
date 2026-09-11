"""Where the Claude CLI keeps its per-user files.

``CLAUDE_CONFIG_DIR`` selects a Claude profile. Anything Omnigent reads or writes
on Claude's behalf has to follow the same choice, or it lands in the wrong profile.
"""

from __future__ import annotations

import os
from pathlib import Path


def _configured_dir() -> Path | None:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else None


def claude_config_dir() -> Path:
    """Return Claude's config dir: ``$CLAUDE_CONFIG_DIR`` when set, else ``~/.claude``.

    :returns: The directory holding ``settings.json``, ``projects/`` and ``sessions/``.
    """
    return _configured_dir() or Path.home() / ".claude"


def claude_json_path() -> Path:
    """Return Claude's user-scope config file.

    It lives inside ``$CLAUDE_CONFIG_DIR`` when that is set, and otherwise in the
    home directory beside ``~/.claude`` rather than inside it.

    :returns: ``$CLAUDE_CONFIG_DIR/.claude.json`` or ``~/.claude.json``.
    """
    return (_configured_dir() or Path.home()) / ".claude.json"
