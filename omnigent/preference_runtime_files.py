"""Disposable runner handoffs; resumable choices remain in the session store."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from filelock import FileLock, Timeout

from omnigent.native.owner_claim import owner_is_gone, read_owner_claim, write_owner_claim
from omnigent.preference_startup import (
    PREFERENCE_SNAPSHOT_ENV,
    PreferenceLaunch,
    write_launch_snapshot,
)
from omnigent.process_logging import data_dir

_logger = logging.getLogger(__name__)


def _root() -> Path:
    return data_dir().resolve() / "preferences" / "runtime"


def write_runtime_snapshot(launch: PreferenceLaunch) -> Path:
    """Keep the launching process as owner until its runner claims the handoff."""
    root = _root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(root / ".lock"), timeout=5):
        directory = Path(tempfile.mkdtemp(prefix="launch-", dir=root))
        try:
            write_owner_claim(directory)
            path = directory / "preferences.json"
            write_launch_snapshot(path, launch)
            return path
        except BaseException:
            shutil.rmtree(directory)
            raise


def claim_runtime_snapshot() -> None:
    """Claim only a disposable handoff, leaving legacy resume files untouched."""
    value = os.environ.get(PREFERENCE_SNAPSHOT_ENV)
    if not value:
        return
    path = Path(value)
    root = _root()
    if path.name != "preferences.json" or path.parent.parent != root or path.parent.is_symlink():
        return
    with FileLock(str(root / ".lock"), timeout=5):
        if not path.is_file():
            raise ValueError("Session preference handoff is missing; retry the launch")
        write_owner_claim(path.parent)


def prune_runtime_snapshots() -> int:
    """Remove only known-dead owners in this boot and PID namespace."""
    from omnigent.inner.terminal import _process_alive

    root = _root()
    if not root.exists():
        return 0
    removed = 0
    try:
        with FileLock(str(root / ".lock"), timeout=0):
            for directory in root.iterdir():
                if (
                    not directory.name.startswith("launch-")
                    or not directory.is_dir()
                    or directory.is_symlink()
                ):
                    continue
                claim = read_owner_claim(directory)
                if claim is not None and owner_is_gone(claim, process_alive=_process_alive):
                    try:
                        shutil.rmtree(directory)
                        removed += 1
                    except OSError:
                        _logger.warning(
                            "Could not remove preference handoff %s", directory, exc_info=True
                        )
    except Timeout:
        pass
    return removed
