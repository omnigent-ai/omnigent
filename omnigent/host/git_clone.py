"""Clone a repository into a new, operation-owned host directory."""

from __future__ import annotations

import contextlib
import ntpath
import os
import shutil
import subprocess
from pathlib import Path

from omnigent.host.frames import HostCloneRepositoryFrame, HostCloneRepositoryResultFrame
from omnigent.inner import _proc
from omnigent.onboarding.sandboxes.repositories import normalize_repository
from omnigent.onboarding.sandboxes.types import GitCloneOptions

CLONE_TIMEOUT_S = 360.0


def validate_clone_path(path: str) -> str:
    """Check portable path syntax; expansion belongs to the target host."""
    if not path.strip() or any(ord(character) < 32 for character in path):
        raise ValueError("Clone destination must be a nonempty path without control characters")
    if not (
        path.startswith(("/", "~/", "~\\", "\\\\"))
        or (ntpath.isabs(path) and ntpath.splitdrive(path)[0])
    ):
        raise ValueError("Clone destination must be absolute or start with ~/")
    return path


def clone_repository(frame: HostCloneRepositoryFrame) -> HostCloneRepositoryResultFrame:
    """Reserve a new leaf, clone with a bounded process tree, and retain only success."""
    destination: Path | None = None
    reserved: os.stat_result | None = None
    complete = False
    error_code = "invalid_input"
    try:
        repository = normalize_repository(
            {"url": frame.url, **({"branch": frame.branch} if frame.branch is not None else {})}
        )
        validate_clone_path(frame.path)
        requested = Path(os.path.expanduser(frame.path))
        if not requested.is_absolute() or requested.name in ("", ".", ".."):
            raise ValueError("Clone destination must be an absolute new directory")
        error_code = "clone_destination_conflict"
        destination = requested.parent.resolve(strict=True) / requested.name
        destination.mkdir()
        reserved = destination.stat(follow_symlinks=False)
        error_code = "clone_failed"
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -oBatchMode=yes",
            "SSH_ASKPASS_REQUIRE": "never",
        }
        command = [
            "git",
            "clone",
            *GitCloneOptions().clone_args(repository.get("branch")),
            "--",
            repository["url"],
            str(destination),
        ]
        # Git helpers may report credentials in stderr. Keep failures generic.
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
                error_code = "clone_timeout"
                raise ValueError(
                    "Repository clone exceeded its deadline; the clone process was stopped"
                ) from None
        if returncode:
            raise ValueError(
                "Git could not clone the repository. Check repository access, branch, "
                "and Git credentials on this host"
            )
        complete = True
        return HostCloneRepositoryResultFrame(
            request_id=frame.request_id, status="ok", path=str(destination)
        )
    except (OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else (
                "Clone destination already exists or its parent is unavailable"
                if error_code == "clone_destination_conflict"
                else "Git could not start or access the clone destination"
            )
        )
        return HostCloneRepositoryResultFrame(
            request_id=frame.request_id, status="failed", error=message, error_code=error_code
        )
    finally:
        if reserved is not None and destination is not None and not complete:
            # A renamed/replaced leaf belongs to someone else, even after failure.
            with contextlib.suppress(OSError):
                current = destination.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
                    shutil.rmtree(destination)
