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
import contextlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from omnigent.terminals.control_bridge import (
    _SEND_KEYS_HEX_BYTES_PER_CALL,
    _hex_send_keys_commands,
    bridge_tmux_control_to_websocket,
    unescape_control_output,
)

_HAS_TMUX = shutil.which("tmux") is not None


def test_unescape_control_output_round_trips_control_bytes() -> None:
    """tmux octal escapes (\\ooo) decode back to raw ESC/CR/LF bytes."""
    assert unescape_control_output(rb"\033[31mRED\033[0m\015\012") == b"\x1b[31mRED\x1b[0m\r\n"
    # A literal backslash is escaped as \134 and must decode back to one byte.
    assert unescape_control_output(rb"a\134b") == b"a\\b"
    # Printable bytes pass through untouched.
    assert unescape_control_output(b"plain text 123") == b"plain text 123"


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
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            ws, socket_path=str(sock), tmux_target=target, read_only=False
        )
    )
    # Wait past attach + burst + the bounded drain (coalesced to ~100 frames of
    # ~20 KB, so ~0.5 s of 5 ms sends; generous margin below).
    await asyncio.sleep(10.0)

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

    await _kill_and_join(sock, task)
