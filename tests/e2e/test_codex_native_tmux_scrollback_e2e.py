"""E2E regression: native attach to an Omnigent-managed tmux terminal cannot
reach the formatted scrollback above the viewport.

The reported journey: run ``omnigent codex`` from a native terminal, produce
more conversation output than fits the viewport, then scroll up with the mouse
wheel or press Page Up. Nothing moves — the view stays pinned at the bottom and
earlier formatted output is unreachable, even though tmux's history buffer
holds it (an affected session reported ``history=288`` with
``history-limit 100000``).

Why: the managed tmux server disables every entry point into tmux copy mode —
``mouse off`` (so wheel events never trigger ``WheelUpPane``), ``prefix None``
+ ``prefix2 None`` + an emptied prefix table (so ``prefix [`` is gone), and no
root-table binding maps Page Up to ``copy-mode`` (so the key passes through to
the inner CLI, which ignores it). The user's own ``~/.tmux.conf`` cannot
restore any of this because the native client attaches with ``-f /dev/null``.

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

Before a fix: no gesture enters copy mode (``pane_in_mode`` stays ``0``,
``scroll_position`` stays empty), the earliest visible line is still the last
screenful, and this test FAILS.
After a fix (a root-table Page Up binding into copy-mode, tmux mouse support,
or any other scrollback entry point): at least one gesture scrolls the view
into history and it PASSES.

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


async def test_native_attach_can_scroll_back_through_managed_tmux_history(
    tmp_path: Path,
) -> None:
    """A user attached to the managed tmux can scroll back to earlier output.

    Regression guard: with the managed lockdown options (``mouse off``,
    ``prefix None``, emptied prefix table, ``-f /dev/null`` attach) neither the
    mouse wheel nor Page Up reaches tmux's history, so the conversation above
    the viewport is unreachable from the native terminal.
    """
    reg = TerminalRegistry()
    child: pexpect.spawn | None = None
    try:
        # 1. Launch the managed terminal exactly as the codex-native runner
        #    does, with a pane command that overflows the viewport the way a
        #    Codex conversation does, then stays alive like an idle TUI.
        spec = TerminalEnvSpec(
            command="bash",
            args=[
                "-c",
                f'for i in $(seq 1 {FILLER_LINES}); do echo "CODEX OUTPUT LINE $i"; done; '
                "exec sleep 600",
            ],
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(tmp_path),
                sandbox=OSEnvSandboxSpec(type="none"),
            ),
        )
        instance = await reg.launch("conv_scrollback", "codex", "s1", spec)
        socket_path = str(instance.socket_path)

        # Wait for the pane to have pushed real content into tmux history —
        # the precondition the bug report confirmed (``history=288``).
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

        # 2. Attach a real client PTY with the exact production attach command
        #    from ``_attach_direct_tmux``: tmux -S <sock> -f /dev/null attach
        #    -t main, with TMUX stripped so an outer user tmux doesn't nest.
        env = dict(os.environ)
        env.pop("TMUX", None)
        # The user's terminal always advertises a capable TERM; a bare CI
        # shell may carry TERM=dumb, which tmux refuses to attach to.
        env["TERM"] = "xterm-256color"
        child = pexpect.spawn(
            "tmux",
            ["-S", socket_path, "-f", os.devnull, "attach", "-t", "main"],
            env=env,
            dimensions=(24, 80),
            timeout=10,
        )
        # Let the attach settle and the client render the bottom of the pane.
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
            f"({'; '.join(results)}). The managed options disable every "
            "copy-mode entry point (mouse off, prefix None, emptied prefix "
            "table, no root-table Page Up binding), and -f /dev/null blocks "
            "the user's tmux.conf from restoring one, so earlier formatted "
            "output above the viewport is unreachable."
        )
    finally:
        if child is not None:
            child.close(force=True)
        await reg.shutdown()
