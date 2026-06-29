"""Shared PTY ↔ WebSocket bridge for tmux ``attach`` sessions.

Lives under :mod:`omnigent.terminals` (alongside the
:class:`TerminalRegistry`) because both the server and the runner
need the bridge and neither one "owns" it. Importing from a neutral
location avoids a runner → ``server.routes`` dependency.

Used by:

- :mod:`omnigent.server.routes.terminal_attach`, when no runner WS
  factory is configured (in-process / test setups, where the tmux
  socket lives on the same filesystem the server can see).
- :mod:`omnigent.runner.app`, where the runner exposes its own WS
  attach endpoint so out-of-process runners run ``tmux attach``
  locally and never expose a socket path to the server.

Wire protocol (same as the original server route):

- **Server → client**: every PTY read becomes a *binary* WS frame.
- **Client → server**:
    - **Text frames** are JSON control messages. Currently only
      ``{"type": "resize", "cols": N, "rows": M}`` (applied via
      ``ioctl(TIOCSWINSZ)``); unknown shapes are ignored for
      forward-compat.
    - **Binary frames** are raw input bytes written to the PTY.
      Dropped silently when ``read_only`` is true.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import struct
import sys
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

# fcntl/pty/termios are POSIX-only. This module drives tmux PTY ``attach``
# sessions, a feature that is disabled on Windows (see the terminal
# entrypoints), so importing it must not crash the server there. The
# ``sys.platform`` guard is special-cased by mypy, which type-checks on Linux
# and therefore still sees the real modules.
if sys.platform != "win32":
    import fcntl
    import pty
    import termios

from fastapi import WebSocket, WebSocketDisconnect

_logger = logging.getLogger(__name__)

# 4 KiB matches tmux's own copy/redraw paths and is a good fit for
# typical terminal output bursts; bigger reads add latency before the
# first byte hits xterm.js, smaller reads add syscall overhead without
# measurable interactivity gains.
_PTY_READ_CHUNK: Final[int] = 4096

# Default per-frame cap: merge queued PTY chunks into bounded sends so
# huge bursts stream.
_WS_COALESCE_MAX_BYTES: Final[int] = 64 * 1024
# Keep these in sync with web's SYNC_ECHO_* constants so the server
# emits frames the browser is still willing to write synchronously after
# input.
_INTERACTIVE_WS_COALESCE_MAX_BYTES: Final[int] = 2048
_INTERACTIVE_ECHO_WINDOW_S: Final[float] = 0.75

_TMUX_ATTACH_WAIT_GRACE_S: Final[float] = 0.5
_TMUX_ATTACH_WAIT_POLL_S: Final[float] = 0.02

# Application-level WebSocket close codes (RFC 6455 reserves 4xxx).
# 4404 tells the client's reconnect loop to stop — sent on a
# pre-attach lookup miss and on PTY EOF when the tmux session is
# genuinely gone (Claude exited / the session was killed).
WS_CLOSE_TERMINAL_NOT_FOUND: Final[int] = 4404
# 4405 means the user *detached* from tmux: the ``tmux attach`` child
# exited (PTY EOF) but the session is still alive. The client must NOT
# treat this as a terminal-gone exit: a detach misread as 4404 would
# tear the whole session (and runner) down.
WS_CLOSE_TERMINAL_DETACHED: Final[int] = 4405
WS_CLOSE_INTERNAL_ERROR: Final[int] = 4500

# A ``tmux has-session`` liveness probe is local and near-instant; cap
# it so a wedged tmux server can't stall the bridge's teardown.
_TMUX_HAS_SESSION_TIMEOUT_S: Final[float] = 2.0

# Bound concurrent ``pty.fork`` calls across the whole process. The
# runner is multi-threaded (asyncio's default executor, the
# parent-death watchdog, uvicorn), and forking a multi-threaded process
# is delicate: the child must reach ``exec`` doing as little as possible,
# because any work that needs a lock another thread held at fork time can
# deadlock — on macOS this surfaces as a hard crash of the forked child.
# Serializing fork+exec keeps at most one fork "in flight" so concurrent
# terminal attaches (the web Terminals tab used to open one per terminal
# at once) cannot pile forks on top of each other. 1 is deliberately
# conservative; raise only with evidence that parallel forks are safe.
_MAX_CONCURRENT_ATTACH_FORKS: Final[int] = 1

# One semaphore per event loop. The runner has exactly one loop; tests
# create several, so key by loop and hold weakly to let finished loops be
# collected. A module-global semaphore would bind futures to whichever
# loop first blocked on it and break under multiple test loops.
_attach_fork_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _attach_fork_gate() -> asyncio.Semaphore:
    """
    Return the running loop's PTY-fork serialization semaphore.

    Created lazily on first use per event loop and bounded to
    :data:`_MAX_CONCURRENT_ATTACH_FORKS`. Callers acquire it around the
    ``pty.fork`` + ``exec`` so no two attaches fork the runner at once.

    :returns: The :class:`asyncio.Semaphore` for the current loop.
    """
    loop = asyncio.get_running_loop()
    gate = _attach_fork_gates.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(_MAX_CONCURRENT_ATTACH_FORKS)
        _attach_fork_gates[loop] = gate
    return gate


@dataclass(frozen=True)
class _SpawnedPty:
    """
    The parent-side handle to a ``tmux attach`` child on a fresh PTY.

    :param pid: Child process id (the ``tmux attach`` client), e.g.
        ``54321``. Used to signal (SIGTERM) and reap the child.
    :param master_fd: Parent-side PTY master file descriptor the bridge
        reads tmux output from and writes keystrokes to.
    """

    pid: int
    master_fd: int


# Terminal type advertised to tmux for the attach client. The far end
# of this bridge is always an xterm.js-compatible emulator (the web
# terminal or the REPL's embedded terminal), never the bridging
# process's own controlling terminal — so its capabilities, not the
# ambient ``TERM``, describe the client. Inheriting ambient ``TERM``
# breaks headless hosts (managed sandboxes, ``omnigent sandbox
# connect``): no TTY means no TERM, bash substitutes ``TERM=dumb``,
# and ``tmux attach`` refuses dumb terminals ("terminal does not
# support clear") — the web terminal renders that error instead of
# the pane.
_ATTACH_CLIENT_TERM = "xterm-256color"


def _fork_exec_pty(tmux_path: str, argv: list[str], env: dict[str, str]) -> _SpawnedPty:
    """
    Fork a child that ``execve``s *argv* on a new pseudo-terminal.

    Runs the whole fork+exec so the child path never returns into Python
    callers (it ``exec``s, or ``_exit``s on exec failure). Designed to be
    invoked via :func:`asyncio.to_thread` so the (page-table-copying)
    fork of a large runner process does not stall the event loop — and
    therefore the WS tunnel heartbeat — while the kernel works.

    The child does the bare minimum before ``exec`` to stay safe in a
    multi-threaded process: the caller resolves the absolute ``tmux``
    path and builds the env dict in the parent so the child uses
    :func:`os.execve` directly (no Python-level PATH search or dict
    construction, which would allocate). Logging or other allocation on
    the child path is deliberately avoided.

    :param tmux_path: Absolute path to the ``tmux`` binary resolved in
        the parent, e.g. ``"/opt/homebrew/bin/tmux"``.
    :param argv: Full argument vector with ``argv[0]`` the program name,
        e.g. ``["tmux", "-S", "/tmp/x.sock", "attach", "-t", "main"]``.
    :param env: Complete child environment built in the parent, e.g.
        the parent env with ``TERM`` pinned to
        :data:`_ATTACH_CLIENT_TERM`.
    :returns: The :class:`_SpawnedPty` for the parent side.
    :raises OSError: If ``pty.fork`` fails in the parent (e.g. process
        or fd limits). Propagates to the caller to close the WS.
    """
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: replace the image with tmux. Never returns. ``execve``
        # (not ``execvpe``) so there is no Python-level PATH search — the
        # less the child does before exec, the safer the fork. On exec
        # failure ``_exit(127)`` mirrors the shell convention so the
        # parent's waitpid sees a recognizable status.
        try:
            os.execve(tmux_path, argv, env)
        except OSError:
            os._exit(127)
    return _SpawnedPty(pid=pid, master_fd=master_fd)


def _monotonic() -> float:
    """
    Return a monotonic clock reading for terminal bridge timing.

    :returns: Seconds from an unspecified monotonic epoch.
    """
    return time.monotonic()


async def _reap_tmux_attach_child(pid: int) -> None:
    """
    Reap the tmux attach child without using the default executor.

    ``os.waitpid(pid, 0)`` in ``run_in_executor`` can hide shutdown
    latency behind an executor worker. Polling ``waitpid(WNOHANG)``
    keeps the event loop responsive, bounds the graceful wait, and
    still reaps the child after escalating to SIGKILL if it ignores
    SIGTERM.

    :param pid: Child process id returned by ``pty.fork``.
    :returns: None after the child has been reaped or is no longer a
        child of this process.
    """
    deadline = _monotonic() + _TMUX_ATTACH_WAIT_GRACE_S
    killed = False
    while True:
        try:
            waited_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid == pid:
            return
        if _monotonic() >= deadline and not killed:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            killed = True
        await _sleep(_TMUX_ATTACH_WAIT_POLL_S)


async def _sleep(seconds: float) -> None:
    """
    Sleep for *seconds*.

    Exists as a private indirection so tests can shorten bridge
    polling waits without patching :mod:`asyncio` globally.

    :param seconds: Delay in seconds, e.g. ``0.02``.
    :returns: None after the delay elapses.
    """
    await asyncio.sleep(seconds)


async def _tmux_session_alive(socket_path: str, tmux_target: str) -> bool:
    """
    Return whether the agent behind the bridge is still alive.

    Probes ``tmux -S <socket> list-panes -t <target> -F '#{pane_dead}'`` against
    the same private server the bridge attached to. This distinguishes a *detach*
    (the ``tmux attach`` child exits but the agent keeps running) from a genuine
    exit (Claude quit / the session was killed). The pane-dead flag — not bare
    session existence — is the signal because the claude-native terminal opts
    into ``remain-on-exit`` (#540): there the session deliberately outlives the
    inner CLI, so a plain ``has-session`` would wrongly report a crashed agent
    as merely detached and the client would reconnect to a dead pane forever.
    For terminals without ``remain-on-exit`` the inner process's exit destroys
    the session, the probe exits non-zero, and the verdict is unchanged.

    Fails conservative: any probe error (tmux missing, spawn failure, timeout,
    or a non-zero exit from a vanished session) returns ``False`` so the caller
    falls back to the terminal-gone close code rather than wrongly reporting a
    dead agent as alive.

    :param socket_path: Filesystem path to the tmux server socket,
        e.g. ``"/tmp/omnigent-xyz/tmux.sock"``.
    :param tmux_target: The ``-t`` target identifying the session,
        e.g. ``"main"``.
    :returns: ``True`` only when the session exists and its pane process has
        not exited; ``False`` on a missing/dead session or any probe failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-S",
            socket_path,
            "list-panes",
            "-t",
            tmux_target,
            "-F",
            "#{pane_dead}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        _logger.debug("tmux-attach: pane-dead probe spawn failed", exc_info=True)
        return False
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=_TMUX_HAS_SESSION_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, OSError):
        _logger.debug("tmux-attach: pane-dead probe timed out", exc_info=True)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return False
    # ``list-panes`` errors on an unknown/dead session (unlike ``display-message``,
    # which silently falls back to another pane). rc != 0 → session gone; a "1"
    # line → the inner process exited but the session was kept alive by
    # remain-on-exit. Both mean the agent is gone.
    panes = stdout.decode().split()
    return proc.returncode == 0 and bool(panes) and "1" not in panes


async def _write_all_nonblocking(
    loop: asyncio.AbstractEventLoop,
    fd: int,
    data: bytes,
) -> None:
    """
    Write every byte of *data* to non-blocking *fd*, waiting as needed.

    Handles both short writes (kernel accepted fewer bytes than
    requested) and ``EAGAIN`` (buffer completely full) by waiting for
    the fd to become writable via the event loop before retrying.
    Silently exits on any other ``OSError`` (PTY gone).

    :param loop: Running asyncio event loop.
    :param fd: Non-blocking file descriptor (the PTY master).
    :param data: Bytes to write.
    """
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
            view = view[n:]
        except BlockingIOError:
            writable: asyncio.Future[None] = loop.create_future()

            def _on_writable(fut: asyncio.Future[None] = writable) -> None:
                loop.remove_writer(fd)
                if not fut.done():
                    fut.set_result(None)

            loop.add_writer(fd, _on_writable)
            try:
                await writable
            except asyncio.CancelledError:
                with contextlib.suppress(ValueError):
                    loop.remove_writer(fd)
                raise
        except OSError:
            return


async def _forward_pty_to_ws(
    websocket: WebSocket,
    pty_chunks: asyncio.Queue[bytes | None],
    *,
    max_coalesce_bytes: int | Callable[[], int] = _WS_COALESCE_MAX_BYTES,
) -> None:
    """
    Forward queued PTY output to *websocket*, coalescing ready chunks.

    Blocks on the first ``get`` (idle terminal = no frames, no latency),
    then merges only chunks ALREADY queued (``get_nowait``) into one
    ``send_bytes`` — never waiting for more, so a lone keystroke flushes
    immediately. This collapses bursts: a screen redraw enqueues ~10
    chunks that would otherwise be ~10 WS frames into one.

    The cap is hard: if one PTY read is larger than the current cap, it is
    split into multiple WebSocket frames. That matters for the browser
    terminal's interactive echo path, where frames above 2 KiB fall back
    to xterm's queued async write and feel laggy.

    :param websocket: The accepted websocket to send binary frames on.
    :param pty_chunks: Queue the PTY reader feeds; ``None`` is the EOF
        sentinel.
    :param max_coalesce_bytes: Per-frame cap, or a zero-argument callable
        returning the current cap. Defaults to the production
        :data:`_WS_COALESCE_MAX_BYTES`; ``bridge_tmux_pty_to_websocket``
        supplies a callable so recently-typed redraws can use the smaller
        interactive cap while normal output keeps the larger flood cap.
    :returns: None on EOF or websocket disconnect.
    """
    pending = bytearray()
    eof_seen = False
    while True:
        if not pending:
            chunk = await pty_chunks.get()
            if chunk is None:
                return
            pending.extend(chunk)

        limit = _current_coalesce_limit(max_coalesce_bytes)
        while len(pending) < limit:
            try:
                nxt = pty_chunks.get_nowait()
            except asyncio.QueueEmpty:
                break
            if nxt is None:
                eof_seen = True
                break
            pending.extend(nxt)
            limit = _current_coalesce_limit(max_coalesce_bytes)

        while pending:
            limit = _current_coalesce_limit(max_coalesce_bytes)
            frame = bytes(pending[:limit])
            del pending[:limit]
            try:
                await websocket.send_bytes(frame)
            except (RuntimeError, WebSocketDisconnect):
                return
        if eof_seen:
            return


def _current_coalesce_limit(max_coalesce_bytes: int | Callable[[], int]) -> int:
    """
    Resolve the active PTY-output WebSocket frame cap.

    :param max_coalesce_bytes: Static cap or callable dynamic cap.
    :returns: Positive integer cap in bytes.
    :raises ValueError: If the resolved cap is not positive.
    """
    raw = max_coalesce_bytes() if callable(max_coalesce_bytes) else max_coalesce_bytes
    if raw <= 0:
        raise ValueError("max_coalesce_bytes must be positive")
    return raw


def _coalesce_limit_after_input(last_client_input_at: float | None) -> int:
    """
    Return the PTY-output frame cap for the current interaction state.

    A Codex/Claude TUI redraw right after a keystroke must stay small
    enough for the browser's synchronous xterm echo path. Away from user
    input, keep the larger cap so output floods still stream in efficient
    chunks instead of one-frame-per-read.

    :param last_client_input_at: Monotonic timestamp for the last
        forwarded binary client input, e.g. ``12345.0``, or ``None``
        before the client has typed.
    :returns: Frame cap in bytes.
    """
    if last_client_input_at is None:
        return _WS_COALESCE_MAX_BYTES
    if _monotonic() - last_client_input_at < _INTERACTIVE_ECHO_WINDOW_S:
        return _INTERACTIVE_WS_COALESCE_MAX_BYTES
    return _WS_COALESCE_MAX_BYTES


async def bridge_tmux_pty_to_websocket(
    websocket: WebSocket,
    *,
    socket_path: str,
    tmux_target: str,
    read_only: bool,
    on_client_interaction: Callable[[], None] | None = None,
) -> None:
    """
    Bridge a tmux attach PTY to an already-accepted *websocket*.

    Caller must have called ``websocket.accept()``. On exit (any
    branch), the PTY and tmux child are torn down and the websocket
    is closed best-effort.

    :param websocket: An accepted FastAPI :class:`WebSocket`.
    :param socket_path: Filesystem path to the tmux server socket
        (``tmux -S <socket>``).
    :param tmux_target: The ``-t`` target string identifying the
        session/window/pane within that tmux server.
    :param read_only: When ``True``, pass ``-r`` to tmux *and* drop
        inbound binary frames at the application layer. Both layers
        exist because tmux's ``-r`` is the authoritative defense and
        the app-level drop saves the WS round-trip + tmux's
        ignore-and-bell.
    :param on_client_interaction: Optional callback fired on every client
        interaction with the pane — attach (connect), detach
        (disconnect), each forwarded input frame (keystroke / focus /
        mouse), and each resize message. The runner passes
        ``TerminalInstance.note_client_interaction`` so the idle watcher
        can discount the client-driven repaints those events trigger
        (attach/detach reflow, focus in/out, clicks, typing) instead of
        mis-reading them as agent activity. ``None`` (e.g. the
        server-direct attach path, which is out-of-process from the
        watcher) disables that attribution.
    """
    # Attaching is itself a client interaction: tmux resizes the window to
    # the new client, which reflows the pane. Stamp it before the bridge
    # starts so that reflow is discounted.
    if on_client_interaction is not None:
        on_client_interaction()
    argv = ["tmux", "-S", socket_path, "attach"]
    if read_only:
        argv.append("-r")
    argv += ["-t", tmux_target]

    # Resolve the absolute path in the parent so the forked child can
    # ``execv`` without a Python-level PATH search (see _fork_exec_pty).
    # tmux missing is a fail-loud condition, not a silent terminal-gone.
    tmux_path = shutil.which("tmux")
    if tmux_path is None:
        _logger.error("tmux not found on PATH; cannot attach target=%s", tmux_target)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=WS_CLOSE_INTERNAL_ERROR, reason="tmux not found")
        return

    _logger.debug("tmux-attach: spawning argv=%s", argv)

    # Describe the real attach client (an xterm.js-compatible emulator)
    # to tmux regardless of this process's ambient TERM — see
    # _ATTACH_CLIENT_TERM. Built in the parent: the post-fork child must
    # not allocate.
    attach_env = {**os.environ, "TERM": _ATTACH_CLIENT_TERM}

    # Fork+exec off the event loop and serialized process-wide: the fork
    # of a large multi-threaded runner can be slow (page-table copy) and
    # is unsafe to overlap, so to_thread keeps the loop (and the tunnel
    # heartbeat) responsive while _attach_fork_gate() ensures only one
    # fork runs at a time.
    try:
        async with _attach_fork_gate():
            spawned = await asyncio.to_thread(_fork_exec_pty, tmux_path, argv, attach_env)
    except OSError:
        _logger.exception("pty.fork failed for tmux attach target=%s", tmux_target)
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=WS_CLOSE_INTERNAL_ERROR, reason="pty.fork failed")
        return

    pid = spawned.pid
    master_fd = spawned.master_fd

    # Parent: flip the master fd to non-blocking so add_reader
    # doesn't stall the event loop.
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_running_loop()
    pty_chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
    last_client_input_at: float | None = None

    def _current_ws_coalesce_limit() -> int:
        """
        Return the PTY-output frame cap for this bridge.

        :returns: Frame cap in bytes.
        """
        return _coalesce_limit_after_input(last_client_input_at)

    def _on_pty_readable() -> None:
        try:
            while True:
                chunk = os.read(master_fd, _PTY_READ_CHUNK)
                if not chunk:
                    loop.remove_reader(master_fd)
                    pty_chunks.put_nowait(None)
                    return
                pty_chunks.put_nowait(chunk)
        except BlockingIOError:
            return
        except OSError:
            with contextlib.suppress(ValueError):
                loop.remove_reader(master_fd)
            pty_chunks.put_nowait(None)

    loop.add_reader(master_fd, _on_pty_readable)

    async def _ws_to_pty() -> None:
        nonlocal last_client_input_at
        try:
            while True:
                msg = await websocket.receive()
                # Every received frame is a client interaction — a
                # keystroke/focus/mouse byte, a resize, or a disconnect —
                # and each makes the TUI repaint. Stamp before dispatch so
                # the idle watcher discounts the resulting pane change.
                if on_client_interaction is not None:
                    on_client_interaction()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
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
                        winsize = struct.pack("HHHH", rows, cols, 0, 0)
                        with contextlib.suppress(OSError):
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                elif data is not None and not read_only:
                    last_client_input_at = _monotonic()
                    await _write_all_nonblocking(loop, master_fd, data)
        except WebSocketDisconnect:
            return

    pty_task = asyncio.create_task(
        _forward_pty_to_ws(websocket, pty_chunks, max_coalesce_bytes=_current_ws_coalesce_limit),
        name="tmux-attach-pty-to-ws",
    )
    ws_task = asyncio.create_task(_ws_to_pty(), name="tmux-attach-ws-to-pty")
    pty_ended_first = False
    try:
        done, pending = await asyncio.wait(
            {pty_task, ws_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        pty_ended_first = pty_task in done
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None:
                _logger.warning("tmux-attach: bridge task crashed: %r", exc)
    finally:
        # Detach is a client interaction too: when this client goes away
        # tmux resizes the window back to its remaining clients (or the
        # detached default), reflowing the pane. Stamp on every exit path
        # so that detach reflow is discounted rather than read as activity.
        if on_client_interaction is not None:
            on_client_interaction()
        with contextlib.suppress(ValueError):
            loop.remove_reader(master_fd)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        await _reap_tmux_attach_child(pid)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        # Pick a close code only when the PTY ended first (the
        # ``tmux attach`` child exited); a client-disconnect-first uses
        # the default close code. PTY EOF alone is ambiguous: it happens
        # both when the user *detaches* (session still alive) and when
        # the session genuinely ends. Probe ``has-session`` to tell them
        # apart — a detach must not be reported as terminal-gone, or the
        # client tears the whole session and runner down.
        with contextlib.suppress(RuntimeError):
            if pty_ended_first:
                if await _tmux_session_alive(socket_path, tmux_target):
                    await websocket.close(
                        code=WS_CLOSE_TERMINAL_DETACHED,
                        reason="terminal detached",
                    )
                else:
                    await websocket.close(
                        code=WS_CLOSE_TERMINAL_NOT_FOUND,
                        reason="terminal session ended",
                    )
            else:
                await websocket.close()
