"""Unit tests for deciding whether a terminal's shell is idle from one ``ps`` snapshot."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator

import pytest

from omnigent.terminals import shell_inspection
from omnigent.terminals.shell_inspection import (
    INSPECTION_TIMEOUT_SECONDS,
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


def inspect(
    text: str,
    *,
    shell: str = "bash",
    pane: int = PANE,
    launchers: tuple[frozenset[str], ...] = (),
) -> ShellInspection:
    return inspect_shell(
        text, tmux_server_pid=TMUX, pane_pid=pane, shell_command=shell, launchers=launchers
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
    ],
    ids=[
        "plain idle",
        "plain sleep",
        "sandbox idle",
        "sandbox sleep",
        "sandbox work adopted by its init",
        "sandbox work under its launcher",
        "sandbox job and adopted work",
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


@pytest.fixture
async def pane_process() -> AsyncIterator[int]:
    """A real ``sh`` child of this process waiting on its input, standing in for a pane's shell."""
    process = subprocess.Popen(["sh"], stdin=subprocess.PIPE)
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()
        assert process.stdin is not None
        process.stdin.close()


def _fake_tmux(pids: str) -> list[str]:
    """A tmux argv prefix that prints ``pids`` for ``display-message``."""
    return [sys.executable, "-I", "-c", f"print({pids!r})"]


@pytest.mark.asyncio
async def test_reads_a_real_process_table_for_the_pane(pane_process: int) -> None:
    # The pane is this process's child, so this process plays its tmux server.
    tmux = _fake_tmux(f"{os.getpid()} {pane_process}")
    assert await read_pane_shell(tmux, "main", "sh") == ShellInspection(ShellState.IDLE)
    # The same childless process read as another configured program can't be called idle.
    assert await read_pane_shell(tmux, "main", "vim") == UNKNOWN


@pytest.mark.asyncio
async def test_a_pane_that_is_not_the_tmux_servers_child_is_unknown(pane_process: int) -> None:
    # Same pane, but tmux claims another server: as if the pid had been reused.
    tmux = _fake_tmux(f"1 {pane_process}")
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


@pytest.mark.asyncio
async def test_a_slow_tmux_is_unknown_within_the_budget() -> None:
    tmux = [sys.executable, "-I", "-c", "import time; time.sleep(5)"]
    started = time.monotonic()
    assert await read_pane_shell(tmux, "main", "bash") == UNKNOWN
    assert time.monotonic() - started < INSPECTION_TIMEOUT_SECONDS + 1


@pytest.mark.asyncio
async def test_output_past_the_cap_stops_the_read_without_waiting_for_the_rest() -> None:
    # 12 MiB, then a pause: the read gives up at the cap instead of buffering all of it.
    flood = [
        sys.executable,
        "-I",
        "-c",
        "import sys, time; sys.stdout.write('x' * (12 << 20)); sys.stdout.flush(); time.sleep(30)",
    ]
    with pytest.raises(shell_inspection._CommandFailed):
        await asyncio.wait_for(shell_inspection._run(flood, os.environ["PATH"]), 10)


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
        return ShellInspection(ShellState.IDLE)

    inspector = ShellInspector()
    waiting = [asyncio.create_task(inspector.inspect("terminal-a", read)) for _ in range(3)]
    await asyncio.sleep(0)
    # One caller giving up leaves the read the others wait on running.
    waiting[0].cancel()
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*waiting[1:]) == [ShellInspection(ShellState.IDLE)] * 2
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
    answers = iter([ShellInspection(ShellState.IDLE), ShellInspection(ShellState.BUSY, "vim", 0)])

    async def read() -> ShellInspection:
        return next(answers)

    inspector = ShellInspector(cache_seconds=0.25, clock=clock)
    assert await inspector.inspect("terminal-a", read) == ShellInspection(ShellState.IDLE)
    clock.now = 0.2
    assert await inspector.inspect("terminal-a", read) == ShellInspection(ShellState.IDLE)
    clock.now = 0.3
    assert await inspector.inspect("terminal-a", read) == ShellInspection(
        ShellState.BUSY, "vim", 0
    )


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


def test_drops_lines_whose_first_four_fields_are_not_numbers() -> None:
    # A comm with a newline in it puts the rest on its own line, which must not parse as a row.
    forged = PLAIN_SLEEP.replace(" sleep\n", " sleep\nzsh evil 300 300 S+ vim\n")
    assert forged != PLAIN_SLEEP
    assert inspect(forged) == ShellInspection(ShellState.BUSY, "sleep", 0)
    hidden = PLAIN_IDLE.replace(" bash\n", " bash\nx 1 2 3 S+ sleep\n")
    assert inspect(hidden) == ShellInspection(ShellState.IDLE)


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
    assert await read_pane_shell(["tmux"], "main", "bash") == ShellInspection(ShellState.IDLE)
    # Exactly these arguments: no `-E` environment, no `args` or `command` column.
    assert calls[-1][1:] == ["-e", "-o", PS_COLUMNS]
