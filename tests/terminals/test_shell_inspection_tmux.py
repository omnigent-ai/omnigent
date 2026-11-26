"""Inspecting a real terminal's shell through its own tmux server: idle, busy, and sandboxed."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance, create_terminal_instance
from omnigent.terminals.shell_inspection import UNKNOWN, ShellInspection, ShellState

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs a real tmux on PATH")

_IDLE = ShellInspection(ShellState.IDLE)


async def _settles_on(instance: TerminalInstance, expected: ShellInspection) -> ShellInspection:
    """Read the shell until it answers ``expected``; a shell takes a moment to start a job."""

    async def read() -> ShellInspection:
        while (answer := await instance.inspect_shell()) != expected:
            await asyncio.sleep(0.1)
        return answer

    return await asyncio.wait_for(read(), timeout=30)


@pytest.fixture
async def bash_terminal(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[TerminalInstance]:
    sandbox: str | None = request.param
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(type=sandbox) if sandbox else OSEnvSandboxSpec(type="none"),
    )
    spec = TerminalEnvSpec(command="bash", args=["--noprofile", "--norc"], os_env=os_env)
    instance = create_terminal_instance(
        name="bash", session_key=sandbox or "plain", spec=spec
    ).instance
    try:
        await instance.launch(cwd=tmp_path)
        yield instance
    finally:
        await instance.close()
        shutil.rmtree(instance.private_dir, ignore_errors=True)


_SANDBOXED = pytest.param(
    "linux_bwrap",
    marks=pytest.mark.skipif(
        sys.platform != "linux" or shutil.which("bwrap") is None, reason="needs bubblewrap"
    ),
    id="bubblewrap",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bash_terminal", [pytest.param(None, id="plain"), _SANDBOXED], indirect=True
)
async def test_reads_a_real_shell_idle_then_busy_then_idle_again(
    bash_terminal: TerminalInstance, request: pytest.FixtureRequest
) -> None:
    sandboxed = "bubblewrap" in request.node.callspec.id
    await _settles_on(bash_terminal, _IDLE)

    # Long enough to read busy; it ends by itself, as Ctrl-C can't reach a sandboxed job.
    await bash_terminal.send("sleep 3")
    # A sandboxed pane has no job control, so its jobs aren't counted.
    busy = ShellInspection(ShellState.BUSY, "sleep", None if sandboxed else 0)
    await _settles_on(bash_terminal, busy)
    await _settles_on(bash_terminal, _IDLE)


@pytest.mark.asyncio
@pytest.mark.parametrize("bash_terminal", [_SANDBOXED], indirect=True)
async def test_work_the_shell_left_running_keeps_a_sandboxed_shell_busy(
    bash_terminal: TerminalInstance,
) -> None:
    await _settles_on(bash_terminal, _IDLE)
    # A subshell's background job outlives it: the shell is back at its prompt, but under a
    # sandbox the job stays beneath the pane, adopted by the namespace's init.
    await bash_terminal.send("(sleep 4 &)")
    await _settles_on(bash_terminal, ShellInspection(ShellState.BUSY, "sleep", None))
    await _settles_on(bash_terminal, _IDLE)


@pytest.mark.asyncio
@pytest.mark.parametrize("bash_terminal", [pytest.param(None, id="plain")], indirect=True)
async def test_a_program_that_replaced_the_shell_and_runs_another_bash_is_unknown(
    bash_terminal: TerminalInstance, tmp_path: Path
) -> None:
    await _settles_on(bash_terminal, _IDLE)
    # The inner bash marks that it runs with a builtin, then spins on builtins, so it has no
    # children of its own to give it away.
    marker = tmp_path / "inner-bash-runs"
    inner = f": > {shlex.quote(str(marker))}; SECONDS=0; while ((SECONDS < 20)); do :; done"
    program = f"import subprocess; subprocess.run(['bash', '-c', {inner!r}])"
    await bash_terminal.send(f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}")

    async def inner_bash_runs() -> None:
        while not marker.exists():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(inner_bash_runs(), timeout=30)
    for _ in range(5):
        assert await bash_terminal.inspect_shell() == UNKNOWN
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bash_terminal", [pytest.param(None, id="plain")], indirect=True)
async def test_a_busy_jobs_arguments_and_environment_never_reach_the_answer(
    bash_terminal: TerminalInstance, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _settles_on(bash_terminal, _IDLE)
    python = shlex.quote(sys.executable)
    job = f"{python} -c 'import time; time.sleep(4)' argv-secret-value"
    await bash_terminal.send(f"EXAMPLE_TOKEN=env-secret-value {job}")

    async def busy() -> ShellInspection:
        while (answer := await bash_terminal.inspect_shell()).state is not ShellState.BUSY:
            await asyncio.sleep(0.1)
        return answer

    answer = await asyncio.wait_for(busy(), timeout=30)
    # Only the program's own name: its arguments and environment stay on the host.
    assert answer.foreground is not None
    wire = json.dumps(answer.to_dict())
    for secret in ("argv-secret-value", "env-secret-value", "EXAMPLE_TOKEN", "time.sleep"):
        assert secret not in wire
        assert secret not in caplog.text
    await _settles_on(bash_terminal, _IDLE)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="renames itself with Linux's prctl")
@pytest.mark.parametrize("bash_terminal", [pytest.param(None, id="plain")], indirect=True)
async def test_a_job_with_an_empty_name_keeps_the_shell_busy(
    bash_terminal: TerminalInstance,
) -> None:
    await _settles_on(bash_terminal, _IDLE)
    # PR_SET_NAME to "", so ``ps`` prints no comm at all for the job.
    program = "import ctypes, time; ctypes.CDLL(None).prctl(15, b'', 0, 0, 0); time.sleep(4)"
    await bash_terminal.send(f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}")
    await _settles_on(bash_terminal, ShellInspection(ShellState.BUSY, None, 0))
    await _settles_on(bash_terminal, _IDLE)


@pytest.mark.asyncio
@pytest.mark.parametrize("bash_terminal", [pytest.param(None, id="plain")], indirect=True)
async def test_work_in_another_window_of_the_terminal_is_unknown_until_it_ends(
    bash_terminal: TerminalInstance,
) -> None:
    await _settles_on(bash_terminal, _IDLE)
    # The pane's own tmux server opens it, so closing the terminal would end it too.
    await bash_terminal.send("tmux new-window -d 'sleep 4'")
    await _settles_on(bash_terminal, UNKNOWN)
    # Its window closes with the job, leaving the shell alone again.
    await _settles_on(bash_terminal, _IDLE)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="renames itself with Linux's prctl")
@pytest.mark.parametrize("bash_terminal", [pytest.param(None, id="plain")], indirect=True)
async def test_a_program_that_replaced_the_shell_and_named_itself_bash_is_unknown(
    bash_terminal: TerminalInstance, tmp_path: Path
) -> None:
    await _settles_on(bash_terminal, _IDLE)
    marker = tmp_path / "renamed"
    program = (
        "import ctypes, pathlib, time; ctypes.CDLL(None).prctl(15, b'bash', 0, 0, 0); "
        f"pathlib.Path({str(marker)!r}).touch(); time.sleep(20)"
    )
    await bash_terminal.send(f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}")

    async def renamed() -> None:
        while not marker.exists():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(renamed(), timeout=30)
    # Childless and named bash, but its executable is still Python's.
    for _ in range(5):
        assert await bash_terminal.inspect_shell() == UNKNOWN
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="reads the pane's file from /proc")
@pytest.mark.parametrize("bash_terminal", [pytest.param(None, id="plain")], indirect=True)
async def test_a_copy_of_another_program_named_bash_in_place_of_the_shell_is_unknown(
    bash_terminal: TerminalInstance, tmp_path: Path
) -> None:
    await _settles_on(bash_terminal, _IDLE)
    sleep = shutil.which("sleep")
    assert sleep is not None
    copy = tmp_path / "copies" / "bash"
    copy.parent.mkdir()
    shutil.copy(sleep, copy)
    await bash_terminal.send(f"exec {shlex.quote(str(copy))} 20")
    pane = bash_terminal.pane_pid_sync()
    assert pane is not None
    copied = copy.stat()

    async def replaced() -> None:
        while True:
            try:
                running = Path(f"/proc/{pane}/exe").stat()
            except OSError:
                running = None
            if running and (running.st_dev, running.st_ino) == (copied.st_dev, copied.st_ino):
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(replaced(), timeout=30)
    # Childless and named bash, but it runs a copy of sleep, not the bash the command finds.
    for _ in range(5):
        assert await bash_terminal.inspect_shell() == UNKNOWN
        await asyncio.sleep(0.1)
