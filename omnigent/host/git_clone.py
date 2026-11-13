"""Clone a git repository into a new directory on the host."""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from pathlib import Path

from omnigent.host.frames import HostCloneRepositoryFrame, HostCloneRepositoryResultFrame
from omnigent.inner import _proc
from omnigent.onboarding.sandboxes.types import GitCloneOptions

# Bounds one clone; the server waits a margin longer before giving up on the host.
CLONE_TIMEOUT_S = 360.0
_logger = logging.getLogger(__name__)


def clone_repository(frame: HostCloneRepositoryFrame) -> HostCloneRepositoryResultFrame:
    """
    Clone ``frame.url`` into ``frame.path``, which must not exist yet.

    The destination is reserved with an exclusive ``mkdir``, so an existing
    file, directory or symlink is never touched. A failed clone removes only
    the empty directory it reserved; a timed-out clone is left for the user to
    delete. Git output is discarded because credential helpers may print secrets.

    :param frame: The clone request, e.g. ``url="https://git.example.com/team/app.git"``
        and ``path="~/code/app"``.
    :returns: The result frame with the checkout path, or a classified failure.
    """

    def failed(code: str, message: str) -> HostCloneRepositoryResultFrame:
        return HostCloneRepositoryResultFrame(
            request_id=frame.request_id, status="failed", error=message, error_code=code
        )

    try:
        destination = Path(frame.path).expanduser()
    except RuntimeError:  # ``~user`` this host cannot resolve
        destination = Path(frame.path)
    if not destination.is_absolute() or destination.name in ("", ".", ".."):
        return failed("invalid_input", "Choose an absolute or ~/ path for the new checkout")
    try:
        destination.mkdir()
        reserved = destination.stat(follow_symlinks=False)
    except FileExistsError:
        return failed("clone_destination_conflict", f"{destination} already exists")
    except OSError:
        return failed(
            "invalid_input", f"The parent folder of {destination} doesn't exist or isn't writable"
        )

    command = [
        "git",
        "clone",
        *GitCloneOptions(single_branch=False).clone_args(frame.branch),
        "--",
        frame.url,
        str(destination),
    ]
    # No prompts: an empty GIT_ASKPASS also overrides an inherited SSH_ASKPASS.
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS_REQUIRE": "never",
        "GCM_INTERACTIVE": "never",
    }
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
        _logger.warning("Could not start git for clone into %s", destination, exc_info=True)
        returncode = -1

    if returncode == 0:
        _logger.info("Cloned %s into %s", frame.url, destination)
        return HostCloneRepositoryResultFrame(
            request_id=frame.request_id, status="ok", path=str(destination)
        )
    if returncode is None:
        # Partial files stay put: deleting them could race a user who has since
        # begun using the directory, so the user removes it before retrying.
        return failed(
            "clone_timeout",
            f"The clone took too long and was stopped. Delete {destination} before retrying",
        )
    # Remove the reserved directory only while it is still the one we created,
    # and rmdir refuses to delete anything the clone left behind.
    with contextlib.suppress(OSError):
        current = destination.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
            os.rmdir(destination)
    return failed(
        "clone_failed",
        "Git could not clone the repository. Check the URL, branch and this host's Git access",
    )
