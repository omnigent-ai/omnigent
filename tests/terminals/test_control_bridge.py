"""
Unit + integration tests for :mod:`omnigent.terminals.control_bridge`.

Covers the pure helpers (``unescape_control_output`` octal round-trip,
``_hex_send_keys_commands`` chunking) and an end-to-end drive of
``bridge_tmux_control_to_websocket`` against a real private tmux server via a
fake WebSocket: seed-on-attach, ``%output`` streaming of ``send-keys`` input,
and the detach close code.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import omnigent.terminals.control_bridge as control_bridge
import omnigent.terminals.ws_common as ws_common
from omnigent.terminals.control_bridge import (
    _SEND_KEYS_HEX_BYTES_PER_CALL,
    _clipboard_buffer_name,
    _hex_send_keys_commands,
    _read_tmux_buffer,
    bridge_tmux_control_to_websocket,
    unescape_control_output,
)

_HAS_TMUX = shutil.which("tmux") is not None


def _tmux_supports_bracket_paste_flag() -> bool:
    """tmux exposes ``#{bracket_paste_flag}`` only since 3.7."""
    tmux = shutil.which("tmux")
    if tmux is None:
        return False
    try:
        out = subprocess.run(
            [tmux, "-V"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        # "tmux 3.7b" / "tmux next-3.7" — take the trailing version token and
        # strip any suffix letters.
        version = out.split()[-1].removeprefix("next-")
        major, minor = version.rstrip("abcdefghijklmnopqrstuvwxyz").split(".")[:2]
        return (int(major), int(minor)) >= (3, 7)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False


_HAS_TMUX_BRACKET_PASTE_FLAG = _tmux_supports_bracket_paste_flag()


def test_unescape_control_output_round_trips_control_bytes() -> None:
    """tmux octal escapes (\\ooo) decode back to raw ESC/CR/LF bytes."""
    assert unescape_control_output(rb"\033[31mRED\033[0m\015\012") == b"\x1b[31mRED\x1b[0m\r\n"
    # A literal backslash is escaped as \134 and must decode back to one byte.
    assert unescape_control_output(rb"a\134b") == b"a\\b"
    # Printable bytes pass through untouched.
    assert unescape_control_output(b"plain text 123") == b"plain text 123"


def test_clipboard_buffer_name_accepts_only_safe_notifications() -> None:
    """Only ordinary tmux copy-buffer names are accepted as command targets."""
    assert _clipboard_buffer_name(b"%paste-buffer-changed buffer0") == "buffer0"
    assert _clipboard_buffer_name(b"%paste-buffer-changed named-buffer.1") == "named-buffer.1"
    assert _clipboard_buffer_name(b"%paste-buffer-changed bad name") is None
    assert _clipboard_buffer_name(b"%paste-buffer-changed ../bad") is None
    assert _clipboard_buffer_name(b"%output %0 text") is None


@pytest.mark.asyncio
async def test_tmux_buffer_read_cancellation_reaps_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an in-flight clipboard read kills and awaits its tmux child."""

    class _BlockedProcess:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.returncode: int | None = None
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.stdout.feed_eof()

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

    proc = _BlockedProcess()

    async def _spawn(*_args: object, **_kwargs: object) -> _BlockedProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    task = asyncio.create_task(_read_tmux_buffer("tmux", "socket", "buffer0"))
    await asyncio.sleep(0)
    assert task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed is True
    assert proc.returncode == -9


def test_hex_send_keys_commands_encodes_and_chunks() -> None:
    """Input bytes become space-separated hex, split under the per-call cap."""
    cmds = _hex_send_keys_commands("main", b"\x1b[A")
    assert cmds == [b"send-keys -t main -H 1b 5b 41\n"]

    big = b"\x00" * (_SEND_KEYS_HEX_BYTES_PER_CALL + 5)
    cmds = _hex_send_keys_commands("main", big)
    assert len(cmds) == 2
    # First chunk carries exactly the cap's worth of "00" tokens.
    assert cmds[0].count(b"00") == _SEND_KEYS_HEX_BYTES_PER_CALL
    assert cmds[1].count(b"00") == 5


class _FakeWebSocket:
    """Minimal WebSocket stand-in driving bridge_tmux_control_to_websocket.

    Records outbound binary frames, feeds a scripted sequence of inbound
    messages, and captures the close code.
    """

    def __init__(self, inbound: list[dict[str, object]], send_delay_s: float = 0.0) -> None:
        self._inbound = list(inbound)
        self.sent: list[bytes] = []
        self.sent_text: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._recv_gate = asyncio.Event()
        # Per-send delay simulates a real network so a burst backlogs behind the
        # send — the condition under which the forwarder coalesces.
        self._send_delay_s = send_delay_s

    async def send_bytes(self, data: bytes) -> None:
        if self._send_delay_s:
            await asyncio.sleep(self._send_delay_s)
        self.sent.append(data)

    async def send_text(self, data: str) -> None:
        if self._send_delay_s:
            await asyncio.sleep(self._send_delay_s)
        self.sent_text.append(data)

    async def receive(self) -> dict[str, object]:
        if self._inbound:
            return self._inbound.pop(0)
        # Block forever once scripted input is exhausted so the bridge's other
        # task (control→ws) decides when the attach ends.
        await self._recv_gate.wait()
        return {"type": "websocket.disconnect"}

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason


class _BlockingOutputWebSocket(_FakeWebSocket):
    """Fake WebSocket that can hold terminal output sends in flight."""

    def __init__(self) -> None:
        super().__init__(inbound=[])
        self.block_output = False
        self.send_started = asyncio.Event()
        self.release_output = asyncio.Event()

    async def send_bytes(self, data: bytes) -> None:
        if self.block_output:
            self.send_started.set()
            await self.release_output.wait()
        await super().send_bytes(data)


async def _new_private_tmux(inner: str) -> tuple[Path, str]:
    """Create a private single-pane tmux server like terminal.py:launch."""
    tmux = shutil.which("tmux")
    assert tmux
    tmpdir = Path(tempfile.mkdtemp(prefix="cc-test-"))
    sock = tmpdir / "tmux.sock"
    proc = await asyncio.create_subprocess_exec(
        tmux,
        "-S",
        str(sock),
        "-f",
        os.devnull,
        "set-option",
        "-g",
        "history-limit",
        "10000",
        ";",
        "new-session",
        "-d",
        "-s",
        "main",
        "-x",
        "80",
        "-y",
        "24",
        inner,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return sock, "main"


async def _kill_tmux(sock: Path) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        return
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            tmux,
            "-S",
            str(sock),
            "kill-server",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
    shutil.rmtree(sock.parent, ignore_errors=True)


async def _kill_and_join(sock: Path, task: asyncio.Task[None]) -> None:
    """Kill the tmux server and wind the bridge task down cleanly.

    Killing the server closes the control client's stdout, so the bridge
    exits on its own; we wait a bounded time, then cancel as a fallback and
    await the cancellation propagating. Shared teardown for the end-to-end
    tests so each doesn't repeat the kill/join dance.

    :param sock: tmux socket whose server to kill.
    :param task: The running ``bridge_tmux_control_to_websocket`` task.
    """
    await _kill_tmux(sock)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=5)
    if not task.done():
        task.cancel()
        # ``await`` blocks until the cancelled task finishes unwinding — the
        # return value is discarded but the wait is the point.
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_outer_cancellation_joins_child_tasks() -> None:
    """Cancelling the route cannot leave bridge reader/sender tasks detached."""
    sock, target = await _new_private_tmux("sleep 30")
    ws = _FakeWebSocket(inbound=[])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    try:
        await asyncio.sleep(0.2)
        assert task.cancel()
        [task_result] = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(task_result, asyncio.CancelledError)
        await asyncio.sleep(0)
        names = {pending.get_name() for pending in asyncio.all_tasks() if not pending.done()}
        assert not names.intersection(
            {
                "tmux-control-read",
                "tmux-control-forward",
                "tmux-control-clipboard",
                "tmux-ws-to-control",
            }
        )
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_streams_large_output_burst() -> None:
    """A large post-attach output burst streams through intact, no reader crash.

    The raw-``read()`` reader (not ``readline()``) has no per-line length cap,
    so a big burst can't raise ``LimitOverrunError`` and kill the reader. Assert
    a ~200 KiB live burst reaches the browser fully rather than dropping the
    connection.
    """
    # Hold the pane quiet for 1s, THEN emit ~200 KiB in one burst — so the
    # payload arrives as live post-attach %output (the readline path), not via
    # the capture-pane seed.
    payload_len = 200_000
    sock, target = await _new_private_tmux(
        f'python3 -c \'import sys,time; time.sleep(1.0); sys.stdout.write("X"*{payload_len}); '
        "sys.stdout.flush(); time.sleep(30)'"
    )
    # Attach while the pane is still quiet (before the burst fires).
    await asyncio.sleep(0.2)

    ws = _FakeWebSocket(inbound=[])

    async def _run() -> None:
        await bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )

    task = asyncio.create_task(_run())
    # Wait past the burst so the big %output line is read and forwarded.
    await asyncio.sleep(2.0)

    # The reader must still be alive and the large payload must have reached the
    # browser via the live stream.
    total_x = sum(frame.count(b"X") for frame in ws.sent)
    assert total_x >= payload_len, (
        f"large output truncated/dropped: got {total_x} X bytes of {payload_len}"
    )

    await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_coalesces_burst_when_send_lags() -> None:
    """A burst behind a slow send collapses into far fewer, larger frames.

    tmux firehoses ``%output`` as many small per-line writes; when the browser
    send can't keep pace a backlog forms, and the forwarder merges it into large
    ``send_bytes`` instead of thousands of tiny ones. Assert on the *average
    frame size* rather than an absolute frame count: the count is scheduling-
    dependent (how much backlog accrues between drains varies with load), but a
    coalesced frame is always many times a single ``%output`` line (~1 KB),
    which is the timing-robust signal that merging happened at all.
    """
    payload_len = 500_000
    sock, target = await _new_private_tmux(
        f'python3 -c \'import sys,time; time.sleep(1.0); sys.stdout.write("X"*{payload_len}); '
        "sys.stdout.flush(); time.sleep(30)'"
    )
    await asyncio.sleep(0.2)

    # A per-frame send delay makes the browser lag tmux's firehose so a backlog
    # forms; 5 ms is generous enough that a backlog reliably accrues even under
    # a loaded CI runner (where a 1 ms delay can keep pace and defeat merging).
    ws = _FakeWebSocket(inbound=[], send_delay_s=0.005)
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    # Allow ample time for the full burst to drain through the slow send.
    await asyncio.sleep(8.0)

    burst_frames = [f for f in ws.sent if b"X" in f]
    total_x = sum(f.count(b"X") for f in ws.sent)
    assert total_x >= payload_len, f"burst truncated: got {total_x} X bytes of {payload_len}"
    # Coalesced frames are far larger than a single ``%output`` line (~1 KB).
    # Require a comfortably-above-per-line average — proves merging without
    # depending on the exact (scheduling-dependent) frame count. Without
    # coalescing this average would be ~1 KB; merged it is many KB.
    avg_frame = total_x / max(1, len(burst_frames))
    assert avg_frame > 4000, (
        f"expected coalesced frames (avg > 4 KB), got avg {avg_frame:.0f}B over "
        f"{len(burst_frames)} frames — forwarder is not merging the backlog"
    )

    await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_burst_then_exit_delivers_full_tail() -> None:
    """A burst-then-exit program's tail isn't dropped when %exit races the drain.

    The reader and forwarder are separate tasks; shutdown keys on the reader.
    When a program dumps a big burst and exits immediately (``cat bigfile``,
    build output), ``%exit`` arrives while the slow browser send is still
    draining the queued backlog. The bridge must let the forwarder finish
    draining the sentinel-terminated queue before teardown, or the tail is
    silently lost. Emit the burst then exit (no trailing sleep) behind a slow
    send and assert the FULL payload still reaches the browser.
    """
    # The backlog must be too big to fully drain before the reader hits %exit,
    # or the forwarder finishes on its own and the race never triggers. 2 MB
    # behind a 5 ms/frame send leaves a large queued tail at %exit time — the
    # pre-fix code (cancel forwarder on reader-done) drops ~35% of it.
    payload_len = 2_000_000
    sock, target = await _new_private_tmux(
        # Sleep first so the control client attaches BEFORE the burst — the
        # payload then arrives as live %output. Then burst and exit immediately
        # (no trailing sleep) so %exit races the still-draining slow send: the
        # regression window. (A burst emitted before attach is gone at the tmux
        # layer, not a bridge concern.)
        f'python3 -c \'import sys,time; time.sleep(1.5); sys.stdout.write("Y"*{payload_len}); '
        "sys.stdout.flush()'"
    )
    await asyncio.sleep(0.2)

    ws = _FakeWebSocket(inbound=[], send_delay_s=0.005)
    reader_done = asyncio.Event()
    forward_done = asyncio.Event()
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws,
            socket_path=str(sock),
            tmux_target=target,
            read_only=False,
            reader_done=reader_done,
            forward_done=forward_done,
        )
    )
    # Deterministically wait until the reader has queued the whole backlog plus
    # the EOF sentinel, then until the forwarder has fully drained it — no
    # arbitrary wall-clock sleep. Timeouts are generous backstops, not timing.
    await asyncio.wait_for(reader_done.wait(), timeout=20.0)
    await asyncio.wait_for(forward_done.wait(), timeout=20.0)

    total_y = sum(f.count(b"Y") for f in ws.sent)
    assert total_y >= payload_len, (
        f"burst-then-exit dropped the tail: got {total_y} Y bytes of {payload_len} "
        "— forwarder was cancelled before draining the queued backlog"
    )

    await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_seeds_streams_and_detaches() -> None:
    """End-to-end: seed the pre-attach screen, stream typed input, detach clean."""
    # `cat` echoes input back to the pane (→ %output); the printf lands before
    # attach so it can only reach the browser via the capture-pane seed.
    sock, target = await _new_private_tmux("printf 'SEEDED-LINE\\n'; cat")
    await asyncio.sleep(0.3)  # let the printf render before we attach

    ws = _FakeWebSocket(
        inbound=[
            {"type": "websocket.receive", "text": '{"type":"resize","cols":100,"rows":30}'},
            {"type": "websocket.receive", "bytes": b"typed-input\r"},
        ]
    )

    async def _run() -> None:
        await bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )

    task = asyncio.create_task(_run())
    # Give it time to seed, resize, inject input, and observe the echo.
    await asyncio.sleep(1.2)

    joined = b"".join(ws.sent)
    assert b"SEEDED-LINE" in joined, "seed-on-attach did not paint pre-attach screen"
    assert b"typed-input" in joined, "send-keys input was not echoed via %output"

    # The seed frame must not carry bare-LF row separators: capture-pane -p
    # joins rows with a lone \n, which would staircase the whole screen to the
    # right in xterm. The bridge rewrites them to CRLF and prepends home+clear.
    seed_frame = ws.sent[0]
    assert seed_frame.startswith(b"\x1b[H\x1b[2J"), "seed did not start with home+clear"
    assert b"\n" not in seed_frame.replace(b"\r\n", b""), (
        "seed frame contains a bare LF — rows will staircase in xterm"
    )

    # Kill the server → the control client's stdout closes → bridge exits.
    await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_seed_restores_cursor_position() -> None:
    """The seed ends with a CUP escape putting the cursor where the app left it.

    capture-pane records only cell contents, not the cursor, so the seed must
    reposition it explicitly or the browser cursor sits at the end of the
    seeded text instead of inside the app's prompt.
    """
    from omnigent.terminals.control_bridge import _run_tmux_capture

    # Park the cursor at row 9, col 8 (1-based) and hold the pane open.
    sock, target = await _new_private_tmux(
        'python3 -c \'import sys,time; sys.stdout.write("a\\r\\nb\\r\\n\\x1b[9;8H"); '
        "sys.stdout.flush(); time.sleep(30)'"
    )
    await asyncio.sleep(0.4)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        # tmux reports 0-based cursor_x=7, cursor_y=8 → CUP is 1-based [9;8H.
        assert b"\x1b[9;8H" in seed, f"cursor CUP escape missing from seed: {seed[-24:]!r}"
        # Visible cursor → show-cursor tail.
        assert seed.endswith(b"\x1b[?25h"), f"seed did not end with show-cursor: {seed[-12:]!r}"
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_seed_full_height_pane_does_not_scroll_or_shift_cursor() -> None:
    """A full-height pane seed must render row1 at top and the cursor in place.

    capture-pane -p emits a trailing LF after the final row; writing it on a
    full-height pane scrolls the whole screen up one line (the "extra line"
    off-by-one) and shifts the restored cursor. Render the seed through a real
    VT emulator (pyte) and assert no scroll + exact cursor position.
    """
    import pyte

    from omnigent.terminals.control_bridge import _run_tmux_capture

    # Fill all 24 rows (row1..row24) and park the cursor at row24 col6.
    sock, target = await _new_private_tmux(
        "python3 -c 'import sys,time\n"
        'sys.stdout.write("\\x1b[?1049h")\n'
        'for r in range(1,25): sys.stdout.write(f"\\x1b[{r};1Hrow{r}")\n'
        'sys.stdout.write("\\x1b[24;6H")\n'
        "sys.stdout.flush(); time.sleep(30)'"
    )
    await asyncio.sleep(0.4)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        screen = pyte.Screen(80, 24)
        stream = pyte.ByteStream(screen)
        stream.feed(seed)
        display = screen.display
        # No scroll-up: the top row is still row1, the bottom is row24.
        assert display[0].startswith("row1"), f"top row scrolled off: {display[0]!r}"
        assert display[23].startswith("row24"), f"bottom row wrong: {display[23]!r}"
        # Cursor restored to the app's position (0-based (23, 5) for [24;6H).
        assert (screen.cursor.y, screen.cursor.x) == (23, 5), (
            f"cursor off: got ({screen.cursor.y}, {screen.cursor.x}), expected (23, 5)"
        )
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_seed_recovers_primary_screen_scrollback() -> None:
    """On the primary screen the seed captures full history, not just the screen."""
    from omnigent.terminals.control_bridge import _run_tmux_capture

    sock, target = await _new_private_tmux("bash --norc")
    await asyncio.sleep(0.2)
    tmux = shutil.which("tmux")
    assert tmux
    # Emit 100 history lines — far more than the 24-row visible screen.
    proc = await asyncio.create_subprocess_exec(
        tmux,
        "-S",
        str(sock),
        "send-keys",
        "-t",
        target,
        "-l",
        "for i in $(seq 1 100); do echo hist-$i; done",
    )
    await proc.communicate()
    proc = await asyncio.create_subprocess_exec(
        tmux, "-S", str(sock), "send-keys", "-t", target, "Enter"
    )
    await proc.communicate()
    await asyncio.sleep(0.6)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        # Full history recovered (a visible-only capture would show ~23 lines).
        assert seed.count(b"hist-") >= 100, (
            f"scrollback not recovered: only {seed.count(b'hist-')} history lines"
        )
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_seed_alternate_screen_does_not_leak_primary_history() -> None:
    """On the alternate screen the seed must not include stale primary history.

    ``capture-pane -S -`` on an alt-screen pane returns the primary buffer's
    scrollback from before the app switched — lines that were never part of
    the app's UI. The bridge must capture the visible screen only there.
    """
    from omnigent.terminals.control_bridge import _run_tmux_capture

    # 50 primary-screen "OLD" lines, then enter the alternate screen and draw.
    sock, target = await _new_private_tmux(
        "python3 -c 'import sys,time\n"
        'for i in range(50): sys.stdout.write(f"OLD-{i}\\r\\n")\n'
        'sys.stdout.write("\\x1b[?1049h")\n'
        'for r in range(1,25): sys.stdout.write(f"\\x1b[{r};1HALT-row{r}")\n'
        "sys.stdout.flush(); time.sleep(30)'"
    )
    await asyncio.sleep(0.5)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        assert seed.count(b"OLD-") == 0, (
            f"alt-screen seed leaked {seed.count(b'OLD-')} stale primary-history lines"
        )
        assert b"ALT-row" in seed, "alt-screen visible content missing from seed"
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_seed_replays_alt_screen_and_mouse_modes() -> None:
    """The seed replays the pane program's screen/input modes.

    capture-pane records cells only; a TUI that entered the alternate screen
    and enabled mouse tracking BEFORE this client attached (OpenCode, vim)
    would otherwise leave the browser xterm believing no tracking is active —
    wheel events then send nothing and the view cannot scroll.
    """
    from omnigent.terminals.control_bridge import _run_tmux_capture

    # The OpenCode/bubbletea shape: alt screen + any-motion mouse + SGR.
    sock, target = await _new_private_tmux(
        "python3 -c 'import sys,time; "
        'sys.stdout.write("\\x1b[?1049h\\x1b[?1003h\\x1b[?1006h\\x1b[1;1HTUI"); '
        "sys.stdout.flush(); time.sleep(30)'"
    )
    await asyncio.sleep(0.5)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        # Alt screen entered BEFORE the clear+content so the seed paints into
        # the alt buffer, never into primary-screen scrollback.
        assert seed.startswith(b"\x1b[?1049h"), f"seed prelude missing 1049h: {seed[:16]!r}"
        assert b"\x1b[?1003h" in seed, "mouse any-motion mode not replayed"
        assert b"\x1b[?1006h" in seed, "SGR mouse encoding not replayed"
        # Modes the program never set stay unset.
        assert b"\x1b[?1002h" not in seed
        assert b"\x1b[?1000h" not in seed
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_seed_rejoins_soft_wrapped_lines() -> None:
    """A pane line wrapped across rows seeds as one logical line.

    ``capture-pane`` without ``-J`` emits one entry per screen row, so a long
    line would seed into xterm as hard lines and copying it back out would
    insert a newline at each wrap point (xterm only rejoins rows it flagged
    as wrapped itself).
    """
    from omnigent.terminals.control_bridge import _run_tmux_capture

    # 150 chars in an 80-col pane wraps across two rows.
    sock, target = await _new_private_tmux(
        'python3 -c \'import sys,time; sys.stdout.write("x" * 150); '
        "sys.stdout.flush(); time.sleep(30)'"
    )
    await asyncio.sleep(0.5)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        assert b"x" * 150 in seed, "soft-wrapped line was not rejoined in the seed"
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(
    not _HAS_TMUX_BRACKET_PASTE_FLAG,
    reason="tmux #{bracket_paste_flag} requires tmux >= 3.7",
)
@pytest.mark.asyncio
async def test_seed_replays_bracketed_paste_mode() -> None:
    """The seed replays bracketed paste when the pane program enabled it.

    A shell/TUI that enabled bracketed paste (``?2004h``) BEFORE this client
    attached would otherwise leave the browser xterm unaware, so a multi-line
    paste arrives as raw newlines and readline executes each line on arrival.
    """
    from omnigent.terminals.control_bridge import _run_tmux_capture

    sock, target = await _new_private_tmux(
        "python3 -c 'import sys,time; "
        'sys.stdout.write("\\x1b[?2004h"); sys.stdout.flush(); time.sleep(30)\''
    )
    await asyncio.sleep(0.5)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        assert b"\x1b[?2004h" in seed, "bracketed paste mode not replayed in seed"
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_seed_plain_shell_replays_no_modes() -> None:
    """A primary-screen pane with no mouse tracking gets no mode escapes.

    Spurious enables would put the browser xterm on the alt buffer (killing
    native scrollback) or swallow wheel events into mouse reports the shell
    can't use.
    """
    from omnigent.terminals.control_bridge import _run_tmux_capture

    sock, target = await _new_private_tmux("bash")
    await asyncio.sleep(0.5)
    try:
        seed = await _run_tmux_capture(str(sock), target)
        assert seed is not None
        for mode in (b"?1049h", b"?1000h", b"?1002h", b"?1003h", b"?1005h", b"?1006h", b"?1h"):
            assert b"\x1b[" + mode not in seed, f"spurious mode enable {mode!r} in seed"
    finally:
        await _kill_tmux(sock)


def test_repair_snapshot_resets_alt_screen_and_mouse_after_gap() -> None:
    """A live repair heals mode-disable bytes lost while a TUI exits."""
    import pyte

    screen = pyte.Screen(40, 5)
    stream = pyte.ByteStream(screen)
    initial_modes = set(screen.mode)
    stream.feed(b"\x1b[?1049h\x1b[?1003h\x1b[?1006h\x1b[?1hALT")
    assert set(screen.mode) != initial_modes

    primary = control_bridge._PaneMetadata(
        cursor_x=0,
        cursor_y=0,
        cursor_visible=True,
        alternate_on=False,
    )
    prelude, postlude = control_bridge._mode_restore_escapes(primary, reset=True)
    stream.feed(prelude + b"\x1b[H\x1b[2JPRIMARY" + postlude)

    assert set(screen.mode) == initial_modes
    assert screen.display[0].startswith("PRIMARY")
    # The initial-seed path remains enable-only for a fresh xterm.
    assert control_bridge._mode_restore_escapes(primary) == (b"", b"")


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_tmux_clipboard_buffer_read_is_exact_and_bounded(tmp_path: Path) -> None:
    """Named buffer reads preserve bytes and reject missing/oversized buffers."""
    sock, _target = await _new_private_tmux("sleep 30")
    tmux = shutil.which("tmux")
    assert tmux
    try:
        payload = b"exact\x00bytes\n"
        payload_path = tmp_path / "clipboard.bin"
        payload_path.write_bytes(payload)
        proc = await asyncio.create_subprocess_exec(
            tmux,
            "-S",
            str(sock),
            "load-buffer",
            "-b",
            "buffer-exact",
            str(payload_path),
        )
        await proc.communicate()
        assert proc.returncode == 0
        assert await _read_tmux_buffer(tmux, str(sock), "buffer-exact") == payload
        assert await _read_tmux_buffer(tmux, str(sock), "buffer-missing") is None

        payload_path.write_bytes(b"X" * (1024 * 1024 + 1))
        proc = await asyncio.create_subprocess_exec(
            tmux,
            "-S",
            str(sock),
            "load-buffer",
            "-b",
            "buffer-oversized",
            str(payload_path),
        )
        await proc.communicate()
        assert proc.returncode == 0
        assert await _read_tmux_buffer(tmux, str(sock), "buffer-oversized") is None
    finally:
        await _kill_tmux(sock)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_forwards_recent_copy_buffer_as_text_frame() -> None:
    """A copy after input on this attach becomes a bounded clipboard message."""
    sock, target = await _new_private_tmux("sleep 30")
    await asyncio.sleep(0.2)

    ws = _FakeWebSocket(inbound=[{"type": "websocket.receive", "bytes": b"x"}])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    await asyncio.sleep(0.5)

    tmux = shutil.which("tmux")
    assert tmux
    # TUI applications may update tmux's paste buffer directly rather than
    # entering tmux's outer copy-mode UI, especially over control transport.
    copied = "copied λ\nsecond line".encode()
    proc = await asyncio.create_subprocess_exec(
        tmux,
        "-S",
        str(sock),
        "set-buffer",
        "-b",
        "buffer-copy-test",
        copied.decode(),
    )
    await proc.communicate()
    assert proc.returncode == 0

    async def _clipboard_arrived() -> bool:
        for _ in range(50):
            if ws.sent_text:
                return True
            await asyncio.sleep(0.05)
        return False

    assert await _clipboard_arrived(), "clipboard control frame was not sent"
    message = json.loads(ws.sent_text[-1])
    assert message["type"] == "clipboard-write"
    assert message["encoding"] == "base64"
    assert base64.b64decode(message["data"]) == copied

    await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_ignores_copy_without_recent_input() -> None:
    """A buffer update not attributable to this attach must not copy locally."""
    sock, target = await _new_private_tmux("sleep 30")
    await asyncio.sleep(0.2)

    ws = _FakeWebSocket(inbound=[])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    await asyncio.sleep(0.5)

    tmux = shutil.which("tmux")
    assert tmux
    proc = await asyncio.create_subprocess_exec(
        tmux, "-S", str(sock), "set-buffer", "-b", "buffer-unrelated", "secret"
    )
    await proc.communicate()
    await asyncio.sleep(0.3)
    assert ws.sent_text == []

    await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_read_only_drops_input() -> None:
    """read_only=True must not inject typed bytes into the pane."""
    sock, target = await _new_private_tmux("cat")
    await asyncio.sleep(0.2)

    ws = _FakeWebSocket(inbound=[{"type": "websocket.receive", "bytes": b"should-not-appear\r"}])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=True
        )
    )
    await asyncio.sleep(0.8)
    assert b"should-not-appear" not in b"".join(ws.sent)

    tmux = shutil.which("tmux")
    assert tmux
    proc = await asyncio.create_subprocess_exec(
        tmux, "-S", str(sock), "set-buffer", "-b", "buffer-read-only", "secret"
    )
    await proc.communicate()
    await asyncio.sleep(0.2)
    assert ws.sent_text == []

    await _kill_and_join(sock, task)


def test_output_queue_bounds_backlog_and_resynchronizes_after_drop() -> None:
    """The queue rejects new output at either budget and preserves EOF."""
    queue = ws_common._ByteBoundedOutputQueue(max_bytes=8, max_items=100)
    repaint_requests: list[int] = []
    queue.on_drop = lambda: repaint_requests.append(1)
    queue.put_nowait(b"aaaa")
    queue.put_nowait(b"bbbb")
    queue.put_nowait(b"cc")

    assert queue.queued_bytes == 8
    assert queue.qsize() == 2
    assert queue.dropped_bytes == 2
    assert repaint_requests == [1]
    assert [queue.get_nowait(), queue.get_nowait()] == [b"aaaa", b"bbbb"]

    queue.put_nowait(b"tail")
    queue.put_nowait(None)
    assert [queue.get_nowait(), queue.get_nowait(), queue.get_nowait()] == [
        ws_common._OUTPUT_GAP_RESYNC,
        b"tail",
        None,
    ]

    item_queue = ws_common._ByteBoundedOutputQueue(max_bytes=100, max_items=2)
    item_queue.put_nowait(b"a")
    item_queue.put_nowait(b"b")
    item_queue.put_nowait(b"c")
    assert item_queue.qsize() == 2
    assert item_queue.dropped_bytes == 1


def test_output_queue_prioritizes_snapshot_within_bounds() -> None:
    """A full queue evicts stale output so its recovery snapshot is retained."""
    queue = ws_common._ByteBoundedOutputQueue(max_bytes=40, max_items=3)
    snapshot = b"\x1b[H\x1b[2JSNAPSHOT"
    queue.put_nowait(b"a" * 20)
    queue.put_nowait(b"b" * 20)
    queue.put_nowait(b"lost")

    queue.put_snapshot_nowait(snapshot)

    assert queue.queued_bytes <= queue.max_bytes
    assert queue.qsize() <= queue.max_items
    queue.put_nowait(None)
    retained = [queue.get_nowait() for _ in range(queue.qsize())]
    assert snapshot in retained
    assert ws_common._OUTPUT_GAP_RESYNC in retained
    assert retained[-1] is None


def test_output_queue_reports_oversized_snapshot_rejection() -> None:
    """A snapshot that cannot fit is reported instead of silently discarded."""
    queue = ws_common._ByteBoundedOutputQueue(max_bytes=16, max_items=4)
    queue.put_nowait(b"retained")
    queue.record_dropped_output(4, request_repaint=False)

    assert queue.put_snapshot_nowait(b"S" * 14) is False
    assert queue.get_nowait() == b"retained"
    assert queue.dropped_chunks == 2


def test_output_queue_loss_logs_identity_and_teardown_counters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both first-drop and teardown diagnostics identify the attached client."""
    identity = "session=s1 terminal=t1 runner=r1 client=c1"
    queue = ws_common._ByteBoundedOutputQueue(max_bytes=1, identity=identity)

    with caplog.at_level("WARNING", logger=ws_common.__name__):
        queue.put_nowait(b"too-large")
        queue.log_drop_summary()

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert all(identity in message for message in messages)
    assert "1 chunks (9 bytes) dropped" in messages[-1]


@pytest.mark.asyncio
async def test_gap_repainter_cancel_joins_active_repaint() -> None:
    """Repaint cancellation waits for the active coroutine to unwind."""
    started = asyncio.Event()
    cancelling = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _repaint() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release_cleanup.wait()

    repainter = ws_common._GapRepainter(_repaint, min_interval_s=0)
    repainter.request()
    await asyncio.wait_for(started.wait(), timeout=1.0)
    cancel = asyncio.create_task(repainter.cancel())
    await asyncio.wait_for(cancelling.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert not cancel.done()

    release_cleanup.set()
    await asyncio.wait_for(cancel, timeout=1.0)


@pytest.mark.asyncio
async def test_repair_capture_cancellation_kills_and_reaps_subprocess() -> None:
    """Cancelling repair capture cannot orphan its tmux subprocess."""

    class _BlockedProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.communicating = asyncio.Event()
            self.killed = False
            self.reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicating.set()
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

    proc = _BlockedProcess()

    task = asyncio.create_task(
        control_bridge._communicate_tmux_process(proc)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(proc.communicating.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.killed is True
    assert proc.reaped is True


@pytest.mark.asyncio
async def test_repair_capture_timeout_kills_and_reaps_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck repair capture times out and reaps its tmux subprocess."""

    class _BlockedProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.killed = False
            self.reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

    proc = _BlockedProcess()

    monkeypatch.setattr(control_bridge, "_TMUX_CAPTURE_TIMEOUT_S", 0.01, raising=False)

    result = await asyncio.wait_for(
        control_bridge._communicate_tmux_process(proc),  # type: ignore[arg-type]
        timeout=0.2,
    )
    assert result is None
    assert proc.killed is True
    assert proc.reaped is True


@pytest.mark.asyncio
async def test_oversized_snapshot_falls_back_to_bounded_plain_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An escape-heavy repaint retries in plain mode and remains deliverable."""
    capture_args: list[tuple[object, ...]] = []

    class _CaptureProcess:
        def __init__(self, args: tuple[object, ...]) -> None:
            self.args = args
            self.returncode = 0

    async def _metadata(*_args: object, **_kwargs: object) -> control_bridge._PaneMetadata:
        return control_bridge._PaneMetadata(
            cursor_x=0,
            cursor_y=0,
            cursor_visible=True,
            alternate_on=False,
        )

    async def _spawn(*args: object, **_kwargs: object) -> _CaptureProcess:
        capture_args.append(args)
        return _CaptureProcess(args)

    async def _communicate(
        proc: _CaptureProcess, *, max_stdout_bytes: int | None = None
    ) -> tuple[bytes, bytes]:
        assert max_stdout_bytes == 256
        if "-e" in proc.args:
            raise control_bridge._CaptureOutputTooLargeError
        return b"PLAIN-SNAPSHOT\n", b""

    monkeypatch.setattr(control_bridge.shutil, "which", lambda _name: "/tmux")
    monkeypatch.setattr(control_bridge, "_capture_pane_metadata", _metadata)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(control_bridge, "_communicate_tmux_process", _communicate)

    snapshot = await control_bridge._capture_pane_snapshot("socket", "main", max_bytes=256)

    assert snapshot is not None
    assert len(snapshot) <= 256
    assert b"PLAIN-SNAPSHOT" in snapshot
    assert snapshot.startswith(control_bridge._LIVE_MODE_RESET)
    assert len(capture_args) == 2
    assert "-e" in capture_args[0]
    assert "-e" not in capture_args[1]


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_retries_failed_gap_capture(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A finite drop burst is repaired after one transient capture failure."""
    monkeypatch.setattr(control_bridge, "_SNAPSHOT_CAPTURE_RETRY_BASE_S", 0.01)
    created: list[ws_common._ByteBoundedOutputQueue] = []

    class _RecordingQueue(ws_common._ByteBoundedOutputQueue):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    marker = b"\x1b[H\x1b[2JRETRY-REPAINT"
    capture_calls = 0

    async def _snapshot(_socket_path: str, _tmux_target: str, **_kwargs: object) -> bytes | None:
        nonlocal capture_calls
        capture_calls += 1
        return None if capture_calls == 1 else marker

    monkeypatch.setattr(control_bridge, "_ByteBoundedOutputQueue", _RecordingQueue)
    monkeypatch.setattr(control_bridge, "_capture_pane_snapshot", _snapshot)
    sock, target = await _new_private_tmux("sleep 30")
    ws = _FakeWebSocket(inbound=[])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not created and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert created and created[0].on_drop is not None
        with caplog.at_level("WARNING", logger=control_bridge.__name__):
            created[0].record_dropped_output(1)
            deadline = asyncio.get_running_loop().time() + 5.0
            while marker not in b"".join(ws.sent) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)

        assert marker in b"".join(ws.sent)
        assert capture_calls == 2
        assert not task.done()
        assert any("attempt 1/3" in record.getMessage() for record in caplog.records)
    finally:
        await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_closes_when_snapshot_cannot_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An irreducibly oversized repair fails the attach instead of staying torn."""
    created: list[ws_common._ByteBoundedOutputQueue] = []

    class _RecordingQueue(ws_common._ByteBoundedOutputQueue):
        def __init__(self) -> None:
            super().__init__(max_bytes=64, max_items=4)
            created.append(self)

    async def _oversized_snapshot(
        _socket_path: str, _tmux_target: str, **_kwargs: object
    ) -> bytes:
        return b"S" * 62

    monkeypatch.setattr(control_bridge, "_ByteBoundedOutputQueue", _RecordingQueue)
    monkeypatch.setattr(control_bridge, "_capture_pane_snapshot", _oversized_snapshot)
    sock, target = await _new_private_tmux("sleep 30")
    ws = _FakeWebSocket(inbound=[])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not created and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert created and created[0].on_drop is not None
        created[0].record_dropped_output(1)
        await asyncio.wait_for(task, timeout=5.0)

        assert ws.close_code == ws_common.WS_CLOSE_INTERNAL_ERROR
        assert ws.close_reason == "terminal repaint failed"
        assert b"S" * 62 not in b"".join(ws.sent)
    finally:
        await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_flushes_drop_repaint_before_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot already in flight is delivered ahead of the EOF sentinel."""
    created: list[ws_common._ByteBoundedOutputQueue] = []

    class _RecordingQueue(ws_common._ByteBoundedOutputQueue):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(control_bridge, "_ByteBoundedOutputQueue", _RecordingQueue)
    marker = b"\x1b[H\x1b[2JEOF-REPAINT"
    capture_started = asyncio.Event()

    async def _slow_snapshot(_socket_path: str, _tmux_target: str, **_kwargs: object) -> bytes:
        capture_started.set()
        await asyncio.sleep(0.2)
        return marker

    monkeypatch.setattr(control_bridge, "_capture_pane_snapshot", _slow_snapshot)
    sock, target = await _new_private_tmux("sleep 30")
    ws = _FakeWebSocket(inbound=[])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    deadline = asyncio.get_running_loop().time() + 5.0
    while not created and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert created and created[0].on_drop is not None
    created[0].record_dropped_output(1)
    await asyncio.wait_for(capture_started.wait(), timeout=2.0)
    await _kill_and_join(sock, task)

    assert marker in b"".join(ws.sent)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_stops_timed_out_repaint_before_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF is queued only after an over-deadline repaint has unwound."""
    monkeypatch.setattr(control_bridge, "_FORWARD_DRAIN_TIMEOUT_S", 0.01)
    events: list[str] = []
    created: list[ws_common._ByteBoundedOutputQueue] = []

    class _RecordingQueue(ws_common._ByteBoundedOutputQueue):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

        def put_nowait(self, item: bytes | None) -> None:
            if item is None:
                events.append("eof")
            super().put_nowait(item)

    capture_started = asyncio.Event()

    async def _blocked_snapshot(_socket_path: str, _tmux_target: str, **_kwargs: object) -> bytes:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("repaint-stopped")
        return b"unreachable"

    monkeypatch.setattr(control_bridge, "_ByteBoundedOutputQueue", _RecordingQueue)
    monkeypatch.setattr(control_bridge, "_capture_pane_snapshot", _blocked_snapshot)
    sock, target = await _new_private_tmux("sleep 30")
    ws = _FakeWebSocket(inbound=[])
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not created and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert created and created[0].on_drop is not None
        created[0].record_dropped_output(1)
        await asyncio.wait_for(capture_started.wait(), timeout=2.0)
    finally:
        await _kill_and_join(sock, task)

    assert events.index("repaint-stopped") < events.index("eof")


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_stalled_item_cap_does_not_repaint_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot eviction cannot sustain captures after a finite output burst."""
    created: list[ws_common._ByteBoundedOutputQueue] = []

    class _RecordingQueue(ws_common._ByteBoundedOutputQueue):
        def __init__(self) -> None:
            super().__init__(max_bytes=1024 * 1024, max_items=2)
            created.append(self)

    marker = b"\x1b[H\x1b[2JSTALLED-QUEUE-REPAINT"
    capture_started = asyncio.Event()
    output_stopped = asyncio.Event()
    capture_calls = 0
    observed_payload_bytes = 0
    payload_len = 262144

    def _tracking_unescape(data: bytes) -> bytes:
        nonlocal observed_payload_bytes
        decoded = unescape_control_output(data)
        observed_payload_bytes += decoded.count(b"Q")
        if observed_payload_bytes >= payload_len:
            output_stopped.set()
        return decoded

    async def _snapshot(_socket_path: str, _tmux_target: str, **_kwargs: object) -> bytes:
        nonlocal capture_calls
        capture_calls += 1
        capture_started.set()
        return marker

    monkeypatch.setattr(control_bridge, "_ByteBoundedOutputQueue", _RecordingQueue)
    monkeypatch.setattr(control_bridge, "_capture_pane_snapshot", _snapshot)
    monkeypatch.setattr(control_bridge, "unescape_control_output", _tracking_unescape)
    monkeypatch.setattr(
        control_bridge,
        "_GapRepainter",
        lambda repaint: ws_common._GapRepainter(repaint, min_interval_s=0.01),
    )
    sock, target = await _new_private_tmux("bash --norc")
    ws = _BlockingOutputWebSocket()
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not created and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert created
        queue = created[0]
        ws.block_output = True

        tmux = shutil.which("tmux")
        assert tmux
        command = f"python3 -c 'import os; os.write(1, bytes([81]) * {payload_len})'; sleep 30"
        proc = await asyncio.create_subprocess_exec(
            tmux, "-S", str(sock), "send-keys", "-t", target, "-l", command
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            tmux, "-S", str(sock), "send-keys", "-t", target, "Enter"
        )
        await proc.communicate()

        await asyncio.wait_for(ws.send_started.wait(), timeout=5.0)
        await asyncio.wait_for(capture_started.wait(), timeout=5.0)
        await asyncio.wait_for(output_stopped.wait(), timeout=5.0)
        deadline = asyncio.get_running_loop().time() + 5.0
        while queue.qsize() < queue.max_items and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert queue.qsize() == queue.max_items

        # The complete finite payload has been parsed and the pane is sleeping.
        # Allow one already-pending trailing repaint, then ensure the capture
        # count converges while the output send remains blocked.
        captures_when_output_stopped = capture_calls
        await asyncio.sleep(0.05)
        captures_after_burst = capture_calls
        assert captures_after_burst - captures_when_output_stopped <= 1
        await asyncio.sleep(0.05)
        assert capture_calls == captures_after_burst

        ws.release_output.set()
        deadline = asyncio.get_running_loop().time() + 5.0
        while marker not in b"".join(ws.sent) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert marker in b"".join(ws.sent)
    finally:
        ws.release_output.set()
        await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_control_bridge_saturation_prioritizes_repaint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked sender repaints, then fails loudly if its trailing repair cannot."""
    monkeypatch.setattr(control_bridge, "_SNAPSHOT_CAPTURE_RETRY_BASE_S", 0.01)
    created: list[ws_common._ByteBoundedOutputQueue] = []

    class _RecordingQueue(ws_common._ByteBoundedOutputQueue):
        def __init__(self) -> None:
            super().__init__(max_bytes=16 * 1024, max_items=2)
            created.append(self)

    marker = b"\x1b[H\x1b[2JQUEUE-SATURATION-REPAINT"
    capture_started = asyncio.Event()
    release_capture = asyncio.Event()
    snapshot_returned = asyncio.Event()
    capture_calls = 0

    async def _snapshot(_socket_path: str, _tmux_target: str, **_kwargs: object) -> bytes | None:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls > 1:
            return None
        capture_started.set()
        await release_capture.wait()
        snapshot_returned.set()
        return marker

    monkeypatch.setattr(control_bridge, "_ByteBoundedOutputQueue", _RecordingQueue)
    monkeypatch.setattr(control_bridge, "_capture_pane_snapshot", _snapshot)
    monkeypatch.setattr(
        control_bridge,
        "_GapRepainter",
        lambda repaint: ws_common._GapRepainter(repaint, min_interval_s=0.01),
    )
    sock, target = await _new_private_tmux("bash --norc")
    ws = _BlockingOutputWebSocket()
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not created and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert created
        queue = created[0]
        ws.block_output = True

        tmux = shutil.which("tmux")
        assert tmux
        command = "python3 -c 'import os; os.write(1, b\"Q\" * 262144)'"
        proc = await asyncio.create_subprocess_exec(
            tmux, "-S", str(sock), "send-keys", "-t", target, "-l", command
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            tmux, "-S", str(sock), "send-keys", "-t", target, "Enter"
        )
        await proc.communicate()

        await asyncio.wait_for(ws.send_started.wait(), timeout=5.0)
        await asyncio.wait_for(capture_started.wait(), timeout=5.0)
        deadline = asyncio.get_running_loop().time() + 5.0
        while queue.dropped_bytes == 0 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert queue.dropped_bytes > 0
        assert (
            queue.qsize() >= queue.max_items or queue.queued_bytes + len(marker) > queue.max_bytes
        )

        release_capture.set()
        await asyncio.wait_for(snapshot_returned.wait(), timeout=2.0)
        assert queue.qsize() <= queue.max_items
        assert queue.queued_bytes <= queue.max_bytes
        ws.release_output.set()

        deadline = asyncio.get_running_loop().time() + 5.0
        while marker not in b"".join(ws.sent) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert marker in b"".join(ws.sent)
        await asyncio.wait_for(task, timeout=5.0)
        assert capture_calls == 1 + control_bridge._SNAPSHOT_CAPTURE_MAX_ATTEMPTS
        assert ws.close_code == ws_common.WS_CLOSE_INTERNAL_ERROR
        assert ws.close_reason == "terminal repaint failed"
    finally:
        release_capture.set()
        ws.release_output.set()
        await _kill_and_join(sock, task)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_capture_pane_snapshot_repaints_visible_content() -> None:
    """Gap recovery captures a clear-screen snapshot with pane content."""
    sock, target = await _new_private_tmux("printf 'SNAPMARK\\n'; sleep 30")
    await asyncio.sleep(0.3)
    try:
        snapshot = await control_bridge._capture_pane_snapshot(str(sock), target)
    finally:
        await _kill_tmux(sock)

    assert snapshot is not None
    assert b"\x1b[H\x1b[2J" in snapshot
    assert b"SNAPMARK" in snapshot
