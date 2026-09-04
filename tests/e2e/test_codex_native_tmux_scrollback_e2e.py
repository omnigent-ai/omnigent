"""E2E regression: a native attach to an Omnigent-managed tmux terminal must be
able to reach the formatted scrollback above the viewport.

The reported journey: run ``omnigent codex`` from a native terminal, produce
more conversation output than fits the viewport, then scroll up with the mouse
wheel or press Page Up. Nothing moved — the view stayed pinned at the bottom and
earlier formatted output was unreachable, even though tmux's history buffer
held it (an affected session reported ``history=288`` with
``history-limit 100000``).

Why: the managed tmux server had disabled every entry point into tmux copy
mode — ``mouse off`` (so wheel events never reached ``WheelUpPane``),
``prefix None`` + ``prefix2 None`` + an emptied prefix table (so ``prefix [``
is gone), and no root-table binding mapped Page Up to ``copy-mode`` (so the key
passed through to the inner CLI, which ignores it: Codex handles neither the
wheel nor Page Up, since it renders inline and leaves scrolling to the host
terminal). The user's own ``~/.tmux.conf`` cannot restore any of this because
the native client attaches with ``-f /dev/null``.

Both routes are now open: ``mouse on`` for the wheel and a root-table Page Up
binding for the keyboard. The wheel must still be *forwarded* to a pane that
tracks the mouse, so a full-screen TUI keeps its own wheel handling.

This test drives the real product path end-to-end, with no LLM and no codex
binary (codex-native needs an interactive OAuth login, so the inner CLI is a
stand-in that fills the pane the way a Codex conversation does — the tmux
configuration and the attach command are the production ones):

1. Launch a managed terminal through the production ``TerminalRegistry`` (the
   same path the codex-native runner uses), with a pane command that emits far
   more lines than the 80x24 viewport.
2. Attach a real client PTY with the exact command ``omnigent codex`` execs in
   ``omnigent.codex_native._attach_direct_tmux``:
   ``tmux -S <socket> -f /dev/null attach -t main`` (with ``TMUX`` stripped).
3. Deliver the user's scroll gestures through the attached client: Page Up,
   then SGR mouse wheel-up.
4. Assert some gesture reached the scrollback — the pane enters a scrolled
   (copy-mode) state with a non-zero scroll offset.

Without a scrollback route no gesture enters copy mode (``pane_in_mode`` stays
``0``, ``scroll_position`` stays empty), the earliest visible line is still the
last screenful, and these tests FAIL.

Runs with only ``tmux`` and ``pexpect``::

    pytest tests/e2e/test_codex_native_tmux_scrollback_e2e.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pexpect
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.terminals import TerminalRegistry

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

# The pane emits this many numbered lines, far beyond the 80x24 launch
# viewport, so a healthy scrollback gesture has plenty of history to reach.
FILLER_LINES = 200

# Raw bytes a user's terminal sends for the reported gestures.
PAGE_UP = "\x1b[5~"
# SGR-encoded mouse wheel-up at column 40, row 12 (what a wheel notch sends
# when mouse reporting is active; harmless pass-through bytes when it is not).
WHEEL_UP = "\x1b[<64;40;12M"


def _tmux_out(socket_path: str, *args: str) -> str:
    """Run a tmux control command against the managed server's socket.

    :param socket_path: The managed terminal's private socket path.
    :param args: Tmux command and arguments, e.g. ``("display-message", ...)``.
    :returns: Stripped stdout.
    """
    return subprocess.run(
        ["tmux", "-S", socket_path, *args],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    ).stdout.strip()


def _scrolled_state(socket_path: str) -> tuple[bool, str]:
    """Report whether the attached view has scrolled into history.

    :param socket_path: The managed terminal's private socket path.
    :returns: ``(scrolled, detail)`` — ``scrolled`` is ``True`` when the pane
        is in copy mode with a non-zero scroll offset (the only way a tmux
        pane shows earlier history to an attached client); ``detail`` carries
        the raw fields for the failure message.
    """
    in_mode = _tmux_out(socket_path, "display-message", "-p", "-t", "main", "#{pane_in_mode}")
    scroll_pos = _tmux_out(
        socket_path, "display-message", "-p", "-t", "main", "#{scroll_position}"
    )
    detail = f"pane_in_mode={in_mode!r} scroll_position={scroll_pos!r}"
    scrolled = in_mode == "1" and scroll_pos.isdigit() and int(scroll_pos) > 0
    return scrolled, detail


def _filler_pane_spec(cwd: Path) -> TerminalEnvSpec:
    """Build a pane spec that overflows the viewport, then idles like a TUI.

    :param cwd: Working directory for the pane process.
    :returns: A managed-terminal spec emitting :data:`FILLER_LINES` lines.
    """
    return TerminalEnvSpec(
        command="bash",
        args=[
            "-c",
            f'for i in $(seq 1 {FILLER_LINES}); do echo "CODEX OUTPUT LINE $i"; done; '
            "exec sleep 600",
        ],
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


def _await_filled_history(socket_path: str) -> int:
    """Wait until the pane has pushed real content into tmux history.

    The precondition the bug report confirmed (``history=288``): without it a
    scrollback assertion passes vacuously because there is nothing above the
    viewport to reach.

    :param socket_path: The managed terminal's private socket path.
    :returns: The observed ``history_size``.
    """
    history_size = 0
    for _ in range(120):
        raw = _tmux_out(socket_path, "display-message", "-p", "-t", "main", "#{history_size}")
        history_size = int(raw) if raw.isdigit() else 0
        if history_size >= FILLER_LINES // 2:
            break
        time.sleep(0.25)
    assert history_size >= FILLER_LINES // 2, (
        f"pane never filled tmux history (history_size={history_size}); "
        "the scrollback-gesture assertion below would be vacuous"
    )
    return history_size


def _attach_native_client(socket_path: str) -> pexpect.spawn:
    """Attach a real client PTY with the production native attach command.

    Mirrors ``omnigent.codex_native._attach_direct_tmux``: ``tmux -S <sock>
    -f /dev/null attach -t main`` with ``TMUX`` stripped so an outer user tmux
    does not nest. ``TERM`` is forced because a bare CI shell may carry
    ``TERM=dumb``, which tmux refuses to attach to.

    :param socket_path: The managed terminal's private socket path.
    :returns: The spawned client, for the caller to close.
    """
    env = dict(os.environ)
    env.pop("TMUX", None)
    env["TERM"] = "xterm-256color"
    return pexpect.spawn(
        "tmux",
        ["-S", socket_path, "-f", os.devnull, "attach", "-t", "main"],
        env=env,
        dimensions=(24, 80),
        timeout=10,
    )


async def test_native_attach_can_scroll_back_through_managed_tmux_history(
    tmp_path: Path,
) -> None:
    """A user attached to the managed tmux can scroll back to earlier output.

    Regression guard: the managed lockdown (``prefix None``, emptied prefix
    table, ``-f /dev/null`` attach) leaves no prefix route into copy mode, so
    at least one of the user's two gestures — the wheel or Page Up — has to
    reach tmux's history or the conversation above the viewport is unreachable
    from the native terminal.

    :param tmp_path: Working directory for the managed terminal.
    """
    reg = TerminalRegistry()
    child: pexpect.spawn | None = None
    try:
        # 1. Launch the managed terminal exactly as the codex-native runner
        #    does, with a pane that overflows the viewport the way a Codex
        #    conversation does, then stays alive like an idle TUI.
        instance = await reg.launch("conv_scrollback", "codex", "s1", _filler_pane_spec(tmp_path))
        socket_path = str(instance.socket_path)
        _await_filled_history(socket_path)

        # 2. Attach a real client PTY with the production attach command, then
        #    let it render the bottom of the pane.
        child = _attach_native_client(socket_path)
        child.expect("CODEX OUTPUT LINE", timeout=10)
        time.sleep(1.0)

        # 3. The user's gestures, delivered through the attached client.
        gestures: list[tuple[str, str]] = [
            ("Page Up", PAGE_UP),
            ("mouse wheel-up", WHEEL_UP * 4),
        ]
        results: list[str] = []
        scrolled = False
        for name, payload in gestures:
            child.send(payload)
            time.sleep(1.0)
            ok, detail = _scrolled_state(socket_path)
            results.append(f"{name}: {detail}")
            if ok:
                scrolled = True
                break

        # 4. The bug: no gesture ever scrolls the view into history.
        assert scrolled, (
            "native attach cannot reach the managed tmux scrollback: neither "
            "Page Up nor the mouse wheel moved the view off the bottom "
            f"({'; '.join(results)}). The managed options must keep a "
            "copy-mode entry point (mouse on for the wheel, a root-table Page "
            "Up binding for the keyboard) — -f /dev/null blocks the user's "
            "tmux.conf from restoring one, so earlier formatted output above "
            "the viewport is otherwise unreachable."
        )
    finally:
        if child is not None:
            child.close(force=True)
        await reg.shutdown()


async def test_native_attach_wheel_alone_reaches_scrollback(tmp_path: Path) -> None:
    """The wheel on its own scrolls a native attach into managed tmux history.

    The sibling test stops at the first gesture that works, so a Page Up
    binding alone satisfies it and the wheel stays unguarded. This delivers
    only wheel-up: with ``mouse off`` the report bytes pass through to the
    inline pane program, which ignores them and nothing scrolls.

    :param tmp_path: Working directory for the managed terminal.
    """
    reg = TerminalRegistry()
    child: pexpect.spawn | None = None
    try:
        instance = await reg.launch("conv_wheel", "codex", "s1", _filler_pane_spec(tmp_path))
        socket_path = str(instance.socket_path)
        _await_filled_history(socket_path)

        child = _attach_native_client(socket_path)
        child.expect("CODEX OUTPUT LINE", timeout=10)
        time.sleep(1.0)

        child.send(WHEEL_UP * 4)
        time.sleep(1.0)

        scrolled, detail = _scrolled_state(socket_path)
        assert scrolled, (
            "the mouse wheel did not reach managed tmux scrollback from a "
            f"native attach ({detail}). Without tmux mouse mode the wheel "
            "reports are forwarded to the inline pane program, which ignores "
            "them, so the wheel is dead for the whole conversation."
        )
    finally:
        if child is not None:
            child.close(force=True)
        await reg.shutdown()


async def test_native_attach_wheel_reaches_a_mouse_tracking_pane(
    tmp_path: Path,
) -> None:
    """A pane that tracks the mouse keeps the wheel instead of losing it to tmux.

    Enabling tmux mouse mode must not hijack the wheel from a full-screen TUI
    that does its own scrolling (Claude Code, an editor): tmux's
    ``WheelUpPane`` binding forwards the report when ``mouse_any_flag`` is set
    and only takes the pane into copy mode when it is not. Guards the cost side
    of ``mouse on``.

    :param tmp_path: Working directory for the managed terminal.
    """
    reg = TerminalRegistry()
    child: pexpect.spawn | None = None
    try:
        # DECSET 1002 + 1006: button/drag tracking with SGR reports, the modes
        # a TUI that scrolls on the wheel requests at startup.
        spec = TerminalEnvSpec(
            command="bash",
            args=["-c", 'printf "\\033[?1002h\\033[?1006h"; echo TRACKING; exec sleep 600'],
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(tmp_path),
                sandbox=OSEnvSandboxSpec(type="none"),
            ),
        )
        instance = await reg.launch("conv_tracking", "codex", "s1", spec)
        socket_path = str(instance.socket_path)

        tracking = ""
        for _ in range(40):
            tracking = _tmux_out(
                socket_path, "display-message", "-p", "-t", "main", "#{mouse_any_flag}"
            )
            if tracking == "1":
                break
            time.sleep(0.25)
        assert tracking == "1", (
            f"pane never entered mouse tracking (mouse_any_flag={tracking!r}); "
            "the forwarding assertion below would be vacuous"
        )

        child = _attach_native_client(socket_path)
        child.expect("TRACKING", timeout=10)
        time.sleep(1.0)

        child.send(WHEEL_UP * 4)
        time.sleep(1.0)

        in_mode = _tmux_out(socket_path, "display-message", "-p", "-t", "main", "#{pane_in_mode}")
        assert in_mode == "0", (
            "tmux took a mouse-tracking pane into copy mode on wheel-up "
            f"(pane_in_mode={in_mode!r}). The wheel belongs to the pane "
            "program whenever it tracks the mouse, or a full-screen TUI's own "
            "scrolling stops working under a managed terminal."
        )
    finally:
        if child is not None:
            child.close(force=True)
        await reg.shutdown()
