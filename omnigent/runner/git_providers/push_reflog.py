"""Find branches a shell call pushed by reading git's reflog afterwards.

``git push`` writes ``update by push`` to the reflog of each remote-tracking ref it
updates, so this sees pushes run through aliases, scripts, pipes, or ``--quiet``,
which the push's output alone cannot show. A push counts only when that entry is
recent and the ref points at the HEAD of a checkout the call ran in, so pushes
from other worktrees of the same repository are not picked up. Pushes to a URL
with no configured remote, and of a branch other than HEAD, are not seen. The
branch is the tracking ref's name under ``refs/remotes/<remote>/``, so a remote
whose fetch refspec renames branches reports the renamed name.

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
from dataclasses import dataclass
from pathlib import Path

from omnigent.runtime.filesystem_registry import _anchored_git_env

# A call that only reports when it finished looks this far back for its push.
RECENT_PUSH_SECONDS = 300.0
_GIT_TIMEOUT_SECONDS = 5.0
# Pushes whose PRs were looked up, so later calls in the same checkout skip them.
_reported: OrderedDict[tuple[str, str, str, int], None] = OrderedDict()
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


@dataclass(frozen=True)
class RecentPush:
    """A branch pushed from a checkout.

    :ivar url: The remote's push URL.
    :ivar branch: The pushed branch.
    :ivar key: Identifies this push for :func:`mark_reported`.
    """

    url: str
    branch: str
    key: tuple[str, str, str, int]


def mark_reported(keys: Iterable[tuple[str, str, str, int]]) -> None:
    """Stop returning these pushes once their PRs were looked up."""
    with _reported_lock:
        for key in keys:
            _reported[key] = None
        while len(_reported) > _MAX_REPORTED:
            _reported.popitem(last=False)


def recent_pushes(directories: Iterable[str]) -> list[RecentPush]:
    """Return the branches recently pushed from *directories* and not yet reported."""
    now = time.time()
    found: list[RecentPush] = []
    checked: set[tuple[str, str]] = set()
    for directory in dict.fromkeys(directories):
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
            if remote is None:
                continue
            key = (common_dir, ref, head, int(match[1]))
            with _reported_lock:
                if key in _reported:
                    continue
            url = (_git(directory, "remote", "get-url", "--push", remote) or "").strip()
            if url:
                found.append(RecentPush(url, ref.removeprefix(f"refs/remotes/{remote}/"), key))
    return found
