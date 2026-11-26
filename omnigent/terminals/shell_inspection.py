"""Whether a terminal's shell is idle, from one process-table snapshot of its tmux pane.

Closing a shell asks "Close shell?" only when something besides the login shell runs in it. The
runner reads its own pane's processes and keeps only an answer: idle, busy with a named job, or
unknown. Rows never leave this module, so no other process, pid or command line is reported,
logged or raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sys
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from omnigent.host.workspace_git.runner import trusted_search_path

# The columns ``ps`` prints, fixed: never arguments, environment or working directory.
PS_COLUMNS = "pid=,ppid=,pgid=,tpgid=,stat=,comm="
# A command name the dialog shows is at most this long.
MAX_COMMAND_NAME_LENGTH = 32
# One inspection's budget for both subprocesses; past it the answer is unknown.
INSPECTION_TIMEOUT_SECONDS = 0.45
# A fresh answer is reused this long, so several close paths firing at once share one read.
CACHE_SECONDS = 0.25
# Inspections running at once in one process, the host git runner's limit.
MAX_CONCURRENT_INSPECTIONS = 4
# Largest output either subprocess may print before the answer is unknown.
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024

# Interactive shells a terminal may run. Any other program, such as an editor or a REPL,
# answers unknown, since only a shell's lack of children says nothing else is running.
ELIGIBLE_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "ksh", "mksh", "fish", "tcsh", "csh"})
# Linux keeps this many characters of an executable's name in ``comm``.
_LINUX_COMM_LENGTH = 15

_ANSI_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


class ShellState(Enum):
    """What a terminal's shell is doing."""

    IDLE = "idle"
    BUSY = "busy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ShellInspection:
    """One terminal's answer: no pids and no other processes, only what the close dialog needs.

    :param state: Idle, busy, or unknown.
    :param foreground: The foreground job's command name when busy and known, e.g. ``"vim"``.
    :param background_jobs: How many other jobs run, or ``None`` where jobs can't be told apart,
        as in a sandboxed pane with no job control.
    """

    state: ShellState
    foreground: str | None = None
    background_jobs: int | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        """The wire form, e.g. ``{"state": "busy", "foreground": "vim", "background_jobs": 0}``."""
        return {
            "state": self.state.value,
            "foreground": self.foreground,
            "background_jobs": self.background_jobs,
        }

    @classmethod
    def from_untrusted(cls, payload: object) -> ShellInspection:
        """Rebuild an answer from another process's JSON, keeping only the expected fields.

        The state must be one of the three, the name is cleaned again, and the count must be a
        whole number from 0 up; anything else answers unknown.

        :param payload: Parsed JSON, e.g. ``{"state": "idle"}``.
        :returns: The rebuilt inspection.
        """
        if not isinstance(payload, dict):
            return UNKNOWN
        try:
            state = ShellState(payload.get("state"))
        except ValueError:
            return UNKNOWN
        if state is not ShellState.BUSY:
            return cls(state)
        name = payload.get("foreground")
        count = payload.get("background_jobs")
        valid_count = type(count) is int and 0 <= count <= 1_000_000
        return cls(
            state,
            sanitize_command_name(name) if isinstance(name, str) else None,
            count if valid_count else None,
        )


UNKNOWN = ShellInspection(ShellState.UNKNOWN)

# The one body every missing, foreign or stopped terminal answers with.
TERMINAL_SHELL_NOT_FOUND = {"error": {"code": "not_found", "message": "Terminal not found"}}


@dataclass(frozen=True)
class _Row:
    pid: int
    ppid: int
    pgid: int
    tpgid: int
    stat: str
    comm: str


def sanitize_command_name(comm: str) -> str | None:
    """The name to show for a process's ``comm``: untrusted text the process chose itself.

    macOS prints the executable's full path, so only its basename is kept, without a login
    shell's leading ``-``. ANSI sequences and control, format and bidi characters are removed,
    and the result is capped at :data:`MAX_COMMAND_NAME_LENGTH` characters.

    :param comm: The ``comm`` column, e.g. ``"/usr/bin/vim"`` or ``"-zsh"``.
    :returns: The name, e.g. ``"vim"``, or ``None`` when nothing printable is left.
    """
    text = _ANSI_SEQUENCE.sub("", comm)
    text = "".join(
        char for char in text if unicodedata.category(char)[0] not in ("C", "Z") or char == " "
    )
    name = text.strip().rsplit("/", 1)[-1].lstrip("-").strip()
    return name[:MAX_COMMAND_NAME_LENGTH] or None


def _parse(snapshot: str) -> list[_Row] | None:
    """The snapshot's rows, or ``None`` when any line isn't one.

    A process with an empty ``comm`` is kept, unnamed. Any other line that doesn't parse, such
    as the rest of a ``comm`` with a newline in it, could hide a process, so nothing is trusted.
    """
    rows: list[_Row] = []
    for line in snapshot.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 5)
        if len(fields) < 5:
            return None
        try:
            pid, ppid, pgid, tpgid = (int(value) for value in fields[:4])
        except ValueError:
            return None
        rows.append(_Row(pid, ppid, pgid, tpgid, fields[4], fields[5] if len(fields) == 6 else ""))
    return rows


def process_executable(pid: int) -> str | None:
    """The file ``pid`` runs, from the kernel rather than the name the process gave itself.

    :param pid: A process this runner may inspect, e.g. a pane's shell.
    :returns: The executable's resolved path, e.g. ``"/usr/bin/bash"``, or ``None`` when the
        platform can't tell or the process is gone.
    """
    if sys.platform.startswith("linux"):
        try:
            path = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
        # An executable replaced on disk since the process started.
        return path.removesuffix(" (deleted)")
    if sys.platform == "darwin":
        return _darwin_process_path(pid)
    return None


def _darwin_process_path(pid: int) -> str | None:
    import ctypes
    import ctypes.util

    library = ctypes.util.find_library("proc")
    if library is None:
        return None
    try:
        libproc = ctypes.CDLL(library)
    except OSError:
        return None
    buffer = ctypes.create_string_buffer(4096)
    length = libproc.proc_pidpath(ctypes.c_int(pid), buffer, ctypes.c_uint32(len(buffer)))
    if length <= 0:
        return None
    return buffer.value.decode(errors="replace")


# A file, by the device and inode it lives at: a copy or a rename can't take its place.
FileIdentity = tuple[int, int]


def _file_identity(path: str) -> FileIdentity | None:
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino)


def process_file_identity(pid: int) -> FileIdentity | None:
    """The file ``pid`` runs, by device and inode, from the kernel.

    On Linux this follows ``/proc/<pid>/exe``, which reaches the running file even from outside
    a sandbox's mount namespace.

    :param pid: A process this runner may inspect, e.g. a pane's shell.
    :returns: ``(st_dev, st_ino)``, or ``None`` when the platform can't tell.
    """
    if sys.platform.startswith("linux"):
        return _file_identity(f"/proc/{pid}/exe")
    path = process_executable(pid)
    return None if path is None else _file_identity(path)


def configured_shell_identity(shell_command: str, path: str) -> FileIdentity | None:
    """The file the terminal's command starts, found as its launch finds it, by identity.

    :param shell_command: The terminal's configured command, e.g. ``"bash"`` or ``"/bin/zsh"``.
    :param path: The search path, e.g. :func:`trusted_search_path`.
    :returns: ``(st_dev, st_ino)``, following symlinks, or ``None`` when it isn't found.
    """
    found = shutil.which(shell_command, path=path)
    return None if found is None else _file_identity(found)


def _shell_name(command: str) -> str | None:
    return sanitize_command_name(command.rsplit("/", 1)[-1])


# One launcher between the pane and the shell: the names its ``comm`` may show.
LauncherStep = frozenset[str]


def _launcher_python_names() -> LauncherStep:
    """The names the sandbox's Python launcher, ``sys.executable``, shows in ``comm``."""
    names: set[str] = set()
    for path in (sys.executable, os.path.realpath(sys.executable)):
        name = sanitize_command_name(os.path.basename(path))
        if name:
            names.update({name, name[:_LINUX_COMM_LENGTH]})
    return frozenset(names)


def sandbox_launchers(backend_type: str | None) -> tuple[LauncherStep, ...]:
    """The processes the framework starts between a terminal's pane and its shell, in order.

    Unsandboxed, the pane's process is the shell. Under bubblewrap it is ``bwrap``, then its
    namespace init ``bwrap``, then the Python launcher. Under another backend the pane's
    process is the Python launcher, since ``sandbox-exec`` execs into it.

    :param backend_type: The terminal's active sandbox backend, e.g. ``"linux_bwrap"``, or
        ``None`` when it isn't sandboxed.
    :returns: One set of accepted names per launcher, outermost first.
    """
    if backend_type is None:
        return ()
    python = _launcher_python_names()
    if backend_type == "linux_bwrap":
        return (frozenset({"bwrap"}), frozenset({"bwrap"}), python)
    return (python,)


def inspect_shell(
    snapshot: str,
    *,
    tmux_server_pid: int,
    pane_pid: int,
    shell_command: str,
    launchers: Sequence[LauncherStep] = (),
    shell_identity: FileIdentity | None = None,
    identity_of: Callable[[int], FileIdentity | None] = process_file_identity,
) -> ShellInspection:
    """Decide whether the pane's login shell is idle, from one ``ps`` snapshot.

    The pane's process must be a child of its own tmux server in this same snapshot, else its
    pid may have been reused and the answer is unknown. The terminal's command must be an
    :data:`ELIGIBLE_SHELLS` shell, and the pane must hold exactly the framework's
    ``launchers`` and then that shell, running the very file the command resolves to. Any
    other layout answers unknown, such as a program that replaced the shell, even one that
    renamed itself ``bash`` or a copy of another program named bash. So does any other live
    process on the pane's tmux server, in another pane or window, since closing the terminal
    ends the whole server. The shell is idle when nothing runs under it and nothing else runs
    anywhere under the pane. A sandbox's namespace init adopts work its shell left running,
    and closing the pane ends that too.

    Idle means no other process, not that the shell waits at its prompt: a loop of shell
    builtins, such as ``while :; do :; done``, starts no process and reads idle.

    :param snapshot: ``ps -e -o`` :data:`PS_COLUMNS` output.
    :param tmux_server_pid: The pane's tmux server, from tmux's ``#{pid}``.
    :param pane_pid: The pane's process, from tmux's ``#{pane_pid}``.
    :param shell_command: The terminal's configured command, e.g. ``"zsh"``.
    :param launchers: The framework's launchers between the pane and the shell, from
        :func:`sandbox_launchers`.
    :param shell_identity: The file the configured shell resolves to, from
        :func:`configured_shell_identity`; ``None`` answers unknown.
    :param identity_of: The file a pid runs, e.g. :func:`process_file_identity`. The shell's
        must be ``shell_identity``, since a process can give itself any ``comm`` and a copy of
        another program can carry a shell's name.
    :returns: The inspection; only names and counts, never a row.
    """
    rows = _parse(snapshot)
    if rows is None:
        return UNKNOWN
    by_pid = {row.pid: row for row in rows}
    # A pid listed twice means a line was forged, e.g. through a newline in a ``comm``.
    if len(by_pid) != len(rows):
        return UNKNOWN
    pane = by_pid.get(pane_pid)
    if pane is None or pane.ppid != tmux_server_pid or tmux_server_pid not in by_pid:
        return UNKNOWN
    children: dict[int, list[_Row]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    # Closing the terminal ends its whole tmux server, including work in other windows.
    # tmux can retain an exited window's zombie, but a zombie leader may still have live
    # threads (ps's `l` flag). Only unthreaded zombies are safe to ignore.
    if any(
        row.pid != pane.pid and (not row.stat.startswith("Z") or "l" in row.stat)
        for row in children.get(tmux_server_pid, [])
    ):
        return UNKNOWN

    wanted = _shell_name(shell_command)
    if wanted is None or wanted not in ELIGIBLE_SHELLS:
        return UNKNOWN
    shell = _shell_after_launchers(pane, launchers, wanted, children)
    if shell is None or shell_identity is None or identity_of(shell.pid) != shell_identity:
        return UNKNOWN

    jobs = _descendants(shell, children)
    chain: set[int] = set()
    row = shell
    while row.pid != pane.pid:
        row = by_pid[row.ppid]
        chain.add(row.pid)
    job_pids = {job.pid for job in jobs}
    strays = [
        row
        for row in _descendants(pane, children)
        if row.pid not in chain and row.pid != shell.pid and row.pid not in job_pids
    ]
    answer = _jobs_answer(shell, jobs, children)
    if not strays:
        return answer
    # Work outside the shell's own tree has no job control to count it by.
    if answer.state is ShellState.IDLE:
        first = min(strays, key=lambda row: row.pid)
        return ShellInspection(ShellState.BUSY, sanitize_command_name(first.comm), None)
    return ShellInspection(ShellState.BUSY, answer.foreground, None)


def _shell_after_launchers(
    pane: _Row,
    launchers: Sequence[LauncherStep],
    shell_name: str,
    children: dict[int, list[_Row]],
) -> _Row | None:
    """The shell at the end of exactly ``launchers`` from the pane, or ``None``.

    The pane's process is the first launcher, or the shell when there are none. Each next
    step must be the one child with an accepted name. A launcher's other children are work
    the shell left running, which keeps the pane busy rather than hiding the shell.
    """
    steps = [*launchers, frozenset({shell_name})]
    if sanitize_command_name(pane.comm) not in steps[0]:
        return None
    row = pane
    for step in steps[1:]:
        named = [
            child
            for child in children.get(row.pid, [])
            if sanitize_command_name(child.comm) in step
        ]
        if len(named) != 1:
            return None
        row = named[0]
    return row


def _descendants(root: _Row, children: dict[int, list[_Row]]) -> list[_Row]:
    """Every process below ``root``, each once even if forged rows make a cycle."""
    found: list[_Row] = []
    seen = {root.pid}
    stack = list(children.get(root.pid, []))
    while stack:
        row = stack.pop()
        if row.pid in seen:
            continue
        seen.add(row.pid)
        found.append(row)
        stack.extend(children.get(row.pid, []))
    return found


def _jobs_answer(
    shell: _Row, jobs: list[_Row], children: dict[int, list[_Row]]
) -> ShellInspection:
    """The answer from the shell's own descendants, ``jobs``."""
    if not jobs:
        return ShellInspection(ShellState.IDLE)

    foreground_group = shell.tpgid
    if foreground_group <= 0:
        # No job control, as under a sandbox's new session: name the shell's first child.
        first = min(children[shell.pid], key=lambda row: row.pid)
        return ShellInspection(ShellState.BUSY, sanitize_command_name(first.comm), None)
    groups = {row.pgid for row in jobs}
    foreground_rows = [row for row in jobs if row.pgid == foreground_group]
    foreground = None
    if foreground_rows:
        leader = next((row for row in foreground_rows if row.pid == foreground_group), None)
        foreground = sanitize_command_name((leader or foreground_rows[0]).comm)
    background = len(groups - {foreground_group})
    return ShellInspection(ShellState.BUSY, foreground, background)


class _CommandFailed(Exception):
    """A subprocess failed; it carries no output so nothing it printed can leak."""


async def _run(argv: Sequence[str], path: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": path, "LC_ALL": "C"},
    )
    stdout = process.stdout
    assert stdout is not None
    output = bytearray()
    try:
        # Read as it arrives, so a flood stops at the cap instead of filling memory first.
        while chunk := await stdout.read(_READ_CHUNK_BYTES):
            output += chunk
            if len(output) > _MAX_OUTPUT_BYTES:
                raise _CommandFailed
        await process.wait()
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if process.returncode != 0:
        raise _CommandFailed
    return output.decode("utf-8", errors="replace")


async def read_pane_shell(
    tmux_command: Sequence[str],
    target: str,
    shell_command: str,
    launchers: Sequence[LauncherStep] = (),
) -> ShellInspection:
    """Inspect one tmux pane's shell: tmux's server and pane pids, then one ``ps`` snapshot.

    Both run with fixed arguments, no shell and the trusted ``PATH``. Any failure, odd output
    or timeout answers unknown, and nothing either command printed is kept.

    :param tmux_command: The terminal's own tmux argv prefix, e.g.
        ``["/usr/bin/tmux", "-S", "/tmp/omnigent-terminal-x/tmux.sock", "-f", "/dev/null"]``.
    :param target: The pane's tmux target, e.g. ``"main"``.
    :param shell_command: The terminal's configured command, e.g. ``"zsh"``.
    :param launchers: The framework's launchers between the pane and the shell, from
        :func:`sandbox_launchers`.
    :returns: The inspection.
    """
    path = trusted_search_path()
    ps = shutil.which("ps", path=path)
    if ps is None:
        return UNKNOWN

    async def read() -> ShellInspection:
        pids = await _run(
            [*tmux_command, "display-message", "-p", "-t", target, "#{pid} #{pane_pid}"], path
        )
        fields = pids.split()
        if len(fields) != 2 or not all(field.isascii() and field.isdigit() for field in fields):
            return UNKNOWN
        snapshot = await _run([ps, "-e", "-o", PS_COLUMNS], path)
        return inspect_shell(
            snapshot,
            tmux_server_pid=int(fields[0]),
            pane_pid=int(fields[1]),
            shell_command=shell_command,
            launchers=launchers,
            shell_identity=configured_shell_identity(shell_command, path),
            identity_of=process_file_identity,
        )

    try:
        return await asyncio.wait_for(read(), INSPECTION_TIMEOUT_SECONDS)
    except (TimeoutError, OSError, _CommandFailed):
        return UNKNOWN


class ShellInspector:
    """Runs inspections one per terminal at a time, under a shared limit, with a short cache.

    A request for a terminal already being read waits for that read; a request within
    :data:`CACHE_SECONDS` of the last answer reuses it. Idle is the exception: it lets a close
    skip asking, so it only comes from a read that began at or after the request, never from one
    that may predate input typed since. Each read, a liveness check included,
    gets :data:`INSPECTION_TIMEOUT_SECONDS` in all, and is cancelled past it. Keys must name one
    terminal, e.g. its tmux socket path; callers check that the caller may see that terminal
    first. A read answers ``None`` for a terminal that's gone.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = MAX_CONCURRENT_INSPECTIONS,
        cache_seconds: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = INSPECTION_TIMEOUT_SECONDS,
    ) -> None:
        self._slots = asyncio.Semaphore(max_concurrent)
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        # Per terminal: when the last answer arrived, when its read began, and the answer.
        self._answers: dict[str, tuple[float, float, ShellInspection | None]] = {}
        # Per terminal: the read under way, with when it began.
        self._in_flight: dict[str, tuple[float, asyncio.Task[ShellInspection | None]]] = {}

    async def inspect(
        self, key: str, read: Callable[[], Awaitable[ShellInspection | None]]
    ) -> ShellInspection | None:
        """The answer for the terminal ``key``, reading it with ``read`` only when needed.

        :param key: One terminal's identity, e.g. its tmux socket path.
        :param read: Checks the terminal is alive and reads it, e.g. ``read_pane_shell`` bound
            to its pane; ``None`` when it's gone.
        :returns: The inspection, or ``None`` for a gone terminal; unknown when ``read`` raises
            or runs past the budget.
        """
        asked = self._clock()
        self._answers = {
            name: answer
            for name, answer in self._answers.items()
            if asked - answer[0] < self._cache_seconds
        }
        cached = self._answers.get(key)
        if cached is not None and _answers_for(cached[1], cached[2], asked=asked):
            return cached[2]
        began, task = self._in_flight.get(key) or self._start(key, read)
        # Shielded so one cancelled caller doesn't cancel the read the others wait on.
        answer = await asyncio.shield(task)
        if _answers_for(began, answer, asked=asked):
            return answer
        # An idle read that began before this request may predate input: read again, sharing it.
        flight = self._in_flight.get(key)
        if flight is None or flight[0] < asked:
            flight = self._start(key, read)
        return await asyncio.shield(flight[1])

    def _start(
        self, key: str, read: Callable[[], Awaitable[ShellInspection | None]]
    ) -> tuple[float, asyncio.Task[ShellInspection | None]]:
        began = self._clock()
        flight = (began, asyncio.ensure_future(self._read(key, began, read)))
        self._in_flight[key] = flight
        return flight

    async def _read(
        self, key: str, began: float, read: Callable[[], Awaitable[ShellInspection | None]]
    ) -> ShellInspection | None:
        try:
            async with self._slots:
                answer: ShellInspection | None
                try:
                    # Cancelling the read past its budget kills and reaps what it started.
                    answer = await asyncio.wait_for(read(), self._timeout_seconds)
                # A surprise answers unknown, so no exception text holding a row is logged.
                except Exception:  # noqa: BLE001
                    answer = UNKNOWN
            self._answers[key] = (self._clock(), began, answer)
            return answer
        finally:
            # A newer read for this terminal may have replaced this one; leave it.
            flight = self._in_flight.get(key)
            if flight is not None and flight[1] is asyncio.current_task():
                del self._in_flight[key]


def _answers_for(began: float, answer: ShellInspection | None, *, asked: float) -> bool:
    """Whether a read that began at ``began`` can answer a request made at ``asked``."""
    return answer is None or answer.state is not ShellState.IDLE or began >= asked
