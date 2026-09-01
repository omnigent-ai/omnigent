"""E2E: macOS Cmd line-editing shortcuts in the session terminal.

On macOS the standard readline-style Cmd shortcuts in a session's terminal
did nothing useful:

- **Cmd+Backspace** should kill to the start of the line (Ctrl-U, ``0x15``),
- **Cmd+Left** should move the cursor to the start of the line (Ctrl-A,
  ``0x01``),
- **Cmd+Right** should move the cursor to the end of the line (Ctrl-E,
  ``0x05``),

because ``terminalKeyEventPayload`` (``web/src/components/blocks/
TerminalSession.ts``) used to map only Shift+Enter, and xterm.js has no
default binding for ``metaKey`` combos: Cmd+Left/Right produced no bytes at
all, and Cmd+Backspace fell through to a plain single-char DEL. The Option
(Alt) equivalents always worked because xterm encodes Alt-modified keys as
ESC-prefixed sequences.

Each test drives the real journey: open a shell from the workspace rail's
"+" menu, type a command line, press the Cmd shortcut, and finish the line so
that the *shell's own line editing* proves where the cursor was. The typed
line is arranged so the command only produces its file side-effect when the
shortcut performed its readline operation — e.g. for Cmd+Backspace the test
types a poison prefix, presses Cmd+Backspace (which must kill the whole
line), then types ``touch <file>``; on the buggy build the poison prefix
survives, the garbled line runs as an ``echo``/not-found, and the file never
appears. xterm renders to a canvas (stdout is not in the DOM), so the file
side-effect plus the bytes observed on the terminal-attach WebSocket are the
machine-checkable signals.

A readiness handshake (a ``touch`` that must land before the scenario) makes
sure the PTY's shell is accepting and executing input before any Cmd keys are
sent, so the assertions can only fail on the shortcut behavior itself.

All three tests use the function-scoped ``terminal_session`` fixture (the
``zsh``-declaring agent, runner-bound session); the launched shell is
actually ``bash --noprofile --norc``, whose default emacs bindings implement
Ctrl-A / Ctrl-E / Ctrl-U — exactly what the fix is expected to send.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Readline control bytes the Cmd shortcuts are expected to transmit.
CTRL_A = b"\x01"  # cursor to line start (Cmd+Left)
CTRL_E = b"\x05"  # cursor to line end   (Cmd+Right)
CTRL_U = b"\x15"  # kill to line start   (Cmd+Backspace)


def _open_new_shell(page: Page) -> None:
    """Create a shell via the tab strip's "+" -> Shell menu.

    Mirrors ``shells/test_new_shell.py``: opens the desktop Workspace rail
    and picks the single declared terminal type, which launches it directly.

    :param page: Playwright page already navigated to ``/c/{id}``.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _capture_attach_frames(page: Page) -> list[bytes]:
    """Record every payload the page sends on terminal-attach WebSockets.

    Registered before navigation so the attach socket — opened
    asynchronously once the shell's xterm mounts — cannot slip through.
    Keystrokes are sent as binary frames; text frames are kept too (encoded)
    so the diagnostic trail is complete.

    :param page: Playwright page, not yet navigated.
    :returns: A list that fills in as frames are sent.
    """
    frames: list[bytes] = []

    def _on_ws(ws: object) -> None:
        url = ws.url  # type: ignore[attr-defined]
        if "/attach" not in url:
            return

        def _on_frame(payload: str | bytes) -> None:
            frames.append(payload if isinstance(payload, bytes) else payload.encode())

        ws.on("framesent", _on_frame)  # type: ignore[attr-defined]

    page.on("websocket", _on_ws)
    return frames


def _connected_shell_textarea(page: Page) -> Locator:
    """Open a shell and return its focused xterm input textarea.

    Waits for the rail shell's xterm to report a live attach before
    returning — input typed before the WS attach opens is dropped. A shell
    occasionally sticks in ``connecting`` on a loaded box (the attach WS
    loses the race with the PTY spawn), so a stuck shell is closed and
    reopened rather than failing the test on harness weather.

    :param page: Playwright page already navigated to ``/c/{id}``.
    :returns: The shell's ``xterm-helper-textarea`` locator, focused.
    """
    rail = page.get_by_role("complementary", name="Workspace")
    last_error: AssertionError | None = None
    for _ in range(3):
        _open_new_shell(page)
        terminal_view = rail.get_by_test_id("terminal-view").last
        expect(terminal_view).to_be_visible(timeout=60_000)
        try:
            expect(terminal_view).to_have_attribute(
                "data-state", "connected", timeout=30_000
            )
        except AssertionError as exc:
            last_error = exc
            # Close the stuck tab (confirming the kill) and retry fresh.
            rail.get_by_role("button", name=re.compile(r"^Close ")).last.click()
            page.get_by_role("button", name=re.compile("Close")).last.click()
            page.wait_for_timeout(1_000)
            continue
        textarea = terminal_view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        return textarea
    raise AssertionError(f"shell never connected after 3 attempts: {last_error}")


def _wait_for_file(path: Path, timeout_s: float) -> bool:
    """Poll for *path* to exist.

    :param path: File the shell command is expected to create.
    :param timeout_s: How long to keep polling.
    :returns: True when the file appeared within the deadline.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.25)
    return False


def _await_shell_ready(page: Page, textarea: Locator, tmp_path: Path) -> None:
    """Prove the PTY's shell is accepting and executing typed input.

    The xterm attach connects before bash finishes starting, so keystrokes
    typed immediately can be swallowed. Types a ``touch`` and waits for its
    file, retyping a few times, so every later assertion can only fail on
    the Cmd-shortcut behavior itself.

    :param page: Playwright page with the shell focused.
    :param textarea: The shell's xterm input textarea.
    :param tmp_path: Pytest temp dir shared with the runner-local shell.
    """
    ready = tmp_path / "shell_ready.txt"
    for _ in range(4):
        textarea.focus()
        # Ctrl+C aborts any partially-delivered previous attempt so retries
        # always start from a clean prompt line.
        page.keyboard.press("Control+c")
        page.keyboard.type(f"touch {ready}")
        page.keyboard.press("Enter")
        if _wait_for_file(ready, timeout_s=8.0):
            return
    raise AssertionError(
        "shell never executed the readiness command; cannot exercise Cmd shortcuts"
    )


def _run_line_and_expect_file(
    page: Page, marker: Path, frames: list[bytes], expected_byte: bytes, shortcut: str
) -> None:
    """Submit the composed line and assert the shortcut's observable outcome.

    :param page: Playwright page with the shell focused.
    :param marker: File the line creates only if the shortcut worked.
    :param frames: Captured attach-WS payloads (diagnostic + wire check).
    :param expected_byte: Control byte the shortcut must transmit.
    :param shortcut: Human-readable shortcut name for the failure message.
    """
    page.keyboard.press("Enter")
    file_created = _wait_for_file(marker, timeout_s=15.0)
    sent = b"".join(frames)
    assert expected_byte in sent and file_created, (
        f"{shortcut} did not perform its readline line edit: expected the "
        f"terminal to transmit {expected_byte!r} and the edited command to "
        f"create {marker.name} (created={file_created}); bytes sent on the "
        f"attach WebSocket: {sent!r}"
    )


def test_cmd_backspace_kills_to_line_start(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Cmd+Backspace deletes to the start of the line (Ctrl-U).

    Journey: open a shell → type a poison prefix → press Cmd+Backspace →
    type ``touch <marker>`` → Enter. Only if Cmd+Backspace killed the whole
    line does the clean ``touch`` run and the marker appear; on the buggy
    build the prefix survives (xterm sends at most a single-char DEL) and
    the garbled line creates nothing.
    """
    base_url, session_id = terminal_session
    frames = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    textarea = _connected_shell_textarea(page)
    _await_shell_ready(page, textarea, tmp_path)

    marker = tmp_path / "cmd_backspace.ok"
    page.keyboard.type("echo poison_prefix_line_kill")
    page.keyboard.press("Meta+Backspace")
    page.keyboard.type(f"touch {marker}")
    _run_line_and_expect_file(page, marker, frames, CTRL_U, "Cmd+Backspace")


def test_cmd_left_moves_cursor_to_line_start(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Cmd+Left moves the cursor to the start of the line (Ctrl-A).

    Journey: open a shell → type the tail of a ``touch`` command → press
    Cmd+Left → type the head → Enter. Only if Cmd+Left jumped to the line
    start does the head land in front of the tail and the marker appear; on
    the buggy build no bytes are sent, the cursor stays put, and the
    reversed line fails.
    """
    base_url, session_id = terminal_session
    frames = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    textarea = _connected_shell_textarea(page)
    _await_shell_ready(page, textarea, tmp_path)

    marker = tmp_path / "cmd_left.ok"
    page.keyboard.type(f"{marker}")
    page.keyboard.press("Meta+ArrowLeft")
    page.keyboard.type("touch ")
    _run_line_and_expect_file(page, marker, frames, CTRL_A, "Cmd+Left")


def test_cmd_right_moves_cursor_to_line_end(
    page: Page, terminal_session: tuple[str, str], tmp_path: Path
) -> None:
    """Cmd+Right moves the cursor to the end of the line (Ctrl-E).

    Journey: open a shell → type a ``touch`` command missing its suffix →
    Ctrl+A (a binding that works today) to jump to the line start → press
    Cmd+Right → type the suffix → Enter. Only if Cmd+Right returned the
    cursor to the line end does the suffix complete the path and the marker
    appear; on the buggy build no bytes are sent, the suffix lands at the
    line start, and the reversed line fails.
    """
    base_url, session_id = terminal_session
    frames = _capture_attach_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    textarea = _connected_shell_textarea(page)
    _await_shell_ready(page, textarea, tmp_path)

    marker = tmp_path / "cmd_right.ok"
    stem = str(marker)[: -len(".ok")]
    page.keyboard.type(f"touch {stem}")
    page.keyboard.press("Control+a")
    page.keyboard.press("Meta+ArrowRight")
    page.keyboard.type(".ok")
    _run_line_and_expect_file(page, marker, frames, CTRL_E, "Cmd+Right")
