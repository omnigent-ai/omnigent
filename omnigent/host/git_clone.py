"""Clone a repository into a new host directory for project creation."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

from omnigent.host.frames import HostCloneRepositoryFrame, HostCloneRepositoryResultFrame
from omnigent.inner import _proc
from omnigent.onboarding.sandboxes.types import GitCloneOptions

CLONE_TIMEOUT_S = 360.0
_logger = logging.getLogger(__name__)


def _retry_writable(remove: Callable[[str], object], path: str, _error: BaseException) -> None:
    """Git marks objects read-only; Windows refuses to delete them until writable."""
    os.chmod(path, stat.S_IWRITE)
    remove(path)


def clone_repository(frame: HostCloneRepositoryFrame) -> HostCloneRepositoryResultFrame:
    """
    Clone ``frame.url`` into ``frame.path``, which must not exist yet.

    The destination is reserved with an exclusive ``mkdir`` so an existing
    checkout, file or symlink is never overwritten. A failed or timed-out
    clone removes only the directory this call created. Git output is
    discarded because credential helpers may print secrets.
    """

    def failed(code: str, message: str) -> HostCloneRepositoryResultFrame:
        return HostCloneRepositoryResultFrame(
            request_id=frame.request_id, status="failed", error=message, error_code=code
        )

    try:
        destination = Path(frame.path).expanduser()
    except RuntimeError:  # ~otheruser that this host can't resolve
        destination = Path(frame.path)
    if not destination.is_absolute() or destination.name in ("", ".", ".."):
        return failed("invalid_input", "Choose an absolute or ~/ path for the new checkout")
    try:
        destination.mkdir()
        reserved = destination.stat()
    except FileExistsError:
        return failed("clone_destination_conflict", "The clone destination already exists")
    except OSError:
        return failed("clone_destination_conflict", "The parent folder is unavailable")
    command = [
        "git",
        "clone",
        *GitCloneOptions(single_branch=False).clone_args(frame.branch),
        "--",
        frame.url,
        str(destination),
    ]
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    returncode: int | None
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            **_proc.spawn_kwargs(),
        ) as process:
            try:
                returncode = process.wait(timeout=CLONE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                _proc.kill_tree(process)
                process.wait()
                returncode = None
    except OSError:
        returncode = -1
    if returncode == 0:
        return HostCloneRepositoryResultFrame(
            request_id=frame.request_id, status="ok", path=str(destination)
        )
    try:
        current = destination.stat(follow_symlinks=False)
        # Someone may have replaced the directory meanwhile; leave theirs alone.
        if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
            shutil.rmtree(destination, onexc=_retry_writable)
    except OSError:
        _logger.warning("Could not remove incomplete checkout %s", destination, exc_info=True)
    if returncode is None:
        return failed("clone_timeout", "The clone took too long and was stopped")
    return failed(
        "clone_failed",
        "Git could not clone the repository. Check the URL, branch and this host's Git access",
    )
