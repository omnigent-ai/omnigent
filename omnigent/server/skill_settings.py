"""Persisted server default for cross-provider skill trust."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Literal

from omnigent.server.admin_list import resolve_data_dir

SkillTrust = Literal["current", "all-host"]
_FILENAME = "skill_trust"


def skill_trust_path() -> Path:
    """Return the persisted trust-setting path."""
    return resolve_data_dir() / _FILENAME


def read_skill_trust() -> SkillTrust:
    """Read the safe default when the file is absent or invalid."""
    try:
        value = skill_trust_path().read_text().strip()
    except OSError:
        return "current"
    return value if value in ("current", "all-host") else "current"  # type: ignore[return-value]


def write_skill_trust(value: SkillTrust) -> None:
    """Atomically persist the server trust default."""
    if value not in ("current", "all-host"):
        raise ValueError(value)
    path = skill_trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{_FILENAME}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
