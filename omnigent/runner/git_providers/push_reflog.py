"""Find branches a shell call pushed by reading git's reflog afterwards.

``git push`` writes ``update by push`` to the reflog of each remote-tracking ref it
updates, so this sees pushes run through aliases, scripts, pipes, or ``--quiet``,
which the push's output alone cannot show. A push counts only when that entry is
recent and the ref points at the HEAD of a checkout the call ran in, so pushes
from other worktrees of the same repository are not picked up. Pushes to a URL
with no configured remote, and of a branch other than HEAD, are not seen.

Recency does not prove which session pushed: a session working in a checkout
at the same HEAD as a recent push from elsewhere (a person or another session)
also reports that push.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path

from omnigent.runtime.filesystem_registry import _anchored_git_env

# A call that only reports when it finished looks this far back for its push.
RECENT_PUSH_SECONDS = 300.0
_MAX_DIRECTORIES = 4
_GIT_TIMEOUT_SECONDS = 5.0
# Pushes already reported, so later calls in the same checkout do not look them up again.
_reported: OrderedDict[tuple[str, str, int], None] = OrderedDict()
_reported_lock = threading.Lock()
_MAX_REPORTED = 1024


def _git(directory: str, *argv: str) -> str | None:
    # The checkout's config is agent-writable, so keep it from running programs here.
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-pager",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "log.showSignature=false",
                *argv,
            ],
            cwd=directory,
            env=_anchored_git_env(),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def _claim(push: tuple[str, str, int]) -> bool:
    with _reported_lock:
        if push in _reported:
            return False
        _reported[push] = None
        while len(_reported) > _MAX_REPORTED:
            _reported.popitem(last=False)
        return True


def recent_pushes(directories: Iterable[str]) -> list[tuple[str, str]]:
    """Return ``(push URL, branch)`` for branches recently pushed from *directories*.

    Each push is returned once per process.
    """
    now = time.time()
    found: list[tuple[str, str]] = []
    checked: set[tuple[str, str]] = set()
    for directory in list(dict.fromkeys(directories))[:_MAX_DIRECTORIES]:
        if not Path(directory).is_dir():
            continue
        lines = (_git(directory, "rev-parse", "--git-common-dir", "HEAD") or "").splitlines()
        if len(lines) != 2:
            continue
        common_dir, head = str((Path(directory) / lines[0]).resolve()), lines[1]
        if (common_dir, head) in checked:
            continue
        checked.add((common_dir, head))
        refs = _git(
            directory,
            "for-each-ref",
            "--format=%(refname)",
            f"--points-at={head}",
            "refs/remotes/",
        )
        names = None
        for ref in (refs or "").split():
            if ref.endswith("/HEAD"):
                continue
            entry = _git(
                directory,
                "reflog",
                "show",
                "--no-show-signature",
                "-n",
                "1",
                "--date=unix",
                "--format=%gd %gs",
                ref,
                "--",
            )
            match = re.fullmatch(r".*@\{(\d+)\} update by push", (entry or "").strip())
            if match is None or not now - RECENT_PUSH_SECONDS <= int(match[1]) <= now + 2:
                continue
            if names is None:
                # Remote names may contain "/", so the longest matching prefix names the remote.
                names = sorted((_git(directory, "remote") or "").split(), key=len, reverse=True)
            remote = next(
                (name for name in names if ref.startswith(f"refs/remotes/{name}/")), None
            )
            if remote is None or not _claim((common_dir, ref, int(match[1]))):
                continue
            url = (_git(directory, "remote", "get-url", "--push", remote) or "").strip()
            if url:
                found.append((url, ref.removeprefix(f"refs/remotes/{remote}/")))
    return found
