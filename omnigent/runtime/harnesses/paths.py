"""Filesystem paths shared by host and harness-process lifecycle code."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from omnigent._platform import IS_WINDOWS

HARNESS_TMP_PARENT_ENV_VAR = "OMNIGENT_HARNESS_TMP_PARENT"


def harness_tmp_parent(env: Mapping[str, str] | None = None) -> Path:
    """Return the configured harness socket root without changing relativity."""
    source = os.environ if env is None else env
    configured = source.get(HARNESS_TMP_PARENT_ENV_VAR)
    if configured:
        return Path(configured).expanduser()
    if IS_WINDOWS:
        return Path(tempfile.gettempdir()) / "omnigent"
    return Path(f"/tmp/omnigent-{os.getuid()}")


def absolute_harness_tmp_parent(path: Path) -> Path:
    """Make a harness socket root absolute without resolving symlinks."""
    return Path(os.path.abspath(path.expanduser()))


def resolve_harness_tmp_parent(env: Mapping[str, str] | None = None) -> Path:
    """Return one short absolute socket root for cross-process propagation."""
    return absolute_harness_tmp_parent(harness_tmp_parent(env))
