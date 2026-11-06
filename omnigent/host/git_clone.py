"""Host-side ``git clone`` for repositories a user adds from the Web UI.

Runs in response to ``host.clone_repository`` frames. Git runs with the host
user's own configuration and credentials, through an argv list (never a shell),
with interactive prompts disabled so a missing credential fails instead of
waiting on a terminal nobody is watching.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

# Default budget for one clone. Large monorepos legitimately take many minutes.
CLONE_REPOSITORY_BUDGET_S: float = 1800.0

# Metadata probes on an existing checkout finish well within this.
_PROBE_TIMEOUT_S: float = 30.0

# Mirrors the server's accepted repository URL forms. Checked again here so a
# frame can never smuggle a git transport such as ``ext::`` or a local path.
# A leading ``-`` in the host would reach ssh as an option.
_ACCEPTED_URL = re.compile(
    r"^(?:https://[^\s@/#-][^\s@/#]*/[^\s@#]+|git@[^\s@:#-][^\s@:#]*:[^\s@#]+)$"
)
_DIRECTORY_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# Keep the reported git failure short enough for an inline dialog error.
_MAX_ERROR_LINES = 4
_MAX_ERROR_CHARS = 600

_inflight_lock = threading.Lock()
_inflight_destinations: set[str] = set()


class CloneError(Exception):
    """Raised when a repository cannot be cloned.

    :param message: User-facing reason, e.g. ``"repository not found"``.
    :param conflict: ``True`` when the destination is taken rather than git
        failing, so the server can answer 409 instead of 422.
    """

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        """Store the user-facing message and its category.

        :param message: Error text surfaced to the API caller.
        :param conflict: Whether the destination path is the problem.
        """
        super().__init__(message)
        self.message = message
        self.conflict = conflict


@dataclass(frozen=True)
class ClonedRepository:
    """A checkout ready to use as a working directory.

    :param path: Absolute checkout path, e.g. ``"/Users/me/code/repo"``.
    :param reused_existing: ``True`` when the destination already held a
        checkout of the requested remote and was returned without cloning.
    """

    path: str
    reused_existing: bool


def _git_env() -> dict[str, str]:
    """Return the environment for every git command this module runs.

    :returns: The host environment with prompts disabled and messages in C locale.
    """
    return {
        **os.environ,
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
    }


def _run_git(args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run one git command detached from any controlling terminal.

    :param args: Git argv after ``git``, e.g. ``["clone", "--", url, dest]``.
    :param timeout_s: Seconds before git is killed.
    :returns: The completed process with captured text output.
    :raises FileNotFoundError: If git is not installed.
    :raises subprocess.TimeoutExpired: If git exceeds ``timeout_s``.
    """
    return subprocess.run(
        ["git", *args],
        env=_git_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        # A new session has no controlling terminal, so ssh cannot stop to ask
        # for a passphrase or host-key confirmation on the daemon's tty.
        start_new_session=True,
    )


def _normalize_remote(url: str) -> str:
    """Normalize a remote URL for same-repository comparison.

    :param url: Remote URL, e.g. ``"https://github.com/org/repo.git/"``.
    :returns: The URL without trailing slashes or ``.git``, e.g.
        ``"https://github.com/org/repo"``.
    """
    trimmed = url.strip().rstrip("/")
    return trimmed[: -len(".git")] if trimmed.endswith(".git") else trimmed


def _existing_checkout_matches(destination: str, url: str) -> bool:
    """Return whether ``destination`` is already a checkout of ``url``.

    :param destination: Absolute path that already exists.
    :param url: Requested clone URL.
    :returns: ``True`` only when ``destination`` is a work-tree root whose
        ``origin`` remote is the same repository.
    """
    if not os.path.isdir(destination):
        return False
    try:
        toplevel = _run_git(
            ["-C", destination, "rev-parse", "--show-toplevel"], timeout_s=_PROBE_TIMEOUT_S
        )
        if toplevel.returncode != 0:
            return False
        if os.path.realpath(toplevel.stdout.strip()) != os.path.realpath(destination):
            return False
        origin = _run_git(
            ["-C", destination, "config", "--get", "remote.origin.url"],
            timeout_s=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return origin.returncode == 0 and _normalize_remote(origin.stdout) == _normalize_remote(url)


def _summarize_git_error(stderr: str) -> str:
    """Reduce git's stderr to the lines that explain the failure.

    :param stderr: Captured stderr from ``git clone``.
    :returns: The last few meaningful lines, e.g.
        ``"fatal: repository 'https://github.com/org/missing/' not found"``.
    """
    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.startswith("Cloning into")
    ]
    summary = " ".join(lines[-_MAX_ERROR_LINES:]) or "git clone failed"
    return summary[:_MAX_ERROR_CHARS]


@contextmanager
def _claim_destination(destination: str) -> Iterator[None]:
    """Reserve ``destination`` so two clones cannot write into it at once.

    :param destination: Absolute checkout path.
    :raises CloneError: When another clone into the path is still running.
    """
    with _inflight_lock:
        if destination in _inflight_destinations:
            raise CloneError(f"a clone into {destination} is already in progress", conflict=True)
        _inflight_destinations.add(destination)
    try:
        yield
    finally:
        with _inflight_lock:
            _inflight_destinations.discard(destination)


def clone_repository(
    *,
    url: str,
    parent_directory: str,
    directory_name: str,
    timeout_s: float = CLONE_REPOSITORY_BUDGET_S,
) -> ClonedRepository:
    """Clone ``url`` into ``parent_directory/directory_name`` on this host.

    A destination that already holds a checkout of the same remote is
    returned as-is, so retrying after a dropped request links the finished
    clone instead of failing. Any other existing path is a conflict.

    :param url: ``https://`` or ``git@`` clone URL, e.g.
        ``"git@github.com:org/repo.git"``.
    :param parent_directory: Absolute or ``~``-prefixed folder that will hold
        the checkout, e.g. ``"~/code"``. Missing folders are created.
    :param directory_name: Checkout folder name, e.g. ``"repo"``.
    :param timeout_s: Seconds the clone may run before it is killed.
    :returns: The checkout path and whether an existing checkout was reused.
    :raises CloneError: When the inputs are invalid, the destination is taken,
        or git fails.
    """
    if not _ACCEPTED_URL.fullmatch(url):
        raise CloneError("the repository URL must be an https:// or git@ URL")
    if not _DIRECTORY_NAME.fullmatch(directory_name) or directory_name in (".", ".."):
        raise CloneError(f"{directory_name!r} is not a usable folder name")
    expanded_parent = os.path.expanduser(parent_directory)
    if not os.path.isabs(expanded_parent):
        raise CloneError("the destination folder must be an absolute or ~ path")
    parent = os.path.abspath(expanded_parent)
    destination = os.path.join(parent, directory_name)

    with _claim_destination(destination):
        if os.path.lexists(destination):
            if _existing_checkout_matches(destination, url):
                _logger.info("Reusing existing checkout of %s at %s", url, destination)
                return ClonedRepository(path=destination, reused_existing=True)
            raise CloneError(
                f"{destination} already exists. Choose another folder, or open it "
                "with Open folder.",
                conflict=True,
            )
        if os.path.exists(parent) and not os.path.isdir(parent):
            raise CloneError(f"{parent} is not a folder", conflict=True)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise CloneError(f"could not create {parent}: {exc.strerror or exc}") from exc

        try:
            completed = _run_git(["clone", "--", url, destination], timeout_s=timeout_s)
        except FileNotFoundError as exc:
            raise CloneError("git is not installed on this host") from exc
        except subprocess.TimeoutExpired as exc:
            # A killed git leaves its partial checkout behind; the path was
            # free when the claim began, so everything there is ours.
            shutil.rmtree(destination, ignore_errors=True)
            raise CloneError(f"git clone timed out after {timeout_s:.0f}s") from exc
        if completed.returncode != 0:
            raise CloneError(_summarize_git_error(completed.stderr))

    _logger.info("Cloned %s into %s", url, destination)
    return ClonedRepository(path=destination, reused_existing=False)
