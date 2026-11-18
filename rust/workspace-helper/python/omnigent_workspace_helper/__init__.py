"""Locate the ``omnigent-workspace-helper`` executable this wheel installs."""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

EXECUTABLE = "omnigent-workspace-helper"


def find_binary() -> Path:
    """Return the helper executable installed with this package.

    Looks where pip and uv put a wheel's scripts: the environment's scripts
    directory, then the user scheme's, then ``bin`` beside the package for a
    ``pip install --target`` tree.

    :returns: The executable's path, e.g. ``Path("/venv/bin/omnigent-workspace-helper")``.
    :raises FileNotFoundError: When none of those holds the executable.
    """
    name = EXECUTABLE + (sysconfig.get_config_var("EXE") or "")
    candidates = [Path(sysconfig.get_path("scripts")) / name]
    user_scheme = sysconfig.get_preferred_scheme("user")
    candidates.append(Path(sysconfig.get_path("scripts", scheme=user_scheme)) / name)
    candidates.append(Path(__file__).resolve().parent.parent / "bin" / name)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(
        f"{EXECUTABLE} is not installed for {sys.executable}; looked in "
        + ", ".join(str(candidate.parent) for candidate in candidates)
    )


__all__ = ["EXECUTABLE", "find_binary"]
