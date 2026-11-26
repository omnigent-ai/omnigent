"""Unit tests for deciding whether a terminal's shell is idle from one ``ps`` snapshot."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest

from omnigent.terminals import shell_inspection
from omnigent.terminals.shell_inspection import (
    MAX_COMMAND_NAME_LENGTH,
    PS_COLUMNS,
    UNKNOWN,
    ShellInspection,
    ShellInspector,
    ShellState,
    inspect_shell,
    read_pane_shell,
    sanitize_command_name,
)

TMUX = 100
PANE = 200


def snapshot(*rows: tuple[int, int, int, int, str, str]) -> str:
    """``ps -o pid=,ppid=,pgid=,tpgid=,stat=,comm=`` output for ``rows``."""
    lines = [f"{TMUX:>7} 1 {TMUX} -1 Ss tmux"]
    lines += [
        f"{pid:>7} {ppid:>7} {pgid:>7} {tpgid:>7} {stat} {comm}"
        for pid, ppid, pgid, tpgid, stat, comm in rows
    ]
    return "\n".join(lines) + "\n"


# The launchers a bubblewrap terminal shows, with the Python launcher named as probed.
BWRAP_LAUNCHERS = (frozenset({"bwrap"}), frozenset({"bwrap"}), frozenset({"python"}))


def file_of(name: str) -> tuple[int, int]:
    """A stand-in device and inode for the installed program ``name``."""
    return (1, sum(ord(char) * 31**index for index, char in enumerate(name)))


def honest_files(text: str) -> dict[int, tuple[int, int]]:
    """Each pid's file as its ``comm`` names it, as for processes that didn't rename."""
    files: dict[int, tuple[int, int]] = {}
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[0].isdigit():
            files[int(fields[0])] = file_of(fields[5].lstrip("-").rsplit("/", 1)[-1])
    return files


def inspect(
    text: str,
    *,
    shell: str = "bash",
    pane: int = PANE,
    launchers: tuple[frozenset[str], ...] = (),
    files: dict[int, tuple[int, int]] | None = None,
) -> ShellInspection:
    known = honest_files(text) if files is None else files
    return inspect_shell(
        text,
        tmux_server_pid=TMUX,
        pane_pid=pane,
        shell_command=shell,
        launchers=launchers,
        shell_identity=file_of(shell.rsplit("/", 1)[-1]),
        identity_of=known.get,
    )


# The layouts below are ones read from real panes, plain and sandboxed, renumbered.
PLAIN_IDLE = snapshot((PANE, TMUX, PANE, PANE, "Ss+", "bash"))
PLAIN_SLEEP = snapshot(
    (PANE, TMUX, PANE, 300, "Ss", "bash"),
    (300, PANE, 300, 300, "S+", "sleep"),
)
SANDBOX_IDLE = snapshot(
    (PANE, TMUX, PANE, PANE, "Ss+", "bwrap"),
    (201, PANE, 201, -1, "Ss", "bwrap"),
    (202, 201, 201, -1, "S", "python"),
    (203, 202, 201, -1, "S", "bash"),
)
SANDBOX_SLEEP = SANDBOX_IDLE + snapshot((300, 203, 201, -1, "S", "sleep")).split("\n", 1)[1]
# Work the shell left running, adopted by the sandbox's namespace init or left under its launcher.
SANDBOX_ADOPTED = SANDBOX_IDLE + snapshot((300, 201, 300, -1, "S", "sleep")).split("\n", 1)[1]
SANDBOX_UNDER_LAUNCHER = (
    SANDBOX_IDLE + snapshot((301, 202, 301, -1, "S", "node")).split("\n", 1)[1]
)
SANDBOX_SLEEP_AND_ADOPTED = (
    SANDBOX_SLEEP + snapshot((400, 201, 400, -1, "S", "make")).split("\n", 1)[1]
)


@pytest.mark.parametrize(
    ("text", "launchers", "expected"),
    [
        (PLAIN_IDLE, (), ShellInspection(ShellState.IDLE)),
        (PLAIN_SLEEP, (), ShellInspection(ShellState.BUSY, "sleep", 0)),
        (SANDBOX_IDLE, BWRAP_LAUNCHERS, ShellInspection(ShellState.IDLE)),
        # No job control under the sandbox: the shell's child is named, jobs aren't counted.
        (SANDBOX_SLEEP, BWRAP_LAUNCHERS, ShellInspection(ShellState.BUSY, "sleep", None)),
        # Work beneath the pane but outside the shell's tree still ends with the pane.
        (SANDBOX_ADOPTED, BWRAP_LAUNCHERS, ShellInspection(ShellState.BUSY, "sleep", None)),
        (SANDBOX_UNDER_LAUNCHER, BWRAP_LAUNCHERS, ShellInspection(ShellState.BUSY, "node", None)),
        (
            SANDBOX_SLEEP_AND_ADOPTED,
            BWRAP_LAUNCHERS,
            ShellInspection(ShellState.BUSY, "sleep", None),
        ),
        # Another pane or window of the terminal's tmux server ends with it too.
        (
            PLAIN_IDLE + snapshot((210, TMUX, 210, 210, "Ss+", "sleep")).split("\n", 1)[1],
            (),
            UNKNOWN,
        ),
        (
            SANDBOX_IDLE + snapshot((210, TMUX, 210, 210, "Ss+", "bash")).split("\n", 1)[1],
            BWRAP_LAUNCHERS,
            UNKNOWN,
        ),
        (
            PLAIN_IDLE + snapshot((210, TMUX, 210, 210, "Ts", "sleep")).split("\n", 1)[1],
            (),
            UNKNOWN,
        ),
        # tmux can close a window before reaping its exited process.
        (
            PLAIN_IDLE + snapshot((210, TMUX, 210, -1, "Zs", "sleep")).split("\n", 1)[1],
            (),
            ShellInspection(ShellState.IDLE),
        ),
        (
            PLAIN_IDLE + snapshot((210, TMUX, 210, 210, "Zsl+", "python")).split("\n", 1)[1],
            (),
            UNKNOWN,
        ),
        (
            PLAIN_IDLE
            + snapshot(
                (210, TMUX, 210, -1, "Zs", "sleep"),
                (220, TMUX, 220, 220, "Ss+", "bash"),
            ).split("\n", 1)[1],
            (),
            UNKNOWN,
        ),
    ],
    ids=[
        "plain idle",
        "plain sleep",
        "sandbox idle",
        "sandbox sleep",
        "sandbox work adopted by its init",
        "sandbox work under its launcher",
        "sandbox job and adopted work",
        "work in another window",
        "another pane beside a sandbox",
        "stopped work in another window",
        "exited window awaiting reap",
        "zombie leader with live threads",
        "unreaped window beside running work",
    ],
)
def test_reads_the_probed_pane_layouts(
    text: str, launchers: tuple[frozenset[str], ...], expected: ShellInspection
) -> None:
    assert inspect(text, launchers=launchers) == expected


def test_names_the_foreground_job_and_counts_background_jobs() -> None:
    text = snapshot(
        (PANE, TMUX, PANE, 400, "Ss", "zsh"),
        (300, PANE, 300, 400, "S", "tail"),
        (310, PANE, 310, 400, "S", "python3"),
        (311, 310, 310, 400, "S", "python3"),
        (400, PANE, 400, 400, "S+", "vim"),
        (401, 400, 400, 400, "S+", "rg"),
    )
    assert inspect(text, shell="zsh") == ShellInspection(ShellState.BUSY, "vim", 2)


def test_a_shell_in_the_foreground_with_background_jobs_is_busy_without_a_name() -> None:
    text = snapshot(
        (PANE, TMUX, PANE, PANE, "Ss+", "bash"),
        (300, PANE, 300, PANE, "S", "sleep"),
    )
    assert inspect(text) == ShellInspection(ShellState.BUSY, None, 1)


def test_a_login_shell_and_a_full_path_shell_command_match() -> None:
    text = snapshot((PANE, TMUX, PANE, PANE, "Ss+", "-zsh"))
    assert inspect(text, shell="/bin/zsh") == ShellInspection(ShellState.IDLE)


@pytest.mark.parametrize(
    ("text", "pane"),
    [
        # The pane's process is gone.
        (snapshot(), PANE),
        # Its pid now belongs to a process the tmux server didn't start: reused.
        (snapshot((PANE, 1, PANE, PANE, "Ss+", "bash")), PANE),
        # The tmux server isn't in the same snapshot.
        ("\n".join(PLAIN_IDLE.splitlines()[1:]), PANE),
        # Nothing under the pane is the terminal's shell.
        (snapshot((PANE, TMUX, PANE, PANE, "Ss+", "fish")), PANE),
        # Garbage in.
        ("not a ps line\n1 2 3\n", PANE),
        # The shell exec'd a program that runs another bash: the inner bash isn't the pane's.
        (
            snapshot(
                (PANE, TMUX, PANE, PANE, "Ss+", "python3"),
                (300, PANE, PANE, PANE, "R+", "bash"),
            ),
            PANE,
        ),
    ],
    ids=[
        "pane gone",
        "pid reused",
        "no tmux row",
        "no shell",
        "garbage",
        "program replaced the shell",
    ],
)
def test_answers_unknown_when_the_pane_can_not_be_trusted(text: str, pane: int) -> None:
    assert inspect(text, pane=pane) == ShellInspection(ShellState.UNKNOWN)


def test_a_shell_below_a_program_that_is_not_a_launcher_is_unknown() -> None:
    # Inside the sandbox, a program between the launcher and the shell may be work of its own.
    text = snapshot(
        (PANE, TMUX, PANE, PANE, "Ss+", "bwrap"),
        (201, PANE, 201, -1, "Ss", "bwrap"),
        (202, 201, 201, -1, "S", "python"),
        (203, 202, 201, -1, "S", "node"),
        (204, 203, 201, -1, "R", "bash"),
    )
    assert inspect(text, launchers=BWRAP_LAUNCHERS) == UNKNOWN
    # The sandbox's layout read without its launchers isn't trusted either.
    assert inspect(SANDBOX_IDLE) == UNKNOWN


@pytest.mark.parametrize(
    "running",
    [file_of("python3.12"), file_of("vim"), file_of("sleep"), None],
    ids=["python renamed bash", "vim renamed bash", "copy of sleep named bash", "file unreadable"],
)
def test_a_shell_named_process_that_runs_another_file_is_unknown(
    running: tuple[int, int] | None,
) -> None:
    # A process can name itself ``bash``, and a copy of another program can be named bash too;
    # only the configured shell's own file counts.
    files = {} if running is None else {PANE: running}
    assert inspect(PLAIN_IDLE, files=files) == UNKNOWN


def test_a_shell_command_that_resolves_to_nothing_is_unknown() -> None:
    text = snapshot((PANE, TMUX, PANE, PANE, "Ss+", "bash"))
    answer = inspect_shell(
        text,
        tmux_server_pid=TMUX,
        pane_pid=PANE,
        shell_command="bash",
        shell_identity=None,
        identity_of=honest_files(text).get,
    )
    assert answer == UNKNOWN


def test_reads_the_file_a_real_process_runs(tmp_path) -> None:
    # A copy of ``sleep`` named bash runs a different file than the bash the command finds.
    copy = tmp_path / "bash"
    sleep = shutil.which("sleep")
    assert sleep is not None
    shutil.copy(sleep, copy)
    process = subprocess.Popen([str(copy), "30"])
    try:
        running = shell_inspection.process_file_identity(process.pid)
        assert running is not None
        assert running == shell_inspection.configured_shell_identity(str(copy), "")
        assert running != shell_inspection.configured_shell_identity("bash", os.defpath)
    finally:
        process.kill()
        process.wait()
    assert shell_inspection.process_file_identity(2**22 + 12345) is None
    assert shell_inspection.configured_shell_identity("no-such-shell", os.defpath) is None


@pytest.mark.parametrize("command", ["vim", "python3", "sleep", "/usr/bin/less", "bash -l"])
def test_a_terminal_that_runs_another_program_is_unknown_even_when_childless(
    command: str,
) -> None:
    name = command.rsplit("/", 1)[-1]
    text = snapshot((PANE, TMUX, PANE, PANE, "Ss+", name))
    assert inspect(text, shell=command) == UNKNOWN


@pytest.mark.parametrize("command", ["bash", "/bin/zsh", "sh", "fish"])
def test_a_childless_eligible_shell_is_idle(command: str) -> None:
    name = command.rsplit("/", 1)[-1]
    assert inspect(snapshot((PANE, TMUX, PANE, PANE, "Ss+", name)), shell=command) == (
        ShellInspection(ShellState.IDLE)
    )


def test_a_loop_of_shell_builtins_starts_no_process_and_reads_idle() -> None:
    # Idle means nothing else runs, not that the shell waits at its prompt.
    running_a_builtin_loop = snapshot((PANE, TMUX, PANE, PANE, "Rs+", "bash"))
    assert inspect(running_a_builtin_loop) == ShellInspection(ShellState.IDLE)


def test_the_launchers_follow_the_terminals_sandbox() -> None:
    assert shell_inspection.sandbox_launchers(None) == ()
    bwrap = shell_inspection.sandbox_launchers("linux_bwrap")
    assert bwrap[:2] == (frozenset({"bwrap"}), frozenset({"bwrap"}))
    python = os.path.basename(sys.executable)
    assert python in bwrap[2] or python[:15] in bwrap[2]
    assert shell_inspection.sandbox_launchers("darwin_seatbelt") == (bwrap[2],)


def test_processes_outside_the_pane_never_reach_the_answer() -> None:
    text = PLAIN_IDLE + snapshot((900, 1, 900, 900, "S+", "secret-tool")).split("\n", 1)[1]
    result = inspect(text)
    assert result == ShellInspection(ShellState.IDLE)
    assert "secret" not in repr(result)


@pytest.mark.parametrize(
    ("comm", "name"),
    [
        ("vim", "vim"),
        ("/Users/someone/project/bin/tool", "tool"),
        ("-zsh", "zsh"),
        ("\x1b[31mred\x1b[0m", "red"),
        ("evil‮gnp.exe", "evilgnp.exe"),
        ("tab\there\x07", "tabhere"),
        ("x" * 80, "x" * MAX_COMMAND_NAME_LENGTH),
        ("\x1b]0;title\x07", None),
        ("/", None),
    ],
    ids=[
        "plain",
        "mac full path",
        "login dash",
        "ansi",
        "bidi",
        "control",
        "long",
        "only osc",
        "empty",
    ],
)
def test_sanitizes_the_command_name(comm: str, name: str | None) -> None:
    assert sanitize_command_name(comm) == name


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"state": "idle", "foreground": "x", "background_jobs": 3},
            ShellInspection(ShellState.IDLE),
        ),
        (
            {"state": "busy", "foreground": "vim", "background_jobs": 2},
            ShellInspection(ShellState.BUSY, "vim", 2),
        ),
        # The name is cleaned again on this side.
        (
            {"state": "busy", "foreground": "\x1b[31m/usr/bin/vim\u202e", "background_jobs": 0},
            ShellInspection(ShellState.BUSY, "vim", 0),
        ),
        (
            {"state": "busy", "foreground": 7, "background_jobs": None},
            ShellInspection(ShellState.BUSY),
        ),
        # Only a whole number from 0 counts.
        (
            {"state": "busy", "foreground": None, "background_jobs": -1},
            ShellInspection(ShellState.BUSY),
        ),
        (
            {"state": "busy", "foreground": None, "background_jobs": 1.5},
            ShellInspection(ShellState.BUSY),
        ),
        (
            {"state": "busy", "foreground": None, "background_jobs": True},
            ShellInspection(ShellState.BUSY),
        ),
        (
            {"state": "busy", "foreground": None, "background_jobs": "2"},
            ShellInspection(ShellState.BUSY),
        ),
        ({"state": "running"}, UNKNOWN),
        (["idle"], UNKNOWN),
        (None, UNKNOWN),
    ],
    ids=[
        "idle drops extras",
        "busy",
        "dirty name",
        "name not text",
        "negative count",
        "fractional count",
        "bool count",
        "text count",
        "bad state",
        "not an object",
        "null",
    ],
)
def test_rebuilds_an_answer_from_the_expected_fields_only(
    payload: object, expected: ShellInspection
) -> None:
    rebuilt = ShellInspection.from_untrusted(payload)
    assert rebuilt == expected
    assert set(rebuilt.to_dict()) == {"state", "foreground", "background_jobs"}


# A stand-in tmux server: it starts a real ``sh`` waiting on its input, its only pane, and
# prints that shell's pid.
_SERVER = (
    "import subprocess, sys; "
    "shell = subprocess.Popen(['sh'], stdin=subprocess.PIPE); "
    "print(shell.pid, flush=True); sys.stdin.read(); shell.kill(); shell.wait()"
)


@pytest.fixture
async def pane_process() -> AsyncIterator[tuple[int, int]]:
    """``(server pid, pane pid)``: a real shell whose parent plays its tmux server."""
    server = subprocess.Popen(
        [sys.executable, "-I", "-c", _SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    assert server.stdout is not None and server.stdin is not None
    try:
        yield server.pid, int(server.stdout.readline())
    finally:
        server.stdin.close()
        server.wait(timeout=10)
        server.stdout.close()


def _fake_tmux(pids: str) -> list[str]:
    """A tmux argv prefix that prints ``pids`` for ``display-message``."""
    return [sys.executable, "-I", "-c", f"print({pids!r})"]


@pytest.mark.asyncio
async def test_reads_a_real_process_table_for_the_pane(pane_process: tuple[int, int]) -> None:
    server, pane = pane_process
    tmux = _fake_tmux(f"{server} {pane}")
    assert await read_pane_shell(tmux, "main", "sh") == ShellInspection(ShellState.IDLE)
    # The same childless process read as another configured program can't be called idle.
    assert await read_pane_shell(tmux, "main", "vim") == UNKNOWN


@pytest.mark.asyncio
async def test_a_pane_that_is_not_the_tmux_servers_child_is_unknown(
    pane_process: tuple[int, int],
) -> None:
    # Same pane, but tmux claims another server: as if the pid had been reused.
    tmux = _fake_tmux(f"1 {pane_process[1]}")
    assert await read_pane_shell(tmux, "main", "sh") == UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tmux",
    [
        _fake_tmux("not pids"),
        _fake_tmux("1 2 3"),
        [sys.executable, "-I", "-c", "import sys; print('100 200'); sys.exit(1)"],
        ["/nonexistent/tmux"],
    ],
    ids=["garbage", "extra field", "tmux fails", "no tmux"],
)
async def test_a_failing_tmux_is_unknown_and_nothing_it_printed_is_logged(
    tmux: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert await read_pane_shell(tmux, "main", "bash") == UNKNOWN
    assert "not pids" not in caplog.text and "200" not in caplog.text


class _EndlessProcess:
    """A subprocess that prints ``output`` and then nothing more, until it is killed."""

    def __init__(self, output: bytes = b"") -> None:
        self.stdout = asyncio.StreamReader()
        if output:
            self.stdout.feed_data(output)
        self.returncode: int | None = None
        self.killed = asyncio.Event()

    def kill(self) -> None:
        self.returncode = -9
        self.killed.set()

    async def wait(self) -> int:
        await self.killed.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.mark.asyncio
async def test_output_past_the_cap_stops_the_read_without_waiting_for_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _EndlessProcess(b"x" * (shell_inspection._MAX_OUTPUT_BYTES + 1))

    async def start(*argv: str, **options: object) -> _EndlessProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    # The output never ends, so the read can only finish by giving up at the cap; the bound
    # only stops a regression that waits for the rest from hanging the run.
    with pytest.raises(shell_inspection._CommandFailed):
        await asyncio.wait_for(shell_inspection._run(["ps"], "/usr/bin"), 60)
    assert process.killed.is_set()
    assert not process.stdout.at_eof()


@pytest.mark.asyncio
async def test_a_tmux_that_never_answers_is_unknown_and_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = _EndlessProcess()

    async def start(*argv: str, **options: object) -> _EndlessProcess:
        return tmux

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    # tmux never answers, so the budget always runs out, however short or busy the machine.
    monkeypatch.setattr(shell_inspection, "INSPECTION_TIMEOUT_SECONDS", 0.01)
    answer = await asyncio.wait_for(read_pane_shell(["tmux"], "main", "bash"), 60)
    assert answer == UNKNOWN
    assert tmux.killed.is_set()


IDLE = ShellInspection(ShellState.IDLE)
BUSY_VIM = ShellInspection(ShellState.BUSY, "vim", 0)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_concurrent_requests_for_one_terminal_share_one_read() -> None:
    reads = 0
    release = asyncio.Event()

    async def read() -> ShellInspection:
        nonlocal reads
        reads += 1
        await release.wait()
        return BUSY_VIM

    inspector = ShellInspector()
    waiting = [asyncio.create_task(inspector.inspect("terminal-a", read)) for _ in range(3)]
    await asyncio.sleep(0)
    # One caller giving up leaves the read the others wait on running.
    waiting[0].cancel()
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*waiting[1:]) == [BUSY_VIM] * 2
    assert waiting[0].cancelled()
    assert reads == 1


@pytest.mark.asyncio
async def test_a_read_past_the_budget_is_cancelled_and_answers_unknown() -> None:
    cancelled = asyncio.Event()

    async def stalled() -> ShellInspection:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    inspector = ShellInspector(timeout_seconds=0.05)
    assert await asyncio.wait_for(inspector.inspect("terminal-a", stalled), 2) == UNKNOWN
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_a_gone_terminal_answers_none() -> None:
    async def gone() -> None:
        return None

    assert await ShellInspector().inspect("terminal-a", gone) is None


@pytest.mark.asyncio
async def test_an_answer_is_reused_briefly_then_read_again() -> None:
    clock = _Clock()
    answers = iter([BUSY_VIM, UNKNOWN])

    async def read() -> ShellInspection:
        return next(answers)

    inspector = ShellInspector(cache_seconds=0.25, clock=clock)
    assert await inspector.inspect("terminal-a", read) == BUSY_VIM
    clock.now = 0.2
    assert await inspector.inspect("terminal-a", read) == BUSY_VIM
    clock.now = 0.3
    assert await inspector.inspect("terminal-a", read) == UNKNOWN


@pytest.mark.asyncio
async def test_an_idle_answer_from_before_the_request_is_read_again() -> None:
    clock = _Clock()
    answers = iter([IDLE, BUSY_VIM])
    reads = 0

    async def read() -> ShellInspection:
        nonlocal reads
        reads += 1
        return next(answers)

    inspector = ShellInspector(cache_seconds=0.25, clock=clock)
    assert await inspector.inspect("terminal-a", read) == IDLE
    # Within the cache window, but input may have come since that read began.
    clock.now = 0.1
    assert await inspector.inspect("terminal-a", read) == BUSY_VIM
    assert reads == 2


@pytest.mark.asyncio
async def test_a_request_that_joins_an_earlier_read_answering_idle_reads_again() -> None:
    clock = _Clock()
    release = asyncio.Event()
    answers = iter([IDLE, BUSY_VIM])
    reads = 0

    async def read() -> ShellInspection:
        nonlocal reads
        reads += 1
        answer = next(answers)
        if answer is IDLE:
            await release.wait()
        return answer

    inspector = ShellInspector(clock=clock)
    first = asyncio.create_task(inspector.inspect("terminal-a", read))
    await asyncio.sleep(0)
    # Asked while the first read runs: typing may land between them.
    clock.now = 0.1
    second = asyncio.create_task(inspector.inspect("terminal-a", read))
    await asyncio.sleep(0)
    release.set()
    assert await first == IDLE
    assert await second == BUSY_VIM
    assert reads == 2


@pytest.mark.asyncio
async def test_requests_after_a_stale_idle_share_one_new_read() -> None:
    clock = _Clock()
    answers = iter([IDLE, IDLE])
    reads = 0
    release = asyncio.Event()

    async def read() -> ShellInspection:
        nonlocal reads
        reads += 1
        answer = next(answers)
        if reads == 2:
            await release.wait()
        return answer

    inspector = ShellInspector(clock=clock)
    assert await inspector.inspect("terminal-a", read) == IDLE
    clock.now = 0.1
    later = [asyncio.create_task(inspector.inspect("terminal-a", read)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*later) == [IDLE] * 3
    assert reads == 2


@pytest.mark.asyncio
async def test_the_cache_never_answers_for_another_terminal() -> None:
    async def idle() -> ShellInspection:
        return ShellInspection(ShellState.IDLE)

    async def busy() -> ShellInspection:
        return ShellInspection(ShellState.BUSY, "vim", 0)

    inspector = ShellInspector()
    assert await inspector.inspect("terminal-a", idle) == ShellInspection(ShellState.IDLE)
    assert await inspector.inspect("terminal-b", busy) == ShellInspection(
        ShellState.BUSY, "vim", 0
    )


@pytest.mark.asyncio
async def test_inspections_across_terminals_stay_under_the_limit() -> None:
    running = 0
    most = 0
    release = asyncio.Event()

    async def read() -> ShellInspection:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await release.wait()
        running -= 1
        return ShellInspection(ShellState.IDLE)

    inspector = ShellInspector(max_concurrent=4)
    waiting = [asyncio.create_task(inspector.inspect(f"terminal-{n}", read)) for n in range(10)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert running == 4
    release.set()
    await asyncio.gather(*waiting)
    assert most == 4


@pytest.mark.asyncio
async def test_a_read_that_raises_answers_unknown_and_logs_nothing_of_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def read() -> ShellInspection:
        raise RuntimeError("900 1 900 900 S+ secret-tool")

    assert await ShellInspector().inspect("terminal-a", read) == UNKNOWN
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "line",
    [
        # The rest of a comm with a newline in it, on its own line.
        "zsh evil 300 300 S+ vim",
        "x 1 2 3 S+ sleep",
        # Too few fields to be a row.
        "300 200",
    ],
    ids=["comm continuation", "numbers out of place", "short line"],
)
def test_a_line_that_is_not_a_row_makes_the_answer_unknown(line: str) -> None:
    # Dropping the line could hide a process under the pane, so nothing is trusted.
    assert inspect(PLAIN_IDLE.replace(" bash\n", f" bash\n{line}\n")) == UNKNOWN


def test_a_child_with_an_empty_name_keeps_the_shell_busy() -> None:
    # ``ps`` prints nothing for an empty comm: the child is still there, only unnamed.
    text = PLAIN_IDLE + f"    300 {PANE:>7}     300 {PANE:>7} S \n"
    assert inspect(text) == ShellInspection(ShellState.BUSY, None, 1)
    sandboxed = SANDBOX_IDLE + "    300     203     201      -1 S\n"
    assert inspect(sandboxed, launchers=BWRAP_LAUNCHERS) == ShellInspection(
        ShellState.BUSY, None, None
    )


def test_a_pid_listed_twice_is_unknown() -> None:
    # A forged line that does parse would otherwise stand in for the real row with that pid.
    forged = PLAIN_SLEEP + f"{PANE:>7} {TMUX:>7} {PANE:>7} {PANE:>7} Ss+ bash\n"
    assert inspect(forged) == UNKNOWN


@pytest.mark.asyncio
async def test_ps_is_never_asked_for_arguments_or_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert PS_COLUMNS == "pid=,ppid=,pgid=,tpgid=,stat=,comm="
    calls: list[list[str]] = []

    async def fake_run(argv: list[str], path: str) -> str:
        calls.append(list(argv))
        if "display-message" in argv:
            return f"{TMUX} {PANE}\n"
        return PLAIN_IDLE

    monkeypatch.setattr(shell_inspection, "_run", fake_run)
    monkeypatch.setattr(shell_inspection, "process_file_identity", lambda pid: (1, 2))
    monkeypatch.setattr(
        shell_inspection, "configured_shell_identity", lambda command, path: (1, 2)
    )
    assert await read_pane_shell(["tmux"], "main", "bash") == ShellInspection(ShellState.IDLE)
    # Exactly these arguments: no `-E` environment, no `args` or `command` column.
    assert calls[-1][1:] == ["-e", "-o", PS_COLUMNS]
