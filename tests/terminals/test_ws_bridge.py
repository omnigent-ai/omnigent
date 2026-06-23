"""
Unit tests for :mod:`omnigent.terminals.ws_bridge`.

Covers ``_write_all_nonblocking`` (retry-on-backpressure / short-write
semantics over a real ``os.pipe``), the tmux-attach child reaper, the
detach-vs-terminal-gone close-code logic that distinguishes a tmux
detach (session still alive) from a real session exit,
and ``_forward_pty_to_ws`` PTY→WS frame coalescing (queued bursts merge
into one frame; a lone keystroke still flushes immediately).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import shutil
import signal
import stat
import struct
import subprocess
import termios
import tty
from pathlib import Path

import pytest

import omnigent.terminals.ws_bridge as ws_bridge
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_DETACHED,
    _forward_pty_to_ws,
    _reap_tmux_attach_child,
    _tmux_session_alive,
    _write_all_nonblocking,
    bridge_tmux_pty_to_websocket,
)

_HAS_TMUX = shutil.which("tmux") is not None


@pytest.mark.asyncio
async def test_write_all_nonblocking_delivers_full_payload() -> None:
    """
    All bytes reach the read end of a pipe, even when the payload is
    larger than a single ``os.write`` may accept atomically.
    """
    r_fd, w_fd = os.pipe()
    flags = fcntl.fcntl(w_fd, fcntl.F_GETFL)
    fcntl.fcntl(w_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    payload = b"hello world"
    loop = asyncio.get_running_loop()
    try:
        await _write_all_nonblocking(loop, w_fd, payload)
        os.close(w_fd)
        w_fd = -1
        received = b""
        while True:
            chunk = os.read(r_fd, 4096)
            if not chunk:
                break
            received += chunk
        assert received == payload
    finally:
        if w_fd != -1:
            os.close(w_fd)
        os.close(r_fd)


@pytest.mark.asyncio
async def test_write_all_nonblocking_retries_on_eagain() -> None:
    """
    When the pipe buffer is full, the helper waits for the fd to
    become writable instead of silently dropping bytes.

    Strategy: fill the pipe to capacity, then launch the write in a
    task. While the write is blocked, drain enough bytes from the
    read end to make room. Assert that every byte arrives.
    """
    r_fd, w_fd = os.pipe()
    flags = fcntl.fcntl(w_fd, fcntl.F_GETFL)
    fcntl.fcntl(w_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    r_flags = fcntl.fcntl(r_fd, fcntl.F_GETFL)
    fcntl.fcntl(r_fd, fcntl.F_SETFL, r_flags | os.O_NONBLOCK)

    loop = asyncio.get_running_loop()
    try:
        # Fill the pipe to capacity.
        fill = b"\x00" * 4096
        total_fill = 0
        while True:
            try:
                n = os.write(w_fd, fill)
                total_fill += n
            except BlockingIOError:
                break

        # The pipe is now full. Schedule a write that must wait.
        extra = b"EXTRA_DATA"
        write_task = asyncio.create_task(_write_all_nonblocking(loop, w_fd, extra))
        # Yield so the write task hits EAGAIN and registers a writer.
        await asyncio.sleep(0.05)
        assert not write_task.done()

        # Drain the pipe so the write can proceed.
        drained = b""
        while True:
            try:
                chunk = os.read(r_fd, 4096)
                if not chunk:
                    break
                drained += chunk
            except BlockingIOError:
                break

        await asyncio.wait_for(write_task, timeout=2.0)

        # Read remaining bytes (the extra data that was written after
        # the drain made room).
        os.close(w_fd)
        w_fd = -1
        tail = b""
        while True:
            try:
                chunk = os.read(r_fd, 4096)
                if not chunk:
                    break
                tail += chunk
            except BlockingIOError:
                break

        all_received = drained + tail
        assert all_received[total_fill:] == extra
    finally:
        if w_fd != -1:
            os.close(w_fd)
        os.close(r_fd)


@pytest.mark.asyncio
async def test_write_all_nonblocking_exits_on_closed_fd() -> None:
    """
    When the fd is closed (PTY gone), the helper exits silently
    instead of propagating ``OSError``.
    """
    r_fd, w_fd = os.pipe()
    flags = fcntl.fcntl(w_fd, fcntl.F_GETFL)
    fcntl.fcntl(w_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    os.close(r_fd)
    os.close(w_fd)

    loop = asyncio.get_running_loop()
    # Should not raise — OSError is caught internally.
    await _write_all_nonblocking(loop, w_fd, b"dead fd")


@pytest.mark.asyncio
async def test_reap_tmux_attach_child_polls_without_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reaping the tmux attach child uses non-blocking waitpid polling.

    A blocking ``waitpid`` in the default executor can hide terminal
    attach shutdown latency behind an executor worker. This test pins
    the polling behavior directly.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    calls: list[tuple[int, int]] = []

    def fake_waitpid(pid: int, options: int) -> tuple[int, int]:
        """
        Return "still running" once, then "reaped".

        :param pid: Process id being reaped.
        :param options: Waitpid options.
        :returns: ``(0, 0)`` first, then ``(pid, 0)``.
        """
        calls.append((pid, options))
        if len(calls) == 1:
            return 0, 0
        return pid, 0

    async def fake_sleep(seconds: float) -> None:
        """
        Avoid real polling delay in the test.

        :param seconds: Requested sleep duration.
        :returns: None.
        """
        assert seconds == ws_bridge._TMUX_ATTACH_WAIT_POLL_S

    monkeypatch.setattr(os, "waitpid", fake_waitpid)
    monkeypatch.setattr(ws_bridge, "_sleep", fake_sleep)

    await _reap_tmux_attach_child(123)

    assert calls == [(123, os.WNOHANG), (123, os.WNOHANG)]


@pytest.mark.asyncio
async def test_reap_tmux_attach_child_kills_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A tmux attach child that ignores SIGTERM is escalated to SIGKILL.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    waitpid_results = iter([(0, 0), (0, 0), (123, 9)])
    signals: list[tuple[int, int]] = []

    def fake_waitpid(pid: int, options: int) -> tuple[int, int]:
        """
        Keep the child alive until after the escalation path runs.

        :param pid: Process id being reaped.
        :param options: Waitpid options.
        :returns: Scripted waitpid result.
        """
        assert pid == 123
        assert options == os.WNOHANG
        return next(waitpid_results)

    def fake_kill(pid: int, sig: int) -> None:
        """
        Capture process signals.

        :param pid: Process id signaled.
        :param sig: Signal number sent.
        :returns: None.
        """
        signals.append((pid, sig))

    async def fake_sleep(seconds: float) -> None:
        """
        Avoid real polling delay in the test.

        :param seconds: Requested sleep duration.
        :returns: None.
        """
        assert seconds == ws_bridge._TMUX_ATTACH_WAIT_POLL_S

    clock_values = iter([0.0, 1.0, 1.0])

    def fake_monotonic() -> float:
        """
        Advance directly past the graceful wait deadline.

        :returns: Next scripted monotonic value.
        """
        return next(clock_values)

    monkeypatch.setattr(os, "waitpid", fake_waitpid)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(ws_bridge, "_sleep", fake_sleep)
    monkeypatch.setattr(ws_bridge, "_monotonic", fake_monotonic)

    await _reap_tmux_attach_child(123)

    assert signals == [(123, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_fork_exec_pty_spawns_child_and_returns_pty_handle() -> None:
    """
    ``_fork_exec_pty`` forks a child that execs the binary and hands the
    parent a real pid + PTY master fd.

    Uses ``true`` (exits 0 immediately) in place of ``tmux`` so the test
    is fast and dependency-free; the fork/exec wiring under test is the
    same. A failure means the parent side of ``pty.fork`` did not return
    a usable handle — e.g. the child crashed before exec, or the master
    fd is invalid.
    """
    true_path = shutil.which("true")
    assert true_path is not None  # present on macOS and Linux CI images

    spawned = ws_bridge._fork_exec_pty(true_path, ["true"], dict(os.environ))
    try:
        # pid>0 is the parent's view of the child; a PTY master is a
        # character device, which proves pty.fork returned a real master
        # fd rather than, say, -1 or a closed descriptor.
        assert spawned.pid > 0
        assert stat.S_ISCHR(os.fstat(spawned.master_fd).st_mode)
    finally:
        await _reap_tmux_attach_child(spawned.pid)
        with contextlib.suppress(OSError):
            os.close(spawned.master_fd)


@pytest.mark.asyncio
async def test_attach_fork_gate_is_per_loop_and_bounded() -> None:
    """
    ``_attach_fork_gate`` returns one shared, bounded semaphore per loop.

    The shared instance is what serializes every attach's fork on this
    runner; the bound is what stops concurrent attaches from forking the
    multi-threaded runner in parallel. If this regressed (a fresh
    semaphore per call, or an unbounded value), the fork-storm guard
    would be silently disabled.
    """
    gate = ws_bridge._attach_fork_gate()
    # Same object on repeat calls within one loop → all attaches share
    # one serialization point.
    assert ws_bridge._attach_fork_gate() is gate

    # After acquiring the full budget the gate is locked, so the next
    # fork must wait rather than proceed in parallel.
    for _ in range(ws_bridge._MAX_CONCURRENT_ATTACH_FORKS):
        await gate.acquire()
    assert gate.locked()
    for _ in range(ws_bridge._MAX_CONCURRENT_ATTACH_FORKS):
        gate.release()
    assert not gate.locked()


def _new_tmux_session(socket_path: Path, target: str = "main") -> list[str]:
    """
    Start a detached tmux session on a private socket for bridge tests.

    :param socket_path: Path the private tmux server listens on.
    :param target: Session name, e.g. ``"main"``.
    :returns: The ``tmux -S <socket> -f /dev/null`` argv prefix, reusable
        for follow-up commands (``has-session`` / ``kill-server``).
    """
    base = ["tmux", "-S", str(socket_path), "-f", "/dev/null"]
    # ``sleep 100000`` keeps the session alive without a shell that could
    # exit on its own and race the test's assertions.
    subprocess.run(
        [*base, "new-session", "-d", "-s", target, "sleep 100000"],
        check=True,
    )
    return base


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux required")
@pytest.mark.asyncio
async def test_tmux_session_alive_tracks_real_session(tmp_path: Path) -> None:
    """
    ``_tmux_session_alive`` is ``True`` for a live session, ``False``
    once the server is killed, and ``False`` for an unknown socket.

    This is the signal the bridge uses to tell a detach (session alive)
    apart from a session exit.

    :param tmp_path: Pytest tmp directory for the private socket.
    """
    socket_path = tmp_path / "tmux.sock"
    base = _new_tmux_session(socket_path)
    try:
        assert await _tmux_session_alive(str(socket_path), "main") is True
        # A different target on a live server is still "not this session".
        assert await _tmux_session_alive(str(socket_path), "nope") is False
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, check=False)

    # Server gone → probe must report dead, not raise.
    assert await _tmux_session_alive(str(socket_path), "main") is False
    # Never-existed socket → probe reports dead (conservative).
    assert await _tmux_session_alive(str(tmp_path / "absent.sock"), "main") is False


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux required")
@pytest.mark.asyncio
async def test_tmux_session_alive_false_for_dead_pane(tmp_path: Path) -> None:
    """
    A kept-alive session whose pane process exited reads as not-alive.

    The claude-native terminal opts into ``remain-on-exit`` (#540), so a crashed
    agent leaves a dead pane on a still-present session. The probe must report
    that as gone via ``#{pane_dead}`` — otherwise a detach-vs-exit decision
    would treat the crash as a mere detach and the web client would reconnect
    to a dead pane forever instead of closing.

    :param tmp_path: Pytest tmp directory for the private socket.
    """
    socket_path = tmp_path / "tmux.sock"
    base = ["tmux", "-S", str(socket_path), "-f", "/dev/null"]
    # One command sequence on a fresh server (";" separates tmux commands):
    # set remain-on-exit globally BEFORE new-session so the session inherits it
    # and survives the inner process exiting. A bare set-option can't run first
    # on its own — no server exists yet to connect to.
    subprocess.run(
        [
            *base,
            "set-option",
            "-g",
            "remain-on-exit",
            "on",
            ";",
            "new-session",
            "-d",
            "-s",
            "main",
            "sh -c 'exit 0'",
        ],
        check=True,
    )
    try:
        for _ in range(250):
            if await _tmux_session_alive(str(socket_path), "main") is False:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - only on a hang/regression
            raise AssertionError("dead pane was never reported as not-alive")
        # The session itself is still present (remain-on-exit), proving the
        # not-alive verdict came from the dead pane, not a vanished session.
        has = subprocess.run([*base, "has-session", "-t", "main"], capture_output=True)
        assert has.returncode == 0, "session vanished; remain-on-exit was not honored"
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, check=False)


class _ParkingFakeWebSocket:
    """
    WebSocket fake whose client side never ends, isolating the PTY side.

    The bridge ends when either side finishes; this fake parks ``receive``
    forever so only the PTY-EOF branch (the ``tmux attach`` child exiting)
    can end the bridge — exactly the path a detach takes. It records the
    close code/reason and signals when the first PTY output arrives (proof
    the attach is live), so the test can detach deterministically.
    """

    def __init__(self) -> None:
        """Initialize output-seen / parking events and close capture."""
        self.first_output = asyncio.Event()
        self._parked = asyncio.Event()  # never set: parks ``receive``
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send_bytes(self, data: bytes) -> None:
        """Record that the PTY produced output (tmux attached, drawing)."""
        self.first_output.set()

    async def receive(self) -> dict[str, object]:
        """Park forever so the PTY side is what ends the bridge."""
        await self._parked.wait()
        return {"type": "websocket.disconnect"}

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Capture the bridge's chosen close code and reason."""
        self.close_code = code
        self.close_reason = reason


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux required")
@pytest.mark.asyncio
async def test_bridge_detach_closes_4405_and_leaves_session_alive(
    tmp_path: Path,
) -> None:
    """
    A tmux detach closes the attach WS with 4405, not 4404, and leaves
    the session running.

    This is the core detach case: the ``tmux attach`` child exits on
    detach (PTY EOF), but ``has-session`` still succeeds, so the bridge
    must report ``WS_CLOSE_TERMINAL_DETACHED`` — reporting 4404 here is
    what made the client tear the whole session (and runner) down.

    The detach is driven with ``tmux detach-client`` (rather than fragile
    in-band detach keystrokes). The ``tmux attach`` client registers with
    the server slightly after the bridge starts drawing, so a single
    detach attempt races that registration (flaky under load); instead we
    retry ``detach-client`` until the bridge actually ends, polling the
    external tmux server's client state rather than guessing a delay.

    :param tmp_path: Pytest tmp directory for the private socket.
    """
    socket_path = tmp_path / "tmux.sock"
    base = _new_tmux_session(socket_path)
    ws = _ParkingFakeWebSocket()
    bridge_task = asyncio.create_task(
        bridge_tmux_pty_to_websocket(
            ws,  # type: ignore[arg-type]  # structural WS fake
            socket_path=str(socket_path),
            tmux_target="main",
            read_only=False,
        )
    )

    async def _drive_detach_until_done() -> None:
        """Retry ``detach-client`` until the bridge's attach child exits."""
        while not bridge_task.done():
            # check=False: returns non-zero until a client is attached.
            subprocess.run(
                [*base, "detach-client", "-s", "main"],
                capture_output=True,
                check=False,
            )
            await asyncio.sleep(0.1)

    try:
        # Gate on first PTY output (attach is drawing), then drive detach.
        await asyncio.wait_for(ws.first_output.wait(), timeout=10.0)
        driver = asyncio.create_task(_drive_detach_until_done())
        try:
            await asyncio.wait_for(bridge_task, timeout=10.0)
        finally:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver

        assert ws.close_code == WS_CLOSE_TERMINAL_DETACHED, (
            f"detach should close with 4405, got {ws.close_code}; a 4404 here "
            "makes the client end the session and kill the still-live runner"
        )
        assert await _tmux_session_alive(str(socket_path), "main") is True, (
            "detach must leave the tmux session (and Claude) running"
        )
    finally:
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task
        subprocess.run([*base, "kill-server"], capture_output=True, check=False)


class _CollectingFakeWebSocket(_ParkingFakeWebSocket):
    """
    Parking WebSocket fake that also records every PTY output chunk.

    Extends :class:`_ParkingFakeWebSocket` (client side parks forever,
    close code captured) with an output buffer so tests can assert on
    the actual bytes the attach client drew — e.g. that real pane
    content arrived rather than a tmux startup error.
    """

    def __init__(self) -> None:
        """Initialize the output buffer alongside the parent's events."""
        super().__init__()
        self.output = bytearray()
        self.output_changed = asyncio.Event()

    async def send_bytes(self, data: bytes) -> None:
        """Append the chunk and signal waiters polling for content."""
        self.output.extend(data)
        self.output_changed.set()
        await super().send_bytes(data)


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux required")
@pytest.mark.asyncio
async def test_bridge_attach_renders_pane_with_dumb_ambient_term(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The attach client draws pane content even when the bridging
    process's ambient ``TERM`` is unusable.

    This is the managed-sandbox / ``sandbox connect`` condition: a
    headless host has no controlling terminal, bash substitutes
    ``TERM=dumb``, and the runner inherits it. The bridge must pin the
    ``tmux attach`` child's ``TERM`` to the real client's terminal
    type (xterm.js ⇒ ``xterm-256color``) rather than inherit the
    ambient value — ``tmux attach`` refuses dumb terminals ("terminal
    does not support clear"), which rendered the web terminal as an
    error loop instead of the Claude TUI.

    The pane marker arriving over the WS proves the attach client ran
    with a usable TERM. If the env pin regresses, tmux prints its
    "open terminal failed" error and exits — the marker never arrives
    and this test times out at the wait below.

    :param tmp_path: Pytest tmp directory for the private socket.
    :param monkeypatch: Used to set the ambient ``TERM=dumb``.
    """
    # The sandbox condition: every process in the chain (including the
    # tmux server about to be spawned) sees a dumb TERM.
    monkeypatch.setenv("TERM", "dumb")

    socket_path = tmp_path / "tmux.sock"
    base = ["tmux", "-S", str(socket_path), "-f", "/dev/null"]
    marker = "BRIDGE_TERM_PIN_OK"
    subprocess.run(
        [*base, "new-session", "-d", "-s", "main", f"echo {marker}; sleep 100000"],
        check=True,
    )

    ws = _CollectingFakeWebSocket()
    bridge_task = asyncio.create_task(
        bridge_tmux_pty_to_websocket(
            ws,  # type: ignore[arg-type]  # structural WS fake
            socket_path=str(socket_path),
            tmux_target="main",
            read_only=False,
        )
    )
    try:

        async def _wait_for_marker() -> None:
            """Wait until the pane marker shows up in the WS output."""
            while marker.encode() not in ws.output:
                ws.output_changed.clear()
                await ws.output_changed.wait()

        # Marker in the attach redraw proves a usable client TERM: with
        # the pin regressed, tmux rejects the dumb terminal and the only
        # output is its "open terminal failed" error before PTY EOF.
        await asyncio.wait_for(_wait_for_marker(), timeout=10.0)
        assert b"open terminal failed" not in ws.output, (
            "tmux rejected the attach client's terminal type — the bridge "
            "leaked the ambient dumb TERM instead of pinning xterm-256color"
        )
        # The attach client is still alive (the bridge ends on PTY EOF,
        # which is what the pre-fix failure produced immediately).
        assert not bridge_task.done(), (
            "attach child exited right after drawing — with a usable TERM "
            "it stays attached until detach/kill"
        )
    finally:
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task
        subprocess.run([*base, "kill-server"], capture_output=True, check=False)


# ── read_only input gating ────────────────────────────────────────


class _ScriptedWebSocket:
    """
    WebSocket fake that feeds the bridge a fixed list of inbound frames.

    Each ``receive()`` pops the next scripted frame; once the script is
    exhausted it sets :attr:`exhausted` and parks forever so the bridge's
    ``_ws_to_pty`` task cannot finish on its own. Because the bridge
    processes each frame to completion before looping back to
    ``receive()``, ``exhausted`` being set is a deterministic barrier:
    every scripted frame has been fully applied (a keystroke either
    written to or dropped before the PTY, a resize ``ioctl`` already
    issued) by the time it fires — no sleeps or polling required.

    :param frames: ASGI-style inbound frame dicts, e.g.
        ``{"type": "websocket.receive", "bytes": b"x"}``.
    """

    def __init__(self, frames: list[dict[str, object]]) -> None:
        self._frames = list(frames)
        self.exhausted = asyncio.Event()
        self._park = asyncio.Event()  # never set: parks ``receive``
        self.sent: list[bytes] = []
        self.close_code: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        """Record PTY output forwarded to the browser (unused here)."""
        self.sent.append(data)

    async def receive(self) -> dict[str, object]:
        """Pop the next scripted frame, then park once the script is done."""
        if self._frames:
            return self._frames.pop(0)
        self.exhausted.set()
        await self._park.wait()
        return {"type": "websocket.disconnect"}  # unreachable

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Capture the bridge's chosen close code."""
        self.close_code = code


def _slave_winsize(slave_fd: int) -> tuple[int, int]:
    """Return ``(rows, cols)`` currently set on the PTY *slave_fd*."""
    packed = fcntl.ioctl(slave_fd, termios.TIOCGWINSZ, b"\x00" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    return rows, cols


def _drain(fd: int) -> bytes:
    """Read all immediately-available bytes from non-blocking *fd*."""
    out = b""
    while True:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return out
        if not chunk:
            return out
        out += chunk


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.asyncio
async def test_bridge_read_only_gates_keystrokes_but_not_resize(
    read_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``read_only`` drops browser keystrokes at the app layer but still
    applies resize control frames.

    This pins the application-level half of the read-only contract a
    read-only collaborator relies on. The runner attach tests only check
    that ``tmux attach -r`` is spawned (the tmux-level defense); nothing
    exercised the in-process ``elif data is not None and not read_only``
    guard. A regression that dropped ``not read_only`` would still pass
    those tests yet let a read-only viewer type into the shared pane.

    Drives ``bridge_tmux_pty_to_websocket`` directly over a real
    ``os.openpty`` pair (the bridge writes browser bytes to the master;
    they surface as input on the slave). Two inbound frames are scripted:
    a keystroke, then a resize. The ``_ScriptedWebSocket.exhausted`` event
    is the synchronization barrier — once set, both frames have been
    fully processed, so the assertions observe a settled PTY state.

    :param read_only: Whether the bridge runs in read-only mode.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    marker = b"rm -rf /\n"
    resize_cols, resize_rows = 137, 41

    master_fd, slave_fd = os.openpty()
    # Raw + non-blocking slave: keystrokes written to the master arrive
    # verbatim (no canonical line buffering, no echo) and reads never
    # block while we drain.
    tty.setraw(slave_fd)
    flags = fcntl.fcntl(slave_fd, fcntl.F_GETFL)
    fcntl.fcntl(slave_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # ``pty.fork`` is stubbed to the parent branch: hand the bridge our
    # pre-made master fd and a bogus pid. ``os.kill`` is neutralized so
    # the teardown SIGTERM to that pid can't hit an unrelated process,
    # and the detach probe is forced negative so teardown doesn't shell
    # out to a real tmux.
    monkeypatch.setattr(pty, "fork", lambda: (999_999, master_fd))
    monkeypatch.setattr(os, "kill", lambda *_a, **_k: None)

    async def _dead_session(*_a: object, **_k: object) -> bool:
        return False

    monkeypatch.setattr(ws_bridge, "_tmux_session_alive", _dead_session)

    ws = _ScriptedWebSocket(
        [
            {"type": "websocket.receive", "bytes": marker},
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "resize", "cols": resize_cols, "rows": resize_rows}),
            },
        ]
    )

    bridge_task = asyncio.create_task(
        bridge_tmux_pty_to_websocket(
            ws,  # type: ignore[arg-type]  # structural WS fake
            socket_path="/nonexistent.sock",
            tmux_target="main",
            read_only=read_only,
        )
    )
    try:
        # Barrier: both scripted frames have been applied to the PTY.
        await asyncio.wait_for(ws.exhausted.wait(), timeout=5.0)

        # Resize is applied regardless of read_only — a read-only viewer
        # must still be able to size their own viewport. If this fails,
        # the resize branch was wrongly gated behind the read_only check
        # (or the ioctl never ran).
        assert _slave_winsize(slave_fd) == (resize_rows, resize_cols), (
            f"resize frame not applied to the PTY in "
            f"read_only={read_only} mode; got {_slave_winsize(slave_fd)}, "
            f"expected {(resize_rows, resize_cols)}."
        )

        # The load-bearing half: keystrokes reach the PTY only when NOT
        # read_only. In read_only the bytes must be dropped before the
        # write — a non-empty drain here means a read-only collaborator
        # could execute commands in the shared terminal.
        delivered = _drain(slave_fd)
        if read_only:
            assert delivered == b"", (
                f"read_only bridge forwarded browser keystrokes to the "
                f"PTY: {delivered!r}. The app-level "
                f"'elif data is not None and not read_only' drop "
                f"regressed; a read-only viewer can now type."
            )
        else:
            assert delivered == marker, (
                f"read-write bridge did not forward keystrokes verbatim; "
                f"PTY received {delivered!r}, expected {marker!r}."
            )
    finally:
        # Closing the slave gives the master read EOF, ending the bridge
        # cleanly; the bridge closes the master fd itself in its finally.
        with contextlib.suppress(OSError):
            os.close(slave_fd)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(bridge_task), timeout=5.0)
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task
        with contextlib.suppress(OSError):
            os.close(master_fd)


@pytest.mark.asyncio
async def test_bridge_stamps_on_client_interaction_for_each_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The bridge fires ``on_client_interaction`` on connect, every inbound
    frame, and disconnect.

    This is the signal the runner's idle watcher uses to discount
    client-driven repaints (attach/detach reflow, focus, mouse, keystroke,
    resize) so they don't read as agent activity. A regression that
    stopped stamping any of these would let those repaints flip the
    session to "running"; this pins that connect, each frame, and the exit
    (disconnect) all stamp.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    stamps: list[int] = []
    after_frames = 0

    master_fd, slave_fd = os.openpty()
    tty.setraw(slave_fd)
    flags = fcntl.fcntl(slave_fd, fcntl.F_GETFL)
    fcntl.fcntl(slave_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    # Hand the bridge our pre-made master fd (parent branch of pty.fork),
    # neutralize the teardown SIGTERM, and force the detach probe negative
    # so teardown doesn't shell out to a real tmux.
    monkeypatch.setattr(pty, "fork", lambda: (999_999, master_fd))
    monkeypatch.setattr(os, "kill", lambda *_a, **_k: None)

    async def _dead_session(*_a: object, **_k: object) -> bool:
        return False

    monkeypatch.setattr(ws_bridge, "_tmux_session_alive", _dead_session)

    ws = _ScriptedWebSocket(
        [
            {"type": "websocket.receive", "bytes": b"x"},  # keystroke
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "resize", "cols": 100, "rows": 30}),
            },
        ]
    )

    bridge_task = asyncio.create_task(
        bridge_tmux_pty_to_websocket(
            ws,  # type: ignore[arg-type]  # structural WS fake
            socket_path="/nonexistent.sock",
            tmux_target="main",
            read_only=False,
            on_client_interaction=lambda: stamps.append(1),
        )
    )
    try:
        # Barrier: both scripted frames have been applied.
        await asyncio.wait_for(ws.exhausted.wait(), timeout=5.0)
        # connect (1) + keystroke frame (1) + resize frame (1) = 3 so far.
        after_frames = len(stamps)
        assert after_frames >= 3, (
            f"expected >=3 interaction stamps (connect + 2 frames), got "
            f"{after_frames}; connect-entry or per-frame stamping regressed"
        )
    finally:
        # Close the slave → master EOF → bridge exits → its finally stamps
        # the disconnect.
        with contextlib.suppress(OSError):
            os.close(slave_fd)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(bridge_task), timeout=5.0)
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task
        with contextlib.suppress(OSError):
            os.close(master_fd)

    # The bridge's finally stamps once more on exit (disconnect). Without
    # it, the detach reflow (tmux resizing back) would read as activity.
    assert len(stamps) > after_frames, (
        f"disconnect/exit did not stamp an interaction; stayed at "
        f"{after_frames}. Detach reflow would then read as running."
    )


@pytest.mark.asyncio
async def test_bridge_splits_pty_redraw_after_control_key_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The full attach bridge uses the interactive cap after control-key input.

    Cursor movement and deletion arrive from xterm as small binary frames
    (for example left-arrow ``ESC [ D`` and backspace ``DEL``). Codex then
    redraws the prompt through the PTY. This pins the end-to-end bridge
    behavior that keeps that redraw eligible for the browser's synchronous
    echo path; testing ``_forward_pty_to_ws`` alone would not catch a
    regression that stopped stamping binary input in
    ``bridge_tmux_pty_to_websocket``.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    master_fd, slave_fd = os.openpty()
    tty.setraw(slave_fd)
    flags = fcntl.fcntl(slave_fd, fcntl.F_GETFL)
    fcntl.fcntl(slave_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    monkeypatch.setattr(pty, "fork", lambda: (999_999, master_fd))
    monkeypatch.setattr(os, "kill", lambda *_a, **_k: None)
    monkeypatch.setattr(ws_bridge, "_monotonic", lambda: 100.0)

    async def _dead_session(*_a: object, **_k: object) -> bool:
        """
        Keep bridge teardown from shelling out to tmux.

        :param _a: Ignored positional arguments.
        :param _k: Ignored keyword arguments.
        :returns: ``False`` so teardown treats the fake session as gone.
        """
        return False

    async def _wait_for_sent_bytes(ws: _ScriptedWebSocket, expected_len: int) -> None:
        """
        Wait until the fake websocket records *expected_len* output bytes.

        :param ws: Fake websocket that records outbound PTY frames.
        :param expected_len: Total byte count to wait for.
        :returns: None.
        """
        while sum(len(frame) for frame in ws.sent) < expected_len:
            await asyncio.sleep(0)

    monkeypatch.setattr(ws_bridge, "_tmux_session_alive", _dead_session)
    redraw = b"r" * (ws_bridge._PTY_READ_CHUNK + 17)
    ws = _ScriptedWebSocket(
        [
            # Left arrow + backspace: representative cursor/editing controls.
            {"type": "websocket.receive", "bytes": b"\x1b[D\x7f"},
        ]
    )

    bridge_task = asyncio.create_task(
        bridge_tmux_pty_to_websocket(
            ws,  # type: ignore[arg-type]  # structural WS fake
            socket_path="/nonexistent.sock",
            tmux_target="main",
            read_only=False,
        )
    )
    try:
        await asyncio.wait_for(ws.exhausted.wait(), timeout=5.0)
        os.write(slave_fd, redraw)
        await asyncio.wait_for(_wait_for_sent_bytes(ws, len(redraw)), timeout=5.0)

        assert all(
            len(frame) <= ws_bridge._INTERACTIVE_WS_COALESCE_MAX_BYTES for frame in ws.sent
        ), (
            f"post-input redraw frames exceeded the browser sync-echo cap: "
            f"{[len(frame) for frame in ws.sent]}."
        )
        assert b"".join(ws.sent) == redraw
    finally:
        with contextlib.suppress(OSError):
            os.close(slave_fd)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(bridge_task), timeout=5.0)
        bridge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bridge_task
        with contextlib.suppress(OSError):
            os.close(master_fd)


class _RecordingWebSocket:
    """
    WebSocket fake recording outbound binary frames for assertions.

    Only ``send_bytes`` (the surface ``_forward_pty_to_ws`` uses) is
    implemented, so ``len(sent)`` is the exact frame count and
    ``b"".join(sent)`` the exact byte stream. A real stub, not
    ``MagicMock``, so any unexpected attribute access fails loudly.

    :ivar sent: Binary frames in send order.
    """

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        """Record one outbound binary frame."""
        self.sent.append(data)


@pytest.mark.asyncio
async def test_forward_pty_to_ws_coalesces_queued_burst() -> None:
    """
    A burst already queued is sent as one coalesced frame, byte-exact.

    ``_on_pty_readable`` enqueues the PTY master in 4 KiB reads, so a screen
    redraw / large paste lands as ~10 queue items. Pre-filling the queue
    with that shape (ten 4 KiB chunks + ``None`` EOF) reproduces the burst
    deterministically — a real PTY's ~12 KiB kernel buffer would cap how
    much is ever queued at once. Before, each item was its own WS frame.

    Hitting ``None`` mid-merge also exercises the EOF-after-flush path, so
    leftover sentinels or dropped tail bytes would be caught here.
    """
    chunk_size = ws_bridge._PTY_READ_CHUNK
    num_chunks = 10
    # Distinct per-chunk fill so a reorder/drop changes the joined bytes.
    chunks = [bytes([i]) * chunk_size for i in range(num_chunks)]
    expected = b"".join(chunks)
    assert len(expected) == 40 * 1024  # 40 KiB, the goal's burst size

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    for chunk in chunks:
        queue.put_nowait(chunk)
    queue.put_nowait(None)  # EOF sentinel, as _on_pty_readable enqueues on read EOF

    ws = _RecordingWebSocket()
    await asyncio.wait_for(
        _forward_pty_to_ws(ws, queue),  # type: ignore[arg-type]  # structural WS fake
        timeout=5.0,
    )

    # 40 KiB (< 64 KiB cap) -> one frame; 10 frames = drain regressed.
    assert len(ws.sent) == 1, (
        f"expected the queued 40 KiB burst to coalesce into one WS "
        f"frame, got {len(ws.sent)} (sizes {[len(f) for f in ws.sent]}). "
        f"More frames means the get_nowait coalescing drain regressed to "
        f"one-frame-per-chunk."
    )
    # Byte-exact: every input byte arrives once and in order, else xterm.js corrupts.
    assert b"".join(ws.sent) == expected, (
        "coalesced frames are not byte-exact with the queued input; "
        "the merge dropped, duplicated, or reordered PTY bytes."
    )
    # EOF must be consumed by the terminating path; leftover None = bug.
    assert queue.empty(), (
        "queue not fully drained after EOF; the None sentinel was not "
        "consumed by the terminating path."
    )


@pytest.mark.asyncio
async def test_forward_pty_to_ws_single_write_sends_immediately_one_frame() -> None:
    """
    A lone chunk is sent immediately as one frame — zero added latency.

    Coalescing must never wait to accumulate: a single keystroke echo has
    to flush the instant it is queued. Enqueue one chunk with NO EOF yet —
    if the forwarder waited for more, ``sent`` stays empty and ``wait_for``
    times out. The frame appearing proves the first ``get`` flushes only
    the available bytes and the ``get_nowait`` drain stops without blocking.
    """
    keystroke = b"x"
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    ws = _RecordingWebSocket()
    task = asyncio.create_task(
        _forward_pty_to_ws(ws, queue)  # type: ignore[arg-type]  # structural WS fake
    )
    try:
        queue.put_nowait(keystroke)

        # Poll for the frame; if coalescing wrongly blocked for more, this times out.
        async def _one_frame_sent() -> None:
            while not ws.sent:
                await asyncio.sleep(0)

        await asyncio.wait_for(_one_frame_sent(), timeout=5.0)

        # One keystroke in one frame — not held back, nothing to merge, no latency added.
        assert ws.sent == [keystroke], (
            f"expected a lone keystroke to be sent as exactly one frame "
            f"{[keystroke]!r}, got {ws.sent!r}. If empty, coalescing "
            f"blocked waiting to accumulate (added latency); if more, an "
            f"extra frame leaked."
        )
    finally:
        queue.put_nowait(None)  # let the forwarder observe EOF and return
        await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_forward_pty_to_ws_caps_coalesced_frame_size() -> None:
    """
    Coalescing stops at *max_coalesce_bytes* so a huge burst still streams.

    Without a cap a multi-megabyte burst would buffer into one giant frame
    and stall first-paint. With a burst (16 KiB) larger than the cap (8 KiB),
    the forwarder must emit several ~cap-sized frames, not one — still
    delivering every byte exactly.
    """
    chunk_size = ws_bridge._PTY_READ_CHUNK  # 4 KiB
    cap = 2 * chunk_size  # 8 KiB cap
    chunks = [bytes([i]) * chunk_size for i in range(4)]  # 16 KiB total
    expected = b"".join(chunks)

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    for chunk in chunks:
        queue.put_nowait(chunk)
    queue.put_nowait(None)

    ws = _RecordingWebSocket()
    await asyncio.wait_for(
        _forward_pty_to_ws(ws, queue, max_coalesce_bytes=cap),  # type: ignore[arg-type]
        timeout=5.0,
    )

    # 16 KiB over an 8 KiB cap must span >1 frame; 1 means the cap check was dropped.
    assert len(ws.sent) > 1, (
        f"expected a 16 KiB burst to span multiple frames under an 8 KiB "
        f"cap, got {len(ws.sent)} frame(s). The max_coalesce_bytes guard "
        f"was not honored."
    )
    # Hard cap: no WebSocket frame should exceed the requested cap.
    assert all(len(f) <= cap for f in ws.sent), (
        f"a coalesced frame exceeded the cap budget; sizes "
        f"{[len(f) for f in ws.sent]} (cap {cap})."
    )
    # Capping must not lose data: every byte still arrives in order.
    assert b"".join(ws.sent) == expected, (
        "byte stream corrupted by the cap split; frames are not the "
        "concatenation of the input chunks."
    )


@pytest.mark.asyncio
async def test_forward_pty_to_ws_honors_small_cap_without_overshoot() -> None:
    """
    A small cap is a hard frame-size boundary, even for one large PTY read.

    The browser terminal's synchronous echo fast path only applies to
    frames at or below the interactive cap. If a 4 KiB PTY read is allowed
    to overshoot a 2 KiB cap, Codex prompt redraws after typing fall back
    to xterm's queued async ``write`` path and the user sees per-key
    latency. The bridge must split, not overshoot, while preserving byte
    order.
    """
    cap = 2048
    chunks = [b"a" * ws_bridge._PTY_READ_CHUNK, b"b" * 1024]
    expected = b"".join(chunks)

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    for chunk in chunks:
        queue.put_nowait(chunk)
    queue.put_nowait(None)

    ws = _RecordingWebSocket()
    await asyncio.wait_for(
        _forward_pty_to_ws(ws, queue, max_coalesce_bytes=cap),  # type: ignore[arg-type]
        timeout=5.0,
    )

    assert all(len(frame) <= cap for frame in ws.sent), (
        f"interactive frames exceeded the browser sync-echo cap: "
        f"{[len(frame) for frame in ws.sent]} > {cap}. Oversized frames "
        f"bypass xterm writeSync and reintroduce typing latency."
    )
    assert b"".join(ws.sent) == expected, (
        "splitting interactive frames changed the byte stream; xterm must "
        "receive exactly the PTY bytes in order."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, -1])
async def test_forward_pty_to_ws_rejects_non_positive_coalesce_cap(cap: int) -> None:
    """
    Invalid frame caps fail loud instead of being silently substituted.

    A zero or negative cap would make the bridge's frame-splitting contract
    nonsensical. Clamping it to an invented fallback would hide the bad
    caller and make latency behavior harder to reason about.

    :param cap: Invalid frame cap to test, e.g. ``0``.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    queue.put_nowait(b"x")

    ws = _RecordingWebSocket()
    with pytest.raises(ValueError, match="max_coalesce_bytes must be positive"):
        await asyncio.wait_for(
            _forward_pty_to_ws(ws, queue, max_coalesce_bytes=cap),  # type: ignore[arg-type]
            timeout=0.1,
        )

    assert ws.sent == []


@pytest.mark.asyncio
async def test_forward_pty_to_ws_accepts_dynamic_coalesce_cap() -> None:
    """
    The coalesce cap can be supplied dynamically per send.

    ``bridge_tmux_pty_to_websocket`` needs the normal 64 KiB cap for
    output floods, but a 2 KiB cap shortly after client input so prompt
    redraws stay eligible for the browser's synchronous echo path.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    queue.put_nowait(b"x" * ws_bridge._PTY_READ_CHUNK)
    queue.put_nowait(None)

    ws = _RecordingWebSocket()

    def _dynamic_cap() -> int:
        """
        Shrink the frame cap after the first frame is sent.

        :returns: ``2048`` for the first frame, then ``512`` for later frames.
        """
        return 2048 if not ws.sent else 512

    await asyncio.wait_for(
        _forward_pty_to_ws(
            ws,  # type: ignore[arg-type]
            queue,
            max_coalesce_bytes=_dynamic_cap,
        ),
        timeout=5.0,
    )

    assert [len(frame) for frame in ws.sent] == [2048, 512, 512, 512, 512], (
        f"dynamic interactive cap was not honored per frame; frame sizes "
        f"were {[len(frame) for frame in ws.sent]}."
    )
    assert b"".join(ws.sent) == b"x" * ws_bridge._PTY_READ_CHUNK


# ── Additional unit tests (no tmux required) ─────────────────


def test_current_coalesce_limit_static_value() -> None:
    """
    ``_current_coalesce_limit`` returns the integer unchanged when
    given a static cap.
    """
    from omnigent.terminals.ws_bridge import _current_coalesce_limit

    assert _current_coalesce_limit(4096) == 4096


def test_current_coalesce_limit_callable_invoked() -> None:
    """
    ``_current_coalesce_limit`` calls the callable and returns its
    result when given a dynamic cap.
    """
    from omnigent.terminals.ws_bridge import _current_coalesce_limit

    assert _current_coalesce_limit(lambda: 2048) == 2048


def test_current_coalesce_limit_rejects_zero() -> None:
    """
    A zero cap raises ``ValueError`` — a zero cap would make the
    frame-splitting loop infinite.
    """
    from omnigent.terminals.ws_bridge import _current_coalesce_limit

    with pytest.raises(ValueError, match="must be positive"):
        _current_coalesce_limit(0)


def test_current_coalesce_limit_rejects_negative_callable() -> None:
    """
    A callable returning a negative value raises ``ValueError``.
    """
    from omnigent.terminals.ws_bridge import _current_coalesce_limit

    with pytest.raises(ValueError, match="must be positive"):
        _current_coalesce_limit(lambda: -5)


def test_coalesce_limit_after_input_returns_large_cap_with_no_input() -> None:
    """
    Before any client input (``last_client_input_at=None``), the
    coalesce cap is the full flood cap — output should stream
    efficiently without the interactive constraint.
    """
    from omnigent.terminals.ws_bridge import _coalesce_limit_after_input

    assert _coalesce_limit_after_input(None) == ws_bridge._WS_COALESCE_MAX_BYTES


def test_coalesce_limit_after_input_returns_small_cap_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the last client input was within the interactive echo window,
    the cap shrinks to the interactive size so browser sync-echo works.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.terminals.ws_bridge import _coalesce_limit_after_input

    monkeypatch.setattr(ws_bridge, "_monotonic", lambda: 100.5)
    # Input was 0.3s ago (within the 0.75s window).
    assert _coalesce_limit_after_input(100.2) == ws_bridge._INTERACTIVE_WS_COALESCE_MAX_BYTES


def test_coalesce_limit_after_input_returns_large_cap_after_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Once the interactive echo window has elapsed, the cap reverts to
    the full flood cap so output floods stream efficiently again.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.terminals.ws_bridge import _coalesce_limit_after_input

    monkeypatch.setattr(ws_bridge, "_monotonic", lambda: 102.0)
    # Input was 2s ago (well past the 0.75s window).
    assert _coalesce_limit_after_input(100.0) == ws_bridge._WS_COALESCE_MAX_BYTES


@pytest.mark.asyncio
async def test_tmux_session_alive_returns_false_on_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_tmux_session_alive`` returns ``False`` when ``create_subprocess_exec``
    raises ``OSError`` (tmux binary not found). This is the conservative
    fallback — any probe error is treated as "session dead".

    :param monkeypatch: Pytest monkeypatch fixture.
    """

    async def _raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError("No such file or directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_oserror)
    assert await _tmux_session_alive("/no/such.sock", "main") is False


@pytest.mark.asyncio
async def test_tmux_session_alive_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_tmux_session_alive`` returns ``False`` when the has-session probe
    times out (e.g. a wedged tmux server). The probe must not block the
    bridge's teardown path.

    :param monkeypatch: Pytest monkeypatch fixture.
    """

    class _HangingProcess:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(999)
            return b"", b""

        def kill(self) -> None:
            pass

    async def _create_hanging(*args: object, **kwargs: object) -> _HangingProcess:
        return _HangingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_hanging)
    # Patch the timeout to a tiny value so the test finishes quickly.
    monkeypatch.setattr(ws_bridge, "_TMUX_HAS_SESSION_TIMEOUT_S", 0.01)

    assert await _tmux_session_alive("/some.sock", "main") is False


@pytest.mark.asyncio
async def test_bridge_closes_ws_with_error_when_tmux_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``shutil.which("tmux")`` returns ``None``, the bridge closes
    the websocket with ``WS_CLOSE_INTERNAL_ERROR`` and returns without
    crashing.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    close_codes: list[int] = []

    class _FakeWS:
        async def close(self, code: int = 1000, reason: str = "") -> None:
            close_codes.append(code)

    ws = _FakeWS()
    await bridge_tmux_pty_to_websocket(
        ws,  # type: ignore[arg-type]
        socket_path="/nonexistent.sock",
        tmux_target="main",
        read_only=False,
    )

    assert close_codes == [ws_bridge.WS_CLOSE_INTERNAL_ERROR]


@pytest.mark.asyncio
async def test_reap_child_handles_child_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_reap_tmux_attach_child`` exits cleanly when ``os.waitpid``
    raises ``ChildProcessError`` (the child was already reaped by
    another path, e.g. a signal handler).

    :param monkeypatch: Pytest monkeypatch fixture.
    """

    def _raise_child_error(pid: int, options: int) -> tuple[int, int]:
        raise ChildProcessError("No child processes")

    monkeypatch.setattr(os, "waitpid", _raise_child_error)

    # Should return without raising.
    await _reap_tmux_attach_child(12345)


@pytest.mark.asyncio
async def test_forward_pty_to_ws_handles_empty_queue_then_eof() -> None:
    """
    When the queue receives ``None`` (EOF) as the very first item,
    ``_forward_pty_to_ws`` returns immediately without sending
    anything. This covers the path where the tmux attach child
    exits before producing any output.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    queue.put_nowait(None)

    ws = _RecordingWebSocket()
    await asyncio.wait_for(
        _forward_pty_to_ws(ws, queue),  # type: ignore[arg-type]
        timeout=5.0,
    )

    assert ws.sent == []


def test_ws_close_code_constants() -> None:
    """
    The WebSocket close codes are in the RFC 6455 application range
    (4000-4999) and are distinct from each other.
    """
    codes = {
        ws_bridge.WS_CLOSE_TERMINAL_NOT_FOUND,
        ws_bridge.WS_CLOSE_TERMINAL_DETACHED,
        ws_bridge.WS_CLOSE_INTERNAL_ERROR,
    }
    assert len(codes) == 3, "close codes must be distinct"
    for code in codes:
        assert 4000 <= code <= 4999, f"close code {code} outside RFC 6455 app range"


def test_monotonic_returns_float() -> None:
    """
    ``_monotonic`` is a thin wrapper over ``time.monotonic`` that
    returns a float. This seems trivial but the wrapper exists as
    a test seam — verify it works as documented.
    """
    from omnigent.terminals.ws_bridge import _monotonic

    val = _monotonic()
    assert isinstance(val, float)
    # Monotonic: a second call should be >= the first.
    assert _monotonic() >= val
