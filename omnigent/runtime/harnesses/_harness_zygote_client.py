"""Runner-side client for forking harness subprocesses via the runner zygote.

A runner spawned by the zygote inherits a control socket back to it (fd in
``OMNIGENT_RUNNER_ZYGOTE_HARNESS_FD``). Instead of ``create_subprocess_exec``-ing
a fresh ``python -m omnigent.runtime.harnesses._runner`` per conversation — which
re-pays the harness import floor — the runner asks the zygote to ``os.fork()`` a
harness child that shares that graph copy-on-write.

The zygote (not the runner) is the harness child's OS parent, so this module
exposes:

* :class:`ZygoteHarnessProc` — an ``asyncio.subprocess.Process``-shaped shim over
  the surface :class:`~omnigent.runtime.harnesses.process_manager.HarnessProcessManager`
  uses: ``pid``, ``returncode``, ``wait()``, ``kill()``, ``send_signal()``. Exit
  status is polled from the zygote (the real parent) by a background task so the
  synchronous ``returncode`` reads in ``_wait_for_bind`` stay fresh; signals go to
  the pid directly.
* :class:`HarnessZygoteClient` — owns the inherited control socket and serializes
  ``fork_harness`` / ``poll`` exchanges over it.

Everything is best-effort and gated by the presence of the inherited fd; the
process manager falls back to a direct exec when the client is unavailable, so
the zygote is never a hard dependency for harness spawning.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
from typing import Any

from omnigent.runner._zygote import ZYGOTE_HARNESS_FD_ENV_VAR

logger = logging.getLogger(__name__)

# Cadence for polling the zygote for a forked harness's exit code. The harness
# runs for the life of the conversation, so this only matters around teardown;
# 0.1s keeps wait() responsive without loading the control channel.
_POLL_INTERVAL_S = 0.1


class ZygoteHarnessUnavailable(RuntimeError):
    """The harness zygote channel is absent or stopped responding.

    The process manager catches this and falls back to a direct exec.
    """


class ZygoteHarnessProc:
    """An ``asyncio.subprocess.Process``-shaped handle for a zygote-forked harness.

    :param pid: The forked harness process id.
    :param client: The owning :class:`HarnessZygoteClient`, polled for exit code.
    """

    def __init__(self, pid: int, client: HarnessZygoteClient) -> None:
        self.pid = pid
        self.returncode: int | None = None
        # close_subprocess_transport(proc) getattrs this; None is handled there.
        self._transport = None
        self._client = client
        # Keep returncode fresh for the manager's synchronous reads (e.g. the
        # _wait_for_bind loop, which never awaits wait()).
        self._poll_task: asyncio.Task[None] | None = asyncio.ensure_future(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Poll the zygote until this harness exits, then cache its code."""
        while self.returncode is None:
            try:
                code = await self._client.poll(self.pid)
            except ZygoteHarnessUnavailable:
                # Zygote gone: the harness reparented and is self-terminating
                # via its own watchdog; stop polling and let wait() proceed via
                # the signal path the manager takes on teardown.
                return
            if code is not None:
                self.returncode = code
                return
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def wait(self) -> int:
        """Block until the harness exits, returning its code.

        :returns: The harness exit code (0 if it could not be determined
            because the zygote went away — the harness is gone either way).
        """
        # A teardown that cancels the manager's task should not turn wait() into
        # a hard error; fall through to the cached returncode.
        if self._poll_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        return self.returncode if self.returncode is not None else 0

    def send_signal(self, sig: int) -> None:
        """Signal the harness pid, ignoring an already-exited process.

        :param sig: POSIX signal number.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(self.pid, sig)

    def terminate(self) -> None:
        """Send ``SIGTERM`` to the harness (best-effort)."""
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        """Send ``SIGKILL`` to the harness (best-effort)."""
        self.send_signal(getattr(signal, "SIGKILL", signal.SIGTERM))


class HarnessZygoteClient:
    """Owns a runner's inherited control socket to the zygote.

    :param control_fd: fd of the runner's connected control socket back to the
        zygote (from ``OMNIGENT_RUNNER_ZYGOTE_HARNESS_FD``).
    """

    def __init__(self, control_fd: int) -> None:
        self._sock: socket.socket | None = socket.socket(
            socket.AF_UNIX, socket.SOCK_STREAM, fileno=control_fd
        )
        self._sock.setblocking(True)
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> HarnessZygoteClient | None:
        """Build a client from the inherited fd env var, or ``None`` if absent.

        :returns: A client when this runner was zygote-forked, else ``None``
            (a direct ``Popen``-launched runner has no zygote channel).
        """
        raw = os.environ.get(ZYGOTE_HARNESS_FD_ENV_VAR)
        if not raw:
            return None
        try:
            fd = int(raw)
        except ValueError:
            return None
        try:
            return cls(fd)
        except OSError:
            return None

    async def fork_harness(self, argv: list[str], env: dict[str, str]) -> ZygoteHarnessProc:
        """Ask the zygote to fork a harness running ``_runner.main(argv)``.

        :param argv: The ``_runner`` CLI flags (incl. ``--parent-pid <runner>``).
        :param env: Full harness environment (replaces the child's ``os.environ``).
        :returns: A shim handle for the forked harness.
        :raises ZygoteHarnessUnavailable: If the zygote is down or errored.
        """
        reply = await self._exchange({"cmd": "fork_harness", "argv": argv, "env": env})
        if "error" in reply:
            raise ZygoteHarnessUnavailable(f"zygote harness fork failed: {reply['error']}")
        pid = reply.get("pid")
        if not isinstance(pid, int):
            raise ZygoteHarnessUnavailable(f"zygote returned no pid: {reply!r}")
        return ZygoteHarnessProc(pid, self)

    async def poll(self, pid: int) -> int | None:
        """Return a forked harness's exit code, or ``None`` if still running.

        :param pid: The forked harness pid.
        :returns: Exit code once the zygote has reaped it, else ``None``.
        """
        reply = await self._exchange({"cmd": "poll", "pid": pid})
        code = reply.get("returncode")
        return code if isinstance(code, int) else None

    async def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send one request and read one reply, serialized under a lock.

        Socket I/O is blocking, so it runs in a worker thread to avoid stalling
        the runner's event loop.

        :param request: The protocol request.
        :returns: The decoded reply.
        :raises ZygoteHarnessUnavailable: On any socket error / EOF.
        """
        async with self._lock:
            sock = self._sock
            if sock is None:
                raise ZygoteHarnessUnavailable("harness zygote channel is closed")
            try:
                return await asyncio.to_thread(self._exchange_blocking, sock, request)
            except (OSError, ValueError) as exc:
                raise ZygoteHarnessUnavailable(f"harness zygote control error: {exc}") from exc

    @staticmethod
    def _exchange_blocking(sock: socket.socket, request: dict[str, Any]) -> dict[str, Any]:
        """Blocking send-one / read-one-line exchange on *sock*.

        :param sock: The connected control socket.
        :param request: The request to send.
        :returns: The decoded reply mapping.
        :raises ZygoteHarnessUnavailable: On EOF before a full reply line.
        :raises OSError: On a socket-level failure (surfaced by the caller).
        """
        sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ZygoteHarnessUnavailable("harness zygote closed the control socket")
            buf.extend(chunk)
        line, _, _ = bytes(buf).partition(b"\n")
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise ZygoteHarnessUnavailable(f"harness zygote sent a non-object reply: {decoded!r}")
        return decoded
