"""
PTY regression tests for TerminalHost prompt expansion.

These tests exercise the real prompt-toolkit renderer instead of
calling height helpers directly. The reported regression was visual:
long prompts stayed in a one-row horizontally-scrolled viewport even
though the prompt buffer contained the full text.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pexpect
import pyte

_TERM = "xterm-256color"
_ROWS = 24
_COLS = 80
_BOOT_TIMEOUT = 10.0
_RENDER_TIMEOUT = 3.0
_SHUTDOWN_TIMEOUT = 3.0


def _spawn_prompt_driver(
    history_path: Path, *, rows: int = _ROWS, cols: int = _COLS
) -> pexpect.spawn:
    """
    Spawn a minimal TerminalHost under a PTY.

    :param history_path: Prompt history file path, e.g.
        ``Path("/tmp/history")``.
    :returns: A live pexpect child running the host.
    """
    code = f"""
import asyncio
from omnigent_ui_sdk import TerminalHost

async def handler(text, files):
    await asyncio.sleep(100)

async def main():
    host = TerminalHost(model_name="test", history_file={str(history_path)!r})
    async with host:
        await host.run(handler)

asyncio.run(main())
"""
    return pexpect.spawn(
        sys.executable,
        ["-c", code],
        env={
            **os.environ,
            "TERM": _TERM,
            "LINES": str(rows),
            "COLUMNS": str(cols),
        },
        encoding="utf-8",
        timeout=_BOOT_TIMEOUT,
        dimensions=(rows, cols),
    )


def _drain_screen(
    child: pexpect.spawn,
    screen: pyte.Screen,
    stream: pyte.Stream,
    *,
    seconds: float,
) -> str:
    """
    Drain PTY output into *screen* for up to *seconds*.

    :param child: Live pexpect child.
    :param screen: pyte screen updated by *stream*.
    :param stream: pyte stream connected to *screen*.
    :param seconds: Maximum wall-clock seconds to drain, e.g. ``0.5``.
    :returns: Current rendered display joined by newlines.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            chunk = child.read_nonblocking(size=4096, timeout=0.05)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        stream.feed(chunk)
    return "\n".join(screen.display)


def _wait_for_prompt(
    child: pexpect.spawn,
    screen: pyte.Screen,
    stream: pyte.Stream,
) -> None:
    """
    Wait until the TerminalHost prompt marker is rendered.

    :param child: Live pexpect child.
    :param screen: pyte screen updated by *stream*.
    :param stream: pyte stream connected to *screen*.
    :raises pexpect.TIMEOUT: If the prompt marker never appears.
    """
    deadline = time.monotonic() + _BOOT_TIMEOUT
    rendered = ""
    while time.monotonic() < deadline:
        rendered = _drain_screen(child, screen, stream, seconds=0.2)
        if "❯" in rendered:
            return
    raise pexpect.TIMEOUT(f"TerminalHost prompt did not render. Display:\n{rendered}")


def test_soft_wrapped_prompt_expands_to_multiple_visible_rows(tmp_path: Path) -> None:
    """
    A long single-line prompt must soft-wrap visibly.

    What this proves: the composer height cap counts visual rows,
    not only hard-newline document lines. If the cap regresses to
    ``Document.line_count``, prompt-toolkit keeps the input window
    at one row and horizontally scrolls to the tail; ``word00`` and
    the prompt marker disappear from the rendered display.

    :param tmp_path: Temporary directory for the prompt history file.
    """
    child = _spawn_prompt_driver(tmp_path / "history")
    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.Stream(screen)
    try:
        _wait_for_prompt(child, screen, stream)
        text = " ".join(f"word{i:02d}" for i in range(24))
        child.send(text)
        rendered = _drain_screen(child, screen, stream, seconds=_RENDER_TIMEOUT)
    finally:
        if not child.closed:
            child.sendcontrol("d")
            try:
                child.expect(pexpect.EOF, timeout=_SHUTDOWN_TIMEOUT)
            except pexpect.TIMEOUT:
                child.close(force=True)

    input_rows = [row.rstrip() for row in screen.display if "word" in row or "❯" in row]
    assert len(input_rows) >= 2, (
        f"Expected the long prompt to occupy at least two visible rows; "
        f"got rows={input_rows!r}. A one-row result means the composer "
        f"height was capped to hard-newline count instead of visual wrap count.\n"
        f"Rendered display:\n{rendered}"
    )
    assert any("❯" in row and "word00" in row for row in input_rows), (
        f"Expected the first wrapped row to keep the prompt marker and word00. "
        f"If missing, prompt-toolkit horizontally scrolled to the tail instead "
        f"of expanding. Rows={input_rows!r}\nRendered display:\n{rendered}"
    )
    assert "word23" in rendered, (
        f"Expected the tail of the typed prompt to remain visible after wrapping. "
        f"Rendered display:\n{rendered}"
    )


def test_prompt_expands_to_screen_height_minus_ui_rows(tmp_path: Path) -> None:
    """
    A very long prompt should use screen rows not occupied by host UI/margin.

    This guards against the legacy fixed eight-row composer cap. With a
    14-row terminal, two TerminalHost UI rows (separator + status), and a
    small bleed margin, the typed prompt should still occupy more than eight
    visible rows before it starts scrolling internally.

    :param tmp_path: Temporary directory for the prompt history file.
    """
    rows = 14
    cols = 40
    child = _spawn_prompt_driver(tmp_path / "history", rows=rows, cols=cols)
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    try:
        _wait_for_prompt(child, screen, stream)
        text = "\n".join(f"line{i:02d}" for i in range(rows))
        child.send(text)
        rendered = _drain_screen(child, screen, stream, seconds=_RENDER_TIMEOUT)
    finally:
        if not child.closed:
            child.sendcontrol("d")
            try:
                child.expect(pexpect.EOF, timeout=_SHUTDOWN_TIMEOUT)
            except pexpect.TIMEOUT:
                child.close(force=True)

    input_rows = [row.rstrip() for row in screen.display if "line" in row or "❯" in row]
    assert len(input_rows) > 8, (
        f"Expected the composer to grow beyond the old fixed eight-row cap; "
        f"got rows={input_rows!r}.\nRendered display:\n{rendered}"
    )
    assert len(input_rows) <= rows - 2 - 3, (
        f"Expected the composer to leave room for separator/status UI rows and bleed margin; "
        f"got rows={input_rows!r}.\nRendered display:\n{rendered}"
    )
    assert any(row.startswith("─") for row in screen.display), (
        f"Expected the separator row to remain visible below the expanded composer. "
        f"Rendered display:\n{rendered}"
    )
