"""Behavioral I/O tests for :class:`TerminalRegistry` against a real tmux.

These complement :mod:`tests.terminals.test_registry` (registry
lifecycle, no keystrokes) and :mod:`tests.tools.builtins.test_sys_terminal`
(the same behaviors through the ``sys_terminal_*`` tool envelopes) by
driving ``TerminalRegistry.launch`` → ``TerminalInstance.send`` / ``.read``
directly: interactive state that survives across calls, cwd anchoring of
the live shell, control-key delivery, and per-session isolation.

End-to-end coverage of the same capability would need a live runner and a
real LLM. These reach the same tmux behaviors with neither, so the
capability keeps coverage in the ``tests/terminals`` CI shard (which
installs tmux).

Skipped when tmux is absent. ``send`` is asynchronous from the shell's
view, so reads poll on a bounded budget rather than asserting a single
capture.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance
from omnigent.terminals import TerminalRegistry

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; registry I/O tests need a real tmux on PATH",
)

_MARKER_BUDGET_S = 5.0
_POLL_INTERVAL_S = 0.1


def _dewrap(screen: str) -> str:
    """Join the pane's ``-x 80`` soft-wrapped rows so a needle straddling the wrap matches."""
    return screen.replace("\n", "")


def _echo_only_on_run(marker: str) -> str:
    """An ``echo`` whose *typed* form can't contain *marker* — only its output can.

    The empty ``""`` splits the literal in the keystroke echo (and in any
    ``_dewrap`` join of the wrapped command line), so a needle search proves
    the command produced output rather than merely that it was typed.
    """
    mid = len(marker) // 2
    return f'echo {marker[:mid]}""{marker[mid:]}'


def _bash_spec(cwd: Path, *, allow_cwd_override: bool = False) -> TerminalEnvSpec:
    return TerminalEnvSpec(
        command="bash",
        allow_cwd_override=allow_cwd_override,
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


async def _read_until(
    instance: TerminalInstance,
    needle: str,
    *,
    budget_s: float = _MARKER_BUDGET_S,
) -> str:
    """Poll ``instance.read`` until *needle* appears or the budget elapses.

    Returns the last pane text seen — containing *needle* on success, or
    the final capture (for a useful failure message) on timeout.
    """
    waited = 0.0
    screen = ""
    while waited < budget_s:
        screen = _dewrap((await instance.read()).get("screen", ""))
        if needle in screen:
            return screen
        await asyncio.sleep(_POLL_INTERVAL_S)
        waited += _POLL_INTERVAL_S
    return screen


def _path_tail(*parts: str) -> str:
    """Join path segments into a tmux-pwd needle.

    Matching a two-segment tail (parent/leaf) rather than a bare leaf
    keeps the assertion off a basename-only shell prompt and off the
    macOS ``/var`` → ``/private/var`` symlink rewrite, both of which
    would otherwise let a test pass without the real pwd in the pane.
    """
    return str(Path(*parts))


@pytest.fixture
def reg() -> TerminalRegistry:
    return TerminalRegistry()


@pytest.fixture
async def shutdown_terminals(reg: TerminalRegistry) -> AsyncIterator[None]:
    """Close every terminal at test exit, even when an assertion fails."""
    yield
    await reg.shutdown()


async def test_shell_state_persists_across_separate_sends(
    reg: TerminalRegistry, shutdown_terminals: None, tmp_path: Path
) -> None:
    """A variable set in one ``send`` is still set in a later ``send``.

    The capability ``sys_terminal_*`` adds over a one-shot ``sys_os_shell``:
    one long-lived shell across calls. A "fresh shell per send" regression
    passes every lifecycle test yet fails here.
    """
    instance = await reg.launch("conv_a", "bash", "s1", _bash_spec(tmp_path))

    await instance.send(text="MARKER_VAR=persisted_value", keys="Enter")
    await instance.send(text="echo VAR_IS_$MARKER_VAR", keys="Enter")

    screen = await _read_until(instance, "VAR_IS_persisted_value")
    assert "VAR_IS_persisted_value" in screen, (
        "variable from the first send was not visible in the second — each "
        f"send is spawning a fresh shell. Last pane:\n{screen!r}"
    )


async def test_working_directory_change_persists_across_sends(
    reg: TerminalRegistry, shutdown_terminals: None, tmp_path: Path
) -> None:
    """A ``cd`` in one send is reflected by ``pwd`` in a later send."""
    subdir = tmp_path / "nested_dir"
    subdir.mkdir()
    instance = await reg.launch("conv_a", "bash", "s1", _bash_spec(tmp_path))

    await instance.send(text="cd nested_dir", keys="Enter")
    await instance.send(text="pwd", keys="Enter")

    needle = _path_tail(tmp_path.name, "nested_dir")
    screen = await _read_until(instance, needle)
    assert needle in screen, (
        f"cd from the first send did not persist into the second send's pwd. "
        f"Last pane:\n{screen!r}"
    )


async def test_launched_shell_starts_in_spec_cwd(
    reg: TerminalRegistry, shutdown_terminals: None, tmp_path: Path
) -> None:
    """``pwd`` in a freshly launched shell reports the spec's cwd.

    The behavioral half of ``_resolve_cwd``'s precedence logic, which is
    unit-tested in isolation but never proven against a live shell.
    """
    instance = await reg.launch("conv_a", "bash", "s1", _bash_spec(tmp_path))

    await instance.send(text="pwd", keys="Enter")

    needle = _path_tail(tmp_path.parent.name, tmp_path.name)
    screen = await _read_until(instance, needle)
    assert needle in screen, (
        f"launched shell's pwd did not report the spec cwd {tmp_path}. Last pane:\n{screen!r}"
    )


async def test_cwd_override_anchors_live_shell_in_subdirectory(
    reg: TerminalRegistry, shutdown_terminals: None, tmp_path: Path
) -> None:
    """A per-launch ``cwd_override`` starts the live shell in that subdir."""
    override_dir = tmp_path / "workdir"
    override_dir.mkdir()

    instance = await reg.launch(
        "conv_a",
        "bash",
        "s1",
        _bash_spec(tmp_path, allow_cwd_override=True),
        cwd_override=str(override_dir),
    )

    assert instance.launch_cwd is not None
    assert Path(instance.launch_cwd).name == "workdir", (
        f"launch_cwd did not reflect the cwd_override; got {instance.launch_cwd!r}"
    )

    await instance.send(text="pwd", keys="Enter")
    needle = _path_tail(tmp_path.name, "workdir")
    screen = await _read_until(instance, needle)
    assert needle in screen, f"cwd_override did not anchor the live shell. Last pane:\n{screen!r}"


async def test_ctrl_c_interrupts_running_command(
    reg: TerminalRegistry, shutdown_terminals: None, tmp_path: Path
) -> None:
    """``keys="C-c"`` interrupts a running foreground command.

    Affirmative, not merely "the prompt recovered": C-c is sent only after
    the foreground job's own output proves it is executing (not still at an
    empty prompt), the sleep is long enough that an ineffective C-c leaves
    the recovery echo queued behind it past the poll budget, and the job's
    post-sleep marker must be absent — so a no-op C-c fails the test.
    """
    instance = await reg.launch("conv_a", "bash", "s1", _bash_spec(tmp_path))

    running = _echo_only_on_run("FOREGROUND_RUNNING")
    not_interrupted = _echo_only_on_run("SLEEP_FINISHED_NOT_INTERRUPTED")
    await instance.send(text=f"{running} && sleep 120 && {not_interrupted}", keys="Enter")

    started = await _read_until(instance, "FOREGROUND_RUNNING")
    assert "FOREGROUND_RUNNING" in started, (
        "foreground job never started, so C-c would land on an empty prompt and "
        f"prove nothing. Last pane:\n{started!r}"
    )

    interrupt = await instance.send(text=None, keys="C-c")
    assert interrupt.get("status") == "sent", f"C-c send failed: {interrupt!r}"

    await instance.send(text=_echo_only_on_run("INTERRUPT_RECOVERED_OK"), keys="Enter")
    screen = await _read_until(instance, "INTERRUPT_RECOVERED_OK")
    assert "INTERRUPT_RECOVERED_OK" in screen, (
        "recovery echo never ran, so the foreground `sleep` was not interrupted "
        f"and still holds the shell. Last pane:\n{screen!r}"
    )
    assert "SLEEP_FINISHED_NOT_INTERRUPTED" not in screen, (
        "the sleep ran to completion, so C-c did not interrupt the foreground "
        f"command list. Last pane:\n{screen!r}"
    )


async def test_send_and_read_after_close_report_not_running(
    reg: TerminalRegistry, shutdown_terminals: None, tmp_path: Path
) -> None:
    """Once closed, the instance's ``send`` / ``read`` error cleanly.

    ``test_registry.py`` proves ``close`` removes the registry entry; this
    proves the instance refuses I/O afterward instead of talking to a dead
    socket.
    """
    instance = await reg.launch("conv_a", "bash", "s1", _bash_spec(tmp_path))
    alive = await instance.is_alive()
    assert alive

    close_result = await reg.close("conv_a", "bash", "s1")
    assert close_result is True
    assert instance.running is False
    alive_after_close = await instance.is_alive()
    assert alive_after_close is False

    send_result = await instance.send(text="echo too_late", keys="Enter")
    assert "error" in send_result
    assert "error" in await instance.read()
