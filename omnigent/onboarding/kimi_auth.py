"""Detect an existing Kimi Code login from its local credential directories."""

from __future__ import annotations

import os
from pathlib import Path

KIMI_CODE_HOME_ENV_VAR = "KIMI_CODE_HOME"


def kimi_credential_dirs(home: Path | None = None) -> tuple[Path, ...]:
    """Return the credential directories used by current and legacy Kimi Code."""
    if home is not None:
        return (home / ".kimi" / "credentials", home / ".kimi-code" / "credentials")
    configured_home = os.environ.get(KIMI_CODE_HOME_ENV_VAR)
    default_home = Path.home()
    paths = [default_home / ".kimi" / "credentials", default_home / ".kimi-code" / "credentials"]
    if configured_home:
        paths.insert(0, Path(configured_home).expanduser() / "credentials")
    return tuple(dict.fromkeys(paths))


def kimi_login_detected(home: Path | None = None) -> bool:
    """Return whether any known Kimi credential directory contains a file."""
    for directory in kimi_credential_dirs(home):
        try:
            if directory.is_dir() and any(path.is_file() for path in directory.iterdir()):
                return True
        except OSError:
            continue
    return False
