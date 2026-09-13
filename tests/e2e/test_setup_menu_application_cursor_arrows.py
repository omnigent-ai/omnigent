"""
Regression test: arrow keys don't move the selection in ``omni setup``
menus when the terminal is in application-cursor-keys mode.

Terminals in application-cursor-keys mode (DECCKM — e.g. fish 4.x under
Ghostty leaves cursor keys in application mode) send arrow presses as SS3
sequences (``ESC O A`` / ``ESC O B``) instead of CSI sequences
(``ESC [ A`` / ``ESC [ B``). The setup menu's raw-termios reader in
``omnigent.onboarding.interactive.select`` parses only the CSI form, so
in such terminals Up/Down do nothing while single-byte keys (j/k, Enter,
Esc) keep working — exactly the reported symptom.

This test spawns the real ``omni setup`` flow under a pseudo-TTY (the same
raw-termios code path a human types into), verifies CSI arrows move the
pointer (control: the harness itself works), then asserts SS3 arrows move
the pointer too. The SS3 assertion FAILS on the buggy build and passes
once ``select()`` accepts application-cursor-key arrows.

Usage::

    python -m pytest tests/e2e/test_setup_menu_application_cursor_arrows.py -v --timeout=180
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The accent pointer select() prefixes the highlighted row with.
_POINTER = "❯"

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so menu
# rows can be matched as plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _spawn_setup(tmp_path: Path) -> pexpect.spawn:
    """
    Spawn ``omni setup`` under a PTY with a fresh HOME.

    A fresh HOME gives a clean, deterministic first-run menu (no persisted
    providers) and keeps the test from touching the real user config.
    ``NO_COLOR`` keeps the frames parseable as plain text; 40 rows keeps
    the landing lockup plus the full harness menu on one screen.

    :param tmp_path: Per-test temp dir used as the fake ``$HOME``.
    :returns: The spawned pexpect child, waiting on the harness overview.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": str(_REPO_ROOT),
        "NO_COLOR": "1",
        # The reported environment; irrelevant to the PTY bytes we send but
        # keeps the spawned CLI from tripping on an exotic outer TERM.
        "TERM": "xterm",
    }
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(40, 120),
        timeout=90,
    )
    # The level-1 harness overview title. Rendering it means select() is
    # live in its raw-termios read loop.
    child.expect(re.compile(rb"Configure harnesses"), timeout=90)
    return child


def _read_quiet(child: pexpect.spawn, settle: float = 1.5) -> bytes:
    """
    Drain PTY output until *settle* seconds elapse.

    select() redraws the menu frame only when the selection moves, so an
    ignored keypress produces no output at all — the caller distinguishes
    "moved" from "ignored" by whether a new pointer row appears.

    :param child: The spawned ``omni setup`` process.
    :param settle: How long to keep draining, in seconds.
    :returns: The raw bytes read (possibly empty).
    """
    buf = b""
    deadline = time.time() + settle
    while time.time() < deadline:
        try:
            buf += child.read_nonblocking(4096, timeout=0.3)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
    return buf


def _pointer_rows(buf: bytes) -> list[str]:
    """
    Extract the menu rows carrying the selection pointer from raw output.

    :param buf: Raw PTY bytes (one or more redraw frames).
    :returns: The pointer-row texts in frame order, stripped of ANSI codes.
    """
    text = _ANSI_RE.sub(b"", buf).decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if _POINTER in line]


def _press_and_get_pointer(child: pexpect.spawn, seq: bytes) -> list[str]:
    """
    Send a key sequence and return any pointer rows from the redraw.

    :param child: The spawned ``omni setup`` process.
    :param seq: Raw bytes to send (e.g. ``b"\\x1b[B"`` for CSI Down).
    :returns: Pointer rows rendered in response; empty if the key was
        ignored (no redraw).
    """
    child.send(seq)
    return _pointer_rows(_read_quiet(child))


@pytest.mark.timeout(180)
def test_setup_menu_arrows_move_selection_in_application_cursor_mode(
    tmp_path: Path,
) -> None:
    """
    Up/Down must move the ``omni setup`` selection for BOTH arrow
    encodings a terminal can send.

    CSI arrows (``ESC [ B``) are the control — they already work, proving
    the PTY harness and menu are functional. SS3 arrows (``ESC O B`` /
    ``ESC O A``), sent by terminals in application-cursor-keys mode
    (DECCKM), are the bug: the menu ignores them, so the selection never
    moves — the reported symptom (arrows dead, j/k fine).
    """
    child = _spawn_setup(tmp_path)
    try:
        # Let the first frame finish rendering.
        _read_quiet(child, settle=2.0)

        # Control: CSI Down must move the pointer (menu + harness sanity).
        csi_rows = _press_and_get_pointer(child, b"\x1b[B")
        assert csi_rows, (
            "control failed: CSI Down (ESC [ B) did not move the setup menu "
            "selection — the PTY harness or menu itself is broken, so the "
            "SS3 assertion below would be meaningless"
        )
        csi_position = csi_rows[-1]

        # Bug: SS3 Down (application-cursor-keys mode) must also move it.
        ss3_down_rows = _press_and_get_pointer(child, b"\x1bOB")
        assert ss3_down_rows and ss3_down_rows[-1] != csi_position, (
            "SS3 Down (ESC O B — what a terminal in "
            "application-cursor-keys/DECCKM mode, e.g. fish 4.x under "
            "Ghostty, sends for the Down arrow) did not move the "
            "`omni setup` menu selection; select() in "
            "omnigent/onboarding/interactive.py parses only the CSI form "
            "(ESC [ B) and silently drops SS3 arrows"
        )

        # And back up with SS3 Up — the selection must return.
        ss3_up_rows = _press_and_get_pointer(child, b"\x1bOA")
        assert ss3_up_rows and ss3_up_rows[-1] == csi_position, (
            "SS3 Up (ESC O A, application-cursor-keys mode) did "
            "not move the `omni setup` menu selection back up"
        )
    finally:
        with contextlib.suppress(Exception):
            child.send(b"\x1b")  # Esc — exit the menu cleanly.
            time.sleep(0.5)
        with contextlib.suppress(Exception):
            child.close(force=True)
