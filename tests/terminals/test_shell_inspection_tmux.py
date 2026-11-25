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
from omnigent.terminals.shell_inspection import ShellInspection, ShellState

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
