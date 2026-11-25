"""Runs a fixed set of read-only Git commands for the workspace API.

Every call gets a clean environment and configuration overrides that keep Git
from writing to the repository, fetching, prompting or running configured
helpers, hooks or filters. Each RPC shares one deadline across all its
commands, waits for a bounded number of slots, caps its output, and kills and
reaps the child's whole process group however the call ends.

Arguments that come from requests are only ever full object names, revisions
checked by :func:`check_revision` and placed after ``--end-of-options``, or
paths placed after ``--`` or on stdin, with literal pathspecs.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import errno
import os
import re
import select
import signal
import subprocess
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

# Git children running at once for the whole host, and for one repository.
_HOST_CONCURRENCY = 4
_REPOSITORY_CONCURRENCY = 2
# Stderr kept for a failure's summary; the rest is drained and dropped.
_STDERR_BYTES = 64 * 1024
SMALL_OUTPUT_BYTES = 64 * 1024
LISTING_OUTPUT_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024
_EXIT_POLL_S = 0.02
# GIT_NO_LAZY_FETCH arrived in this release; older Git may fetch missing
# objects in a partial clone, so those repositories are refused.
_NO_LAZY_FETCH_VERSION = (2, 44)
_MAX_REVISION_BYTES = 1024
_GIT_FALSE = (b"false", b"no", b"off", b"0")

# Kept from the host's environment. Everything else, including every GIT_*
# variable, is dropped so nothing can redirect the repository or its config.
_INHERITED_VARIABLES = ("PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR")
_FIXED_VARIABLES = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # Empty: no transport may run, even if something asks for a fetch.
    "GIT_ALLOW_PROTOCOL": "",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
}
# No fsmonitor hook, index or untracked-cache writes, hooks, credential
# helpers, external diff, pager or transports.
_FIXED_CONFIGURATION = (
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "credential.helper=",
    "diff.external=",
    "diff.autoRefreshIndex=false",
    "protocol.allow=never",
    "fetch.negotiationAlgorithm=noop",
)

# Added after the subcommand on every call, so no caller can leave them out.
_SUBCOMMAND_OPTIONS = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-show-signature", "--no-color", "--no-ext-diff", "--no-textconv"),
}
# check-ignore refuses literal pathspecs; it gets raw "./"-prefixed stdin names with
# --no-index. Every other command keeps literal pathspecs.
_MAGIC_PATHSPEC_SUBCOMMANDS = frozenset({"check-ignore"})

# Tests add variables here, such as GIT_TRACE2_EVENT, to observe Git.
test_environment: dict[str, str] = {}


class GitProblem(enum.Enum):
    """Why a Git call gave no usable answer."""

    UNAVAILABLE = "Git isn't installed or couldn't start"
    TIMEOUT = "Git took too long"
    OUTPUT_TOO_LARGE = "Git's output was too large"
    MISSING_OBJECT = "An object this needs isn't in the repository"
    PARTIAL_CLONE_NEEDS_NEWER_GIT = "This partial clone needs Git 2.44 or newer"
    INVALID_REVISION = "That revision can't be passed to Git"
    UNSAFE_CONFIGURATION = "This repository's Git configuration can't be read safely"
    FAILED = "Git failed"


class GitError(Exception):
    """A Git call that failed, with a user-safe ``detail``.

    :param problem: Why it failed.
    :param detail: Git's last error line, or a summary; never configuration values.
    """

    def __init__(self, problem: GitProblem, detail: str = "") -> None:
        super().__init__(detail or problem.value)
        self.problem = problem
        self.detail = detail


class Deadline:
    """One monotonic deadline shared by every Git call of one RPC.

    :param seconds: Time from now the RPC may take.
    """

    def __init__(self, seconds: float) -> None:
        self._expires = time.monotonic() + seconds

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self._expires - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


def is_object_name(text: str) -> bool:
    """Whether ``text`` is a full SHA-1 or SHA-256 object name."""
    return len(text) in (40, 64) and all(c in "0123456789abcdefABCDEF" for c in text)


def check_revision(revision: str) -> str:
    """Return ``revision`` if Git may be asked about it, else raise INVALID_REVISION.

    It goes after ``--end-of-options``; a leading ``-`` is refused as well, so
    no Git version can read it as an option.
    """
    if (
        not revision
        or len(revision.encode()) > _MAX_REVISION_BYTES
        or revision.startswith("-")
        or any(c in revision for c in "\0\n\r")
    ):
        raise GitError(GitProblem.INVALID_REVISION)
    return revision


def parse_version(text: str) -> tuple[int, int] | None:
    """``"git version 2.54.0 (Apple Git-157)"`` → ``(2, 54)``."""
    match = re.match(r"git version (\d+)\.(\d+)", text.strip())
    return (int(match[1]), int(match[2])) if match else None


@dataclass(frozen=True)
class GitOutput:
    """One finished Git child.

    :param stdout: What it wrote, at most the call's cap.
    :param returncode: Its exit status; negative for a signal.
    :param stderr: At most 64 KiB of its error output.
    :param overflowed: The output passed the cap; only with ``allow_partial``.
    """

    stdout: bytes
    returncode: int
    stderr: str
    overflowed: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0


# Slots are per event loop, as asyncio primitives are bound to one.
_host_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_repository_slots: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()
# Cleanups in flight, held so a cancelled call can't drop its own.
_cleanups: set[asyncio.Task[None]] = set()


def _host_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _host_slots.get(loop)
    if slots is None:
        slots = _host_slots[loop] = asyncio.Semaphore(_HOST_CONCURRENCY)
    return slots


def _repository_semaphore(common_directory: Path) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    registry = _repository_slots.get(loop)
    if registry is None:
        registry = _repository_slots[loop] = weakref.WeakValueDictionary()
    key = os.path.realpath(common_directory)
    slots = registry.get(key)
    if slots is None:
        slots = asyncio.Semaphore(_REPOSITORY_CONCURRENCY)
        registry[key] = slots
    return slots


def trusted_search_path() -> str:
    """The host's ``PATH`` without relative entries.

    Children run in the workspace, where a ``.`` or empty entry would find a
    planted ``git`` or ``rg``.
    """
    entries = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(entry for entry in entries if os.path.isabs(entry))


def _environment(*, literal_pathspecs: bool = True) -> dict[str, str]:
    environment = {name: os.environ[name] for name in _INHERITED_VARIABLES if name in os.environ}
    environment["PATH"] = trusted_search_path()
    environment.update(_FIXED_VARIABLES)
    environment.update(test_environment)
    if not literal_pathspecs:
        environment.pop("GIT_LITERAL_PATHSPECS", None)
    return environment


def failure(output: GitOutput, command: str) -> GitError:
    """The error for a Git child that didn't succeed."""
    if output.returncode < 0:
        return GitError(
            GitProblem.FAILED, f"git {command} was stopped by signal {-output.returncode}"
        )
    stderr = output.stderr.strip()
    if any(
        marker in stderr
        for marker in (
            "missing blob object",
            "unable to read",
            "bad object",
            "could not fetch",
            "lazy fetching disabled",
        )
    ):
        return GitError(GitProblem.MISSING_OBJECT)
    summary = stderr.splitlines()[-1][:300] if stderr else ""
    return GitError(GitProblem.FAILED, summary or f"git {command} exited with {output.returncode}")


async def _read_capped(
    stream: asyncio.StreamReader, limit: int, sink: Callable[[bytes], None] | None = None
) -> tuple[bytes, bool]:
    """Read to the end, or until more than ``limit`` bytes arrived.

    :param sink: Takes each chunk as it arrives, instead of it being kept.
    """
    kept = bytearray()
    received = 0
    while chunk := await stream.read(_READ_CHUNK):
        overflowed = len(chunk) > limit - received
        if overflowed:
            chunk = chunk[: limit - received]
        received += len(chunk)
        if sink is None:
            kept += chunk
        else:
            sink(chunk)
        if overflowed:
            return bytes(kept), True
    return bytes(kept), False


async def _drain(stream: asyncio.StreamReader) -> bytes:
    """Read to the end, keeping at most 64 KiB."""
    kept = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        kept += chunk[: _STDERR_BYTES - len(kept)]
    return bytes(kept)


def _feed(stream: IO[bytes], data: bytes) -> None:
    # A child that exits early closes its stdin; that isn't an error here.
    with contextlib.suppress(OSError):
        stream.write(data)
    with contextlib.suppress(OSError):
        stream.close()


async def _readable(descriptor: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    loop.add_reader(descriptor, lambda: ready.done() or ready.set_result(None))
    try:
        await ready
    finally:
        loop.remove_reader(descriptor)


async def _pidfd_exit(pid: int) -> None:
    # pyrefly: ignore[missing-attribute]  # Linux only; offered when present
    descriptor = os.pidfd_open(pid)
    try:
        await _readable(descriptor)
    finally:
        os.close(descriptor)


async def _kqueue_exit(pid: int) -> None:
    # pyrefly: ignore[missing-attribute]  # macOS and BSD only; offered when present
    queue = select.kqueue()
    try:
        # pyrefly: ignore[missing-attribute]
        event = select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)
        queue.control([event], 0)
        await _readable(queue.fileno())
    finally:
        queue.close()


async def _polled_exit(pid: int) -> None:
    # pyrefly: ignore[missing-attribute]  # not on macOS; offered when present
    while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        await asyncio.sleep(_EXIT_POLL_S)


def _available_exit_watchers() -> list[Callable[[int], Awaitable[None]]]:
    """The ways this host can see a child exit without reaping it, best first."""
    watchers: list[Callable[[int], Awaitable[None]]] = []
    if hasattr(os, "pidfd_open"):
        watchers.append(_pidfd_exit)
    if hasattr(select, "kqueue"):
        watchers.append(_kqueue_exit)
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        watchers.append(_polled_exit)
    return watchers


_EXIT_WATCHERS = _available_exit_watchers()


def runs_on_this_host() -> bool:
    """Whether this host can own Git's processes: POSIX process groups and an exit watcher.

    Native Windows has neither, so callers that can do without Git skip it there.
    """
    return hasattr(os, "killpg") and bool(_EXIT_WATCHERS)


async def _exit_of(pid: int) -> None:
    """Return once ``pid`` has exited, without reaping it.

    The leader stays unreaped, so its process id and group id can't be reused
    until the group has been killed. A watcher that can't start, such as a
    pidfd refused by a sandbox, gives way to the next.

    :raises OSError: When no watcher works on this host.
    """
    failure: OSError = OSError(errno.ENOSYS, "No way to watch a child's exit")
    for watch in _EXIT_WATCHERS:
        try:
            await watch(pid)
            return
        except ProcessLookupError:
            return  # kqueue's answer for a child that already exited, still unreaped
        except OSError as error:
            failure = error
    raise failure


class OwnedProcess:
    """A child leading its own session, whose whole group this runner owns.

    Its exit is observed without reaping it, so the group can still be killed
    safely after the leader exits; :meth:`close` kills the group, then reaps.

    :param process: The started child.
    :param stdout: Its output, as a stream.
    :param stderr: Its error output, when piped.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader | None,
        transports: Sequence[asyncio.BaseTransport],
    ) -> None:
        self.pid = process.pid
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self._process = process
        self._transports = transports
        self._exit = asyncio.ensure_future(_exit_of(process.pid))
        self._closing: asyncio.Task[None] | None = None

    async def exited(self) -> None:
        """Return once the leader has exited; it stays unreaped until :meth:`close`."""
        await asyncio.shield(self._exit)

    def kill(self) -> None:
        """Kill the whole group; the leader is unreaped, so the group id is still ours."""
        if self.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)

    async def close(self) -> int:
        """Kill what is left of the group, then reap the leader and close its pipes.

        Runs once, in a task this module holds, so a repeated cancellation of the
        caller can't skip the reap.

        :returns: The leader's exit status, negative for a signal.
        """
        if self._closing is None:
            self.kill()
            self._closing = asyncio.ensure_future(self._reap())
            _cleanups.add(self._closing)
            self._closing.add_done_callback(_cleanups.discard)
        await asyncio.shield(self._closing)
        assert self.returncode is not None
        return self.returncode

    async def _reap(self) -> None:
        with contextlib.suppress(Exception):
            await self._exit
        for transport in self._transports:
            transport.close()
        self.returncode = await asyncio.to_thread(self._process.wait)


async def start_owned_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin: bytes | None = None,
    pipe_stderr: bool = True,
    limit: int = 2**16,
) -> OwnedProcess:
    """Start ``argv`` in its own session, with its output as streams.

    :param stdin: Written to the child's input from a thread; otherwise it reads nothing.
    :param limit: The output stream's line-length limit, for ``readline``.
    :raises OSError: When the program can't start.
    """
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if pipe_stderr else subprocess.DEVNULL,
        start_new_session=True,
    )
    loop = asyncio.get_running_loop()
    transports: list[asyncio.BaseTransport] = []
    streams: list[asyncio.StreamReader] = []
    try:
        for pipe in (process.stdout, process.stderr):
            if pipe is None:
                continue
            reader = asyncio.StreamReader(limit=limit)
            transport, _ = await loop.connect_read_pipe(
                lambda reader=reader: asyncio.StreamReaderProtocol(reader), pipe
            )
            transports.append(transport)
            streams.append(reader)
    except BaseException:
        # Still unreaped, so the group id is still this child's.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        for transport in transports:
            transport.close()
        process.wait()
        raise
    owned = OwnedProcess(process, streams[0], streams[1] if pipe_stderr else None, transports)
    if stdin is not None:
        assert process.stdin is not None
        feeding = asyncio.ensure_future(asyncio.to_thread(_feed, process.stdin, stdin))
        _cleanups.add(feeding)
        feeding.add_done_callback(_cleanups.discard)
    return owned


async def _run(
    working_directory: Path,
    arguments: Sequence[str],
    *,
    deadline: Deadline,
    slots: Sequence[asyncio.Semaphore],
    stdin: bytes | None = None,
    stdout_bytes: int = SMALL_OUTPUT_BYTES,
    allow_partial: bool = False,
    stdout_sink: Callable[[bytes], None] | None = None,
    literal_pathspecs: bool = True,
) -> GitOutput:
    """Start one Git child once ``slots`` allow it, and clean up its process group."""
    if deadline.expired():
        raise GitError(GitProblem.TIMEOUT)
    async with contextlib.AsyncExitStack() as held:
        try:
            async with asyncio.timeout(deadline.remaining()):
                for semaphore in slots:
                    await semaphore.acquire()
                    held.callback(semaphore.release)
        except TimeoutError:
            raise GitError(GitProblem.TIMEOUT) from None
        argv = ["git"]
        for setting in _FIXED_CONFIGURATION:
            argv += ["-c", setting]
        argv += arguments
        try:
            process = await start_owned_process(
                argv,
                cwd=working_directory,
                env=_environment(literal_pathspecs=literal_pathspecs),
                stdin=stdin,
            )
        except OSError:
            raise GitError(GitProblem.UNAVAILABLE) from None
        assert process.stderr is not None
        draining = asyncio.ensure_future(_drain(process.stderr))
        try:
            async with asyncio.timeout(deadline.remaining()):
                stdout, overflowed = await _read_capped(process.stdout, stdout_bytes, stdout_sink)
                if overflowed and not allow_partial:
                    raise GitError(GitProblem.OUTPUT_TOO_LARGE)
                if overflowed:
                    process.kill()
                await process.exited()
                stderr = await draining
        except TimeoutError:
            raise GitError(GitProblem.TIMEOUT) from None
        except OSError:
            raise GitError(GitProblem.UNAVAILABLE) from None  # Git's exit couldn't be watched
        finally:
            draining.cancel()
            # Descendants that outlive Git go with its group here, then Git is reaped.
            returncode = await process.close()
        return GitOutput(
            stdout=stdout,
            returncode=returncode,
            stderr=stderr.decode("utf-8", "replace"),
            overflowed=overflowed,
        )


async def _git_version(deadline: Deadline) -> tuple[int, int] | None:
    output = await _run(Path("/"), ["version"], deadline=deadline, slots=[_host_semaphore()])
    return parse_version(output.stdout.decode("utf-8", "replace"))


@dataclass(frozen=True)
class _BehaviorConfiguration:
    filter_drivers: frozenset[str]
    partial_clone: bool


def parse_behavior_configuration(records: bytes) -> _BehaviorConfiguration:
    """Read filter drivers and partial-clone settings from ``config --null`` records.

    Values stay opaque. A key that isn't UTF-8, or a driver name a ``-c``
    override can't carry, refuses the repository: it could hide a filter.
    """
    drivers: set[str] = set()
    partial_clone = False
    for record in records.split(b"\0"):
        if not record:
            continue
        raw_key, has_value, value = record.partition(b"\n")
        try:
            key = raw_key.decode()
        except UnicodeDecodeError:
            raise GitError(GitProblem.UNSAFE_CONFIGURATION) from None
        section, dot, rest = key.partition(".")
        if not dot:
            continue
        subsection, _, variable = rest.rpartition(".")
        section, variable = section.lower(), variable.lower()
        if (
            section == "filter"
            and subsection
            and variable in ("clean", "process", "smudge", "required")
        ):
            # `-c filter.<name>.clean=` splits at the first `=`.
            if "=" in subsection or "\n" in subsection:
                raise GitError(GitProblem.UNSAFE_CONFIGURATION)
            drivers.add(subsection)
        elif section == "extensions" and not subsection and variable == "partialclone":
            partial_clone = True
        elif section == "remote" and variable == "promisor":
            # A bare key is true, an empty value false; anything else Git doesn't read
            # as false counts, so an odd spelling can't skip the version gate.
            is_false = bool(has_value) and (not value or value.lower() in _GIT_FALSE)
            partial_clone = partial_clone or not is_false
    return _BehaviorConfiguration(frozenset(drivers), partial_clone)


def _filter_overrides(drivers: frozenset[str]) -> list[str]:
    overrides: list[str] = []
    for driver in sorted(drivers):
        for key in ("clean", "process", "smudge"):
            overrides += ["-c", f"filter.{driver}.{key}="]
        overrides += ["-c", f"filter.{driver}.required=false"]
    return overrides


@dataclass
class GitRepository:
    """A repository located from a workspace directory.

    :param working_directory: Where commands run; ``-- .`` scopes them to it.
    :param git_directory: Its absolute ``.git`` directory.
    :param common_directory: Shared by its worktrees; keys the repository's slots.
    :param top_level: Its working tree's root.
    :param prefix: The working directory below ``top_level``, ``/``-separated,
        ending in ``/`` unless empty.
    """

    working_directory: Path
    git_directory: Path
    common_directory: Path
    top_level: Path
    prefix: str
    filter_drivers: frozenset[str] = frozenset()
    partial_clone: bool = False
    _slots: asyncio.Semaphore | None = field(default=None, repr=False)

    @classmethod
    async def discover(cls, working_directory: Path, deadline: Deadline) -> GitRepository | None:
        """Locate the repository containing ``working_directory``, or ``None`` outside one.

        Also reads the configuration later calls neutralize, and refuses a
        partial clone that Git could fetch into.
        """
        output = await _run(
            working_directory,
            [
                "rev-parse",
                "--absolute-git-dir",
                "--git-common-dir",
                "--show-toplevel",
                "--show-prefix",
            ],
            deadline=deadline,
            slots=[_host_semaphore()],
        )
        if not output.success:
            # Git won't treat the directory as a working tree, so read it without Git.
            if any(
                refusal in output.stderr
                for refusal in (
                    "not a git repository",
                    "detected dubious ownership",
                    "must be run in a work tree",
                )
            ):
                return None
            raise failure(output, "rev-parse")
        try:
            lines = output.stdout.decode().split("\n")
        except UnicodeDecodeError:
            raise GitError(GitProblem.FAILED, "The repository path isn't valid UTF-8") from None
        lines += [""] * 4
        git_directory, common, top_level, prefix = lines[:4]
        if not top_level:
            # A bare repository or the inside of `.git`: there is no working tree.
            return None
        common_directory = Path(common)
        if not common_directory.is_absolute():
            common_directory = working_directory / common_directory
        repository = cls(
            working_directory=working_directory,
            git_directory=Path(git_directory),
            common_directory=common_directory,
            top_level=Path(top_level),
            prefix=prefix,
        )
        repository._slots = _repository_semaphore(common_directory)
        configuration = await repository.run_unchecked(
            [
                "config",
                "--null",
                "--get-regexp",
                r"^(filter\..*\.(clean|process|smudge|required)|extensions\.partialclone|remote\..*\.promisor)$",
            ],
            deadline=deadline,
            stdout_bytes=256 * 1024,
        )
        # Exit 1 means no matching keys.
        if not configuration.success and configuration.returncode != 1:
            raise failure(configuration, "config")
        behavior = parse_behavior_configuration(configuration.stdout)
        repository.filter_drivers = behavior.filter_drivers
        repository.partial_clone = behavior.partial_clone
        if behavior.partial_clone:
            version = await _git_version(deadline)
            if version is None or version < _NO_LAZY_FETCH_VERSION:
                raise GitError(GitProblem.PARTIAL_CLONE_NEEDS_NEWER_GIT)
        return repository

    async def run(
        self,
        arguments: Sequence[str],
        *,
        deadline: Deadline,
        stdin: bytes | None = None,
        stdout_bytes: int = SMALL_OUTPUT_BYTES,
    ) -> bytes:
        """Run a command, failing unless Git exits successfully."""
        output = await self.run_unchecked(
            arguments, deadline=deadline, stdin=stdin, stdout_bytes=stdout_bytes
        )
        if not output.success:
            raise failure(output, arguments[0])
        return output.stdout

    async def run_unchecked(
        self,
        arguments: Sequence[str],
        *,
        deadline: Deadline,
        stdin: bytes | None = None,
        stdout_bytes: int = SMALL_OUTPUT_BYTES,
        allow_partial: bool = False,
        stdout_sink: Callable[[bytes], None] | None = None,
    ) -> GitOutput:
        """Run a command on this repository and return its output whatever its exit status.

        :param allow_partial: Keep the output up to the cap, with ``overflowed``
            set, instead of failing with OUTPUT_TOO_LARGE.
        :param stdout_sink: Takes the output in chunks as it arrives, up to the cap,
            so a large listing is never held whole; ``stdout`` is then empty.
        """
        assert self._slots is not None
        subcommand, *rest = arguments
        command = [subcommand, *_SUBCOMMAND_OPTIONS.get(subcommand, ()), *rest]
        repository = [
            *_filter_overrides(self.filter_drivers),
            f"--git-dir={self.git_directory}",
            f"--work-tree={self.top_level}",
            "--no-optional-locks",
        ]
        # The repository's slot first, then the host's, always in that order.
        return await _run(
            self.working_directory,
            [*repository, *command],
            deadline=deadline,
            slots=[self._slots, _host_semaphore()],
            stdin=stdin,
            stdout_bytes=stdout_bytes,
            allow_partial=allow_partial,
            stdout_sink=stdout_sink,
            literal_pathspecs=subcommand not in _MAGIC_PATHSPEC_SUBCOMMANDS,
        )
