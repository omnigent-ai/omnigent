"""Find the branches a shell tool call pushed by reading git's own state afterwards.

``git push`` records ``update by push`` in the reflog of each remote-tracking ref
it updates, whichever alias, wrapper, pipe, or redirection ran it. A branch counts
as pushed by the call when that entry falls inside the call's time window and the
ref now points at the HEAD of a checkout the call touched. The HEAD match keeps
other worktrees' pushes out, and fetches such as ``gh pr checkout`` write no push
entry. A push to a URL that matches no remote, or of a branch other than HEAD, is
not seen.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePath

from omnigent.runner.git_providers import ShellSegment
from omnigent.runtime.filesystem_registry import _anchored_git_env

_GIT_TIMEOUT_S = 5.0
# Reflog times are whole seconds.
WINDOW_SLACK_S = 2.0
# How far back a call without a known start time looks for its pushes.
UNTIMED_LOOKBACK_S = 300.0
_REMEMBERED_CHECKOUTS = 4
_HANDLED_PUSHES = 64
_PENDING_BRANCHES = 8
PENDING_TTL_S = 900.0
RECHECK_INTERVAL_S = 15.0
_SESSIONS = 256
_GIT_VALUE_OPTIONS = frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})
# ``(provider, host, repository, branch)``.
BranchKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class Checkout:
    """A git worktree and the commit its HEAD points at."""

    toplevel: str
    common_dir: str
    head: str


@dataclass(frozen=True)
class PushedBranch:
    """A branch a push updated.

    :ivar repository: The local repository's common git directory.
    :ivar url: The remote's push URL.
    :ivar branch: The remote branch name.
    :ivar sha: The pushed commit.
    """

    repository: str
    url: str
    branch: str
    sha: str


def _git(argv: list[str], cwd: str) -> tuple[int | None, str]:
    # The checkout's config is agent-writable; keep it from running programs here.
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
            cwd=cwd,
            env=_anchored_git_env(),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return completed.returncode, completed.stdout.decode("utf-8", errors="replace")


def checkout(directory: str) -> Checkout | None:
    """Return the checkout containing *directory*, or ``None`` when git cannot read one."""
    if not os.path.isdir(directory):
        return None
    code, output = _git(["rev-parse", "--show-toplevel", "--git-common-dir", "HEAD"], directory)
    lines = output.splitlines()
    if code != 0 or len(lines) != 3:
        return None
    toplevel, common_dir, head = lines
    return Checkout(toplevel, os.path.realpath(os.path.join(directory, common_dir)), head)


def _pushed_in_window(cwd: str, ref: str, since: float, until: float) -> bool:
    code, output = _git(
        ["reflog", "show", "-n", "1", "--date=unix", "--format=%gD %gs", ref, "--"], cwd
    )
    match = re.fullmatch(r".*@\{(\d+)\} update by push", output.strip())
    return (
        code == 0
        and match is not None
        and since - WINDOW_SLACK_S <= int(match[1]) <= until + WINDOW_SLACK_S
    )


def pushed_branches(
    checkouts: Iterable[Checkout], since: float, until: float
) -> list[PushedBranch]:
    """Return the branches pushed from *checkouts* between *since* and *until*."""
    repositories: dict[str, list[Checkout]] = {}
    for item in checkouts:
        repositories.setdefault(item.common_dir, []).append(item)
    found: list[PushedBranch] = []
    for common_dir, members in repositories.items():
        cwd = members[0].toplevel
        heads = sorted({member.head for member in members})
        code, output = _git(
            [
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                *(f"--points-at={head}" for head in heads),
                "refs/remotes/",
            ],
            cwd,
        )
        if code != 0:
            continue
        pushed = [
            (sha, ref)
            for sha, _, ref in (line.partition(" ") for line in output.splitlines())
            if ref and not ref.endswith("/HEAD") and _pushed_in_window(cwd, ref, since, until)
        ]
        if not pushed:
            continue
        code, output = _git(["remote"], cwd)
        # Remote names may contain ``/``, so the longest matching prefix names the remote.
        names = sorted(output.split(), key=len, reverse=True) if code == 0 else []
        for sha, ref in pushed:
            name = next((name for name in names if ref.startswith(f"refs/remotes/{name}/")), None)
            if name is None:
                continue
            code, url = _git(["remote", "get-url", "--push", name], cwd)
            if code == 0 and url.strip():
                branch = ref.removeprefix(f"refs/remotes/{name}/")
                found.append(PushedBranch(common_dir, url.strip(), branch, sha))
    return found


def _directory(current: str | None, argument: str) -> str | None:
    """Resolve a ``cd``-style argument, or return ``None`` when only the shell knows the result."""
    if argument == "-" or "$" in argument or "`" in argument:
        return None
    if argument.startswith("~"):
        argument = os.path.expanduser(argument)
    if os.path.isabs(argument):
        return os.path.normpath(argument)
    return os.path.normpath(os.path.join(current, argument)) if current else None


def _git_directory(current: str | None, tokens: Sequence[str]) -> str | None:
    """Return the directory a ``git`` invocation runs in after its ``-C`` options."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-C" and index + 1 < len(tokens):
            current = _directory(current, tokens[index + 1])
            index += 2
        elif token in _GIT_VALUE_OPTIONS:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    return current


def with_directories(
    segments: Sequence[ShellSegment], base: str | None
) -> tuple[list[ShellSegment], list[str]]:
    """Set each segment's ``cwd`` and return every directory the command runs git in.

    *base* is the absolute directory the command starts in, when known.
    """
    current = base
    directories: dict[str, None] = dict.fromkeys([base] if base else [])
    annotated: list[ShellSegment] = []
    for segment in segments:
        tokens = segment.invocation_tokens
        if tokens[0] in {"cd", "pushd"}:
            arguments = [token for token in tokens[1:] if token not in {"-L", "-P", "-e", "--"}]
            if arguments:
                current = _directory(current, arguments[0])
            else:
                current = _directory(current, "~") if tokens[0] == "cd" else None
        elif tokens[0] == "popd":
            current = None
        elif PurePath(tokens[0]).name == "git" and (target := _git_directory(current, tokens)):
            directories[target] = None
        if current:
            directories[current] = None
        annotated.append(replace(segment, cwd=current))
    return annotated, list(directories)


@dataclass
class PendingBranch:
    """A pushed branch that had no open PR yet, rechecked until it does or expires."""

    expires_at: float
    next_check: float


@dataclass
class _Session:
    cursor: float | None = None
    checkouts: list[str] = field(default_factory=list)
    handled: OrderedDict[tuple[str, ...], None] = field(default_factory=OrderedDict)
    pending: OrderedDict[BranchKey, PendingBranch] = field(default_factory=OrderedDict)


class SessionPushes:
    """Per-session observation state, shared by the tool calls a runner process observes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.pop(session_id, None) or _Session()
        self._sessions[session_id] = session
        while len(self._sessions) > _SESSIONS:
            self._sessions.popitem(last=False)
        return session

    def window(self, session_id: str, started_at: float | None, finished_at: float) -> float:
        """Return when the call's pushes could start, and move the session's cursor past it."""
        with self._lock:
            session = self._session(session_id)
            since = started_at
            if since is None:
                since = finished_at - UNTIMED_LOOKBACK_S
                if session.cursor is not None:
                    since = max(since, session.cursor)
            session.cursor = max(session.cursor or finished_at, finished_at)
            return since

    def remember(self, session_id: str, toplevels: Iterable[str]) -> list[str]:
        """Remember checkouts the session used and return those it used before."""
        with self._lock:
            session = self._session(session_id)
            previous = list(session.checkouts)
            for toplevel in toplevels:
                if toplevel in session.checkouts:
                    session.checkouts.remove(toplevel)
                session.checkouts.insert(0, toplevel)
            del session.checkouts[_REMEMBERED_CHECKOUTS:]
            return previous

    def claim(self, session_id: str, key: tuple[str, ...]) -> bool:
        """Return whether *key* is new to the session, marking it handled."""
        with self._lock:
            handled = self._session(session_id).handled
            if key in handled:
                return False
            handled[key] = None
            while len(handled) > _HANDLED_PUSHES:
                handled.popitem(last=False)
            return True

    def add_pending(self, session_id: str, key: BranchKey, now: float) -> None:
        """Recheck *key* later for a PR it does not have yet."""
        with self._lock:
            pending = self._session(session_id).pending
            pending.pop(key, None)
            pending[key] = PendingBranch(now + PENDING_TTL_S, now + RECHECK_INTERVAL_S)
            while len(pending) > _PENDING_BRANCHES:
                pending.popitem(last=False)

    def due(self, session_id: str, now: float, *, force: bool = False) -> list[BranchKey]:
        """Return the pending branches to check now, deferring their next check."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return []
            due: list[BranchKey] = []
            for key, entry in list(session.pending.items()):
                if entry.expires_at <= now:
                    del session.pending[key]
                elif force or entry.next_check <= now:
                    entry.next_check = now + RECHECK_INTERVAL_S
                    due.append(key)
            return due

    def resolved(self, session_id: str, key: BranchKey) -> None:
        """Stop rechecking *key*."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.pending.pop(key, None)
