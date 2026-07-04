"""Shared tmux control-mode (``tmux -C``) ↔ WebSocket bridge.

Alternative transport to :mod:`omnigent.terminals.ws_bridge`. Where the PTY
bridge forks a full ``tmux attach`` client and streams the rendered screen,
this bridge attaches a *control-mode* client and consumes tmux's line protocol:

- ``%output <pane-id> <octal-escaped-bytes>`` — the raw bytes the program in a
  pane just produced, forwarded to the browser xterm.js as binary frames. The
  browser terminal therefore owns the character grid, scrollback, and text
  selection (tmux's own status line / copy-mode chrome is never streamed to a
  control client), which is what gives native scrolling and copy in the web UI.
- ``%begin <t> <n> <flags>`` … ``%end``/``%error <t> <n> <flags>`` — bracketed
  reply blocks for commands the bridge sends, correlated by command number.
- ``%exit`` / ``%window-close`` / ``%layout-change`` — lifecycle + structure.

Design notes learned from the protocol (see ``control_bridge`` spike):

- Attach with ``-C`` (NOT ``-CC``): ``-CC`` requires the parent be a real
  terminal and the client exits immediately when stdin/stdout are pipes. ``-C``
  gives the same protocol with echo already off, which is what we want for a
  programmatic consumer reading/writing pipes.
- A control client only receives ``%output`` produced *after* it attaches, so
  the bridge seeds the browser terminal with ``capture-pane -e -p`` (escapes
  preserved) once on connect, then streams subsequent ``%output``.
- Browser input bytes are injected with ``send-keys -H <hh> <hh> ...``
  (space-separated hex, one token per byte). Feeding raw ESC/control bytes into
  a ``send-keys -l`` command line corrupts the line-based command parser and
  the client exits; the hex channel is byte-exact for ESC sequences, control
  chars, and UTF-8 multibyte alike.

The browser-facing wire protocol is identical to the PTY bridge (binary frames
out = raw pane bytes; text frames in = JSON ``{"type":"resize",...}``; binary
frames in = input bytes), so the two transports are interchangeable behind the
same ``/attach`` WebSocket and a client cannot tell which one served it.

Known limitation vs the PTY bridge: tmux's own overlays (``display-popup``,
copy-mode, status line) are NOT delivered to a control-mode client, so the
native cost-approval popup (:mod:`omnigent.native_cost_popup`) does not render
in a control-mode browser terminal. That popup is a secondary convenience for
users working in a real native TTY; the web ApprovalCard (SSE-driven) remains
the primary approval surface and is unaffected. The harnesses' own input,
paste, and readiness logic run tmux commands directly against the socket
(``send-keys``/``load-buffer``/``capture-pane``) and are independent of the
attach transport, so they behave identically under either bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from fastapi import WebSocket, WebSocketDisconnect

# Re-export the PTY bridge's application close codes so both transports speak
# the same dialect to the frontend reconnect logic (see ws_bridge for the
# authoritative definitions and the client-side classification).
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_INTERNAL_ERROR,
    WS_CLOSE_TERMINAL_DETACHED,
    WS_CLOSE_TERMINAL_NOT_FOUND,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "bridge_tmux_control_to_websocket",
    "unescape_control_output",
]

# tmux octal-escapes bytes < 0x20 and backslash in %output values as ``\ooo``.
_OCTAL_ESCAPE_RE: Final = re.compile(rb"\\([0-7]{3})")

# ``capture-pane -p`` joins rows with a bare LF. Match an LF not already
# preceded by CR so the CRLF rewrite is idempotent (a future tmux emitting
# CRLF is left untouched).
_CAPTURE_ROW_SEP_RE: Final = re.compile(rb"(?<!\r)\n")

# tmux's client→server protocol rejects a single command larger than its 16KB
# imsg cap. ``send-keys -H`` expands each input byte to 3 chars ("xx "), so cap
# the bytes per send-keys invocation well under the limit. 1024 bytes ≈ 3KB
# packed; tmux applies successive invocations in order so the pane sees one
# contiguous stream. Matches terminal.py's literal-send chunking rationale.
_SEND_KEYS_HEX_BYTES_PER_CALL: Final[int] = 1024

# StreamReader line-length cap for the control client's stdout. In practice
# tmux 3.6b chunks control-mode ``%output`` into small lines (a few KB even for
# a 500 KB burst of octal-escaped control bytes on a huge pane), so lines stay
# far under asyncio's 64 KiB default. But that chunk size is a tmux internal,
# not a documented guarantee — a different version/build emitting one line past
# 64 KiB would make ``readline()`` raise ``LimitOverrunError`` and crash the
# reader (a disconnect/reconnect loop under burst output the PTY bridge handles
# fine). Raising the cap to 16 MiB removes that failure mode for negligible
# cost; it is a safety ceiling, not a steady buffer.
_CONTROL_STDOUT_LINE_LIMIT: Final[int] = 16 * 1024 * 1024


def unescape_control_output(value: bytes) -> bytes:
    """Un-escape a ``%output`` value back to raw pane bytes.

    tmux escapes bytes below ASCII space and the backslash itself as a
    three-digit octal sequence ``\\ooo``; every other byte passes through
    verbatim. The decoded result is a raw terminal byte stream (not guaranteed
    valid UTF-8) suitable to write straight into xterm.js.

    :param value: The escaped bytes following ``%output <pane-id> `` on one
        protocol line, e.g. ``rb"\\033[31mRED\\033[0m\\015\\012"``.
    :returns: The raw bytes, e.g. ``b"\\x1b[31mRED\\x1b[0m\\r\\n"``.
    """
    return _OCTAL_ESCAPE_RE.sub(lambda m: bytes([int(m.group(1), 8)]), value)


def _hex_send_keys_commands(target: str, data: bytes) -> list[bytes]:
    """Build ``send-keys -H`` control-mode command line(s) for raw input bytes.

    :param target: The tmux target the keys are sent to, e.g. ``"main"``.
    :param data: Raw input bytes from the browser (keystrokes, paste, mouse
        reports, ESC sequences).
    :returns: One or more newline-terminated command lines, each carrying at
        most :data:`_SEND_KEYS_HEX_BYTES_PER_CALL` bytes as space-separated hex.
    """
    commands: list[bytes] = []
    for start in range(0, len(data), _SEND_KEYS_HEX_BYTES_PER_CALL):
        chunk = data[start : start + _SEND_KEYS_HEX_BYTES_PER_CALL]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        commands.append(f"send-keys -t {target} -H {hexs}\n".encode())
    return commands


async def _run_tmux_capture(socket_path: str, tmux_target: str) -> bytes | None:
    """Capture the current pane screen (with escapes) to seed the browser view.

    A control client only receives ``%output`` produced after it attaches, so
    the pane's pre-attach content must be seeded explicitly. ``-e`` preserves
    SGR/color escapes so the seed paints identically to the live pane.

    ``capture-pane -p`` separates rows with a **bare LF** (``\\n``, no carriage
    return). Written verbatim into xterm.js each LF moves the cursor down but
    not to column 0, so every row starts where the previous one ended — the
    whole seed staircases to the right. We rewrite each row separator to
    ``\\r\\n`` so the grid paints flush-left, matching the live ``%output``
    stream (which already carries CRLF). Home + clear (``\\x1b[H\\x1b[2J``) is
    prepended so the seed lands on a clean screen at the top-left.

    ``capture-pane`` records only the cell contents, not the cursor. Writing
    the seed leaves the browser cursor wherever the last row ended, not where
    the application actually parked it (e.g. inside a prompt input box). We
    query ``#{cursor_x}`` / ``#{cursor_y}`` and append a CUP escape so the
    cursor is restored to its real position, and honor ``#{cursor_flag}`` so a
    hidden cursor stays hidden.

    **Scrollback**, conditioned on the screen mode:

    - **Primary screen** (a shell, the polly REPL): capture from the start of
      history (``-S -``) so the browser recovers the full scrollback, not just
      the visible screen. The extra history lines scroll into xterm's own
      scrollback as they're written; the cursor CUP is screen-relative so it
      still lands correctly on the visible grid.
    - **Alternate screen** (claude, codex, vim): capture the visible screen
      only. The alternate buffer has no scrollback, and ``-S -`` would leak the
      stale *primary*-screen history from before the app switched buffers —
      lines that were never part of the app's UI — corrupting the seed. tmux's
      ``#{alternate_on}`` distinguishes the two.

    :param socket_path: tmux server socket path.
    :param tmux_target: The ``-t`` target, e.g. ``"main"``.
    :returns: The captured bytes to write into xterm, or ``None`` on failure
        (the caller proceeds without a seed rather than aborting the attach).
    """
    tmux = shutil.which("tmux")
    if tmux is None:
        return None
    meta = await _capture_pane_metadata(tmux, socket_path, tmux_target)
    # Only extend the capture into history when on the primary screen; on the
    # alternate screen ``-S -`` leaks stale primary history (see docstring).
    capture_args = ["capture-pane", "-e", "-p", "-t", tmux_target]
    if meta is not None and not meta.alternate_on:
        capture_args += ["-S", "-"]
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux,
            "-S",
            socket_path,
            *capture_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    # ``capture-pane -p`` emits one LF per row — INCLUDING a trailing LF after
    # the final row. Writing that trailing separator paints the last row and
    # then advances the cursor past it, which on a full-height pane scrolls the
    # whole screen up by one line (the "extra line" / off-by-one). Strip the
    # single trailing newline so the last row is painted with no line break
    # after it; the cursor-restore escape then lands on the correct row.
    body = stdout[:-1] if stdout.endswith(b"\n") else stdout
    # Normalize the remaining bare-LF row separators to CRLF (see docstring) and
    # paint onto a cleared screen from the home cursor so the seed can't
    # staircase.
    normalized = _CAPTURE_ROW_SEP_RE.sub(b"\r\n", body)
    cursor = _cursor_restore_escape(meta)
    return b"\x1b[H\x1b[2J" + normalized + cursor


@dataclass(frozen=True)
class _PaneMetadata:
    """Pane state needed to reconstruct the seed: cursor + screen mode.

    :param cursor_x: 0-based cursor column from ``#{cursor_x}``.
    :param cursor_y: 0-based cursor row from ``#{cursor_y}``.
    :param cursor_visible: Whether ``#{cursor_flag}`` reported the cursor shown.
    :param alternate_on: Whether the pane is on the alternate screen
        (``#{alternate_on}`` == 1).
    """

    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    alternate_on: bool


async def _capture_pane_metadata(
    tmux: str, socket_path: str, tmux_target: str
) -> _PaneMetadata | None:
    """Query cursor position, cursor visibility, and alt-screen state.

    One ``display-message`` fetches every field the seed needs. Returns
    ``None`` on any failure — the caller degrades gracefully (skips the
    history extension and the cursor restore) rather than aborting the attach.

    :param tmux: Absolute path to the tmux binary.
    :param socket_path: tmux server socket path.
    :param tmux_target: The ``-t`` target, e.g. ``"main"``.
    :returns: The parsed :class:`_PaneMetadata`, or ``None`` if unavailable.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            tmux,
            "-S",
            socket_path,
            "display-message",
            "-p",
            "-t",
            tmux_target,
            "#{cursor_x},#{cursor_y},#{cursor_flag},#{alternate_on}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    try:
        x_str, y_str, flag_str, alt_str = stdout.decode().strip().split(",")
        return _PaneMetadata(
            cursor_x=int(x_str),
            cursor_y=int(y_str),
            cursor_visible=flag_str.strip() == "1",
            alternate_on=alt_str.strip() == "1",
        )
    except (ValueError, UnicodeDecodeError):
        return None


def _cursor_restore_escape(meta: _PaneMetadata | None) -> bytes:
    """Build the escape that restores the pane cursor after a seed.

    :param meta: Pane metadata from :func:`_capture_pane_metadata`, or ``None``.
    :returns: A CUP escape (``\\x1b[{row};{col}H``, 1-based) plus a show/hide
        escape matching the pane's cursor visibility, or ``b""`` when *meta* is
        ``None`` (a missing cursor restore is cosmetic, never fatal).
    """
    if meta is None:
        return b""
    # tmux cursor_x/y are 0-based; CUP is 1-based.
    cup = f"\x1b[{meta.cursor_y + 1};{meta.cursor_x + 1}H".encode()
    visibility = b"\x1b[?25h" if meta.cursor_visible else b"\x1b[?25l"
    return cup + visibility


async def bridge_tmux_control_to_websocket(
    websocket: WebSocket,
    *,
    socket_path: str,
    tmux_target: str,
    read_only: bool,
    on_client_interaction: Callable[[], None] | None = None,
) -> None:
    """Bridge a tmux control-mode client to an already-accepted *websocket*.

    Drop-in alternative to
    :func:`omnigent.terminals.ws_bridge.bridge_tmux_pty_to_websocket` with the
    same signature and browser wire protocol. Caller must have called
    ``websocket.accept()``. On exit (any branch) the control client is torn
    down and the websocket closed best-effort with the shared 4404/4405 codes.

    :param websocket: An accepted FastAPI :class:`WebSocket`.
    :param socket_path: Filesystem path to the tmux server socket.
    :param tmux_target: The ``-t`` target string identifying the session.
    :param read_only: When ``True``, attach with ``-r`` *and* drop inbound
        binary input frames at the application layer (defense in depth).
    :param on_client_interaction: Optional callback fired on every client
        interaction (connect, disconnect, each input/resize frame) so the
        idle watcher can discount client-driven repaints. See the PTY bridge
        for the full rationale.
    """
    # Attaching reflows the pane to this client's size — stamp it as a client
    # interaction so the idle watcher discounts the resulting repaint.
    if on_client_interaction is not None:
        on_client_interaction()

    tmux = shutil.which("tmux")
    if tmux is None:
        _logger.error("tmux not found on PATH; cannot control-attach target=%s", tmux_target)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=WS_CLOSE_INTERNAL_ERROR, reason="tmux not found")
        return

    # Seed the browser terminal with the current screen BEFORE attaching so no
    # pre-attach content is missing. Failure is non-fatal — a live pane redraw
    # will repaint it shortly.
    seed = await _run_tmux_capture(socket_path, tmux_target)
    if seed:
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_bytes(seed)

    argv = [tmux, "-S", socket_path, "-f", "/dev/null", "-C", "attach"]
    if read_only:
        argv.append("-r")
    argv += ["-t", tmux_target]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Raise the stdout StreamReader line cap well above the 64 KiB
            # default so an oversized ``%output`` line can't crash the reader
            # (see _CONTROL_STDOUT_LINE_LIMIT).
            limit=_CONTROL_STDOUT_LINE_LIMIT,
        )
    except (OSError, ValueError):
        _logger.exception("control-attach spawn failed target=%s", tmux_target)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=WS_CLOSE_INTERNAL_ERROR, reason="control attach failed")
        return

    assert proc.stdin is not None and proc.stdout is not None
    stdin = proc.stdin
    stdout = proc.stdout

    async def _send_command(line: bytes) -> None:
        """Write one newline-terminated control command, ignoring a dead pipe."""
        if stdin.is_closing():
            return
        try:
            stdin.write(line)
            await stdin.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            return

    async def _control_to_ws() -> None:
        """Read tmux control-protocol lines; forward %output, watch lifecycle."""
        while True:
            line = await stdout.readline()
            if not line:
                # tmux control client closed its stdout — server/session gone.
                return
            line = line.rstrip(b"\r\n")
            if line.startswith(b"%output "):
                # %output %<pane-id> <escaped-bytes>
                parts = line.split(b" ", 2)
                if len(parts) == 3:
                    raw = unescape_control_output(parts[2])
                    try:
                        await websocket.send_bytes(raw)
                    except (RuntimeError, WebSocketDisconnect):
                        return
            elif line.startswith(b"%exit"):
                return
            elif line.startswith(b"%window-close"):
                # The single-pane session's only window closing means the pane
                # is gone. Let the exit path decide detach-vs-gone via a
                # liveness probe. (%pane-mode-changed — copy-mode enter/leave —
                # is deliberately NOT a close trigger.)
                return
            # %begin/%end/%error reply blocks and other notifications
            # (%layout-change, %session-changed, %window-*) need no browser
            # forwarding — the browser xterm renders purely from %output.

    async def _ws_to_control() -> None:
        """Read browser frames; resize via refresh-client -C, input via -H hex."""
        try:
            while True:
                msg = await websocket.receive()
                if on_client_interaction is not None:
                    on_client_interaction()
                if msg.get("type") == "websocket.disconnect":
                    return
                text = msg.get("text")
                data = msg.get("bytes")
                if text is not None:
                    try:
                        ctl = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(ctl, dict) and ctl.get("type") == "resize":
                        try:
                            cols = int(ctl["cols"])
                            rows = int(ctl["rows"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        await _send_command(f"refresh-client -C {cols}x{rows}\n".encode())
                elif data is not None and not read_only:
                    for cmd in _hex_send_keys_commands(tmux_target, data):
                        await _send_command(cmd)
        except WebSocketDisconnect:
            return

    # Do NOT prime a default size. A control client leaves the window size
    # untouched until it issues its first ``refresh-client -C``, so priming
    # 80x24 here would shrink the window on attach and then grow it again the
    # instant the browser reports its real dimensions — two spurious SIGWINCHes
    # per attach (visible as a resize bounce every time the terminal is
    # re-mounted, e.g. toggling transcript mode). Instead we wait for the
    # browser's first resize message; tmux dedupes a ``refresh-client -C`` that
    # matches the current window size, so a re-attach at an unchanged size emits
    # no resize at all.

    control_task = asyncio.create_task(_control_to_ws(), name="tmux-control-to-ws")
    ws_task = asyncio.create_task(_ws_to_control(), name="tmux-ws-to-control")
    control_ended_first = False
    try:
        done, pending = await asyncio.wait(
            {control_task, ws_task}, return_when=asyncio.FIRST_COMPLETED
        )
        control_ended_first = control_task in done
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None:
                _logger.warning("control-attach: bridge task crashed: %r", exc)
    finally:
        # Detach reflows the pane back to remaining clients — stamp it.
        if on_client_interaction is not None:
            on_client_interaction()
        # Detach the control client: an empty command line detaches cleanly.
        await _send_command(b"\n")
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        with contextlib.suppress(RuntimeError):
            if control_ended_first:
                # The control client ended: distinguish a genuine session-gone
                # (%exit with a dead/absent pane) from a mere detach. Reuse the
                # PTY bridge's pane-dead probe for a single source of truth.
                from omnigent.terminals.ws_bridge import (
                    _check_pane_dead_definitive,
                    _tmux_session_alive,
                )

                pane_dead = await _check_pane_dead_definitive(socket_path, tmux_target)
                if pane_dead is True or (
                    pane_dead is None and not await _tmux_session_alive(socket_path, tmux_target)
                ):
                    await websocket.close(
                        code=WS_CLOSE_TERMINAL_NOT_FOUND,
                        reason="terminal session ended",
                    )
                else:
                    await websocket.close(
                        code=WS_CLOSE_TERMINAL_DETACHED,
                        reason="terminal detached",
                    )
            else:
                await websocket.close()
