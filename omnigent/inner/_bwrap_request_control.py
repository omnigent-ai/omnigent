"""Parent-owned admission and pinned completion for a Linux shell request."""

from __future__ import annotations

import array
import contextlib
import errno
import os
import select
import signal
import socket
import struct
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from ._bwrap_request_init import _ADMIT, _EXIT, _READY, _validate_control_socket

_POLL_S = 0.05
_CREDENTIALS = struct.Struct("iII")


@dataclass(frozen=True)
class BwrapRequestResult:
    """Command result, with empty bytes for output streams that were not captured."""

    stdout: bytes
    stderr: bytes
    exit_code: int | None
    timed_out: bool


class RequestCancelled(RuntimeError):
    """The owner cancelled the request; an admitted command is never replayable."""


def _pidfd_identity(descriptor: int) -> tuple[int, tuple[int, ...]]:
    fields = dict(
        line.split(":", 1)
        for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines()
        if ":" in line
    )
    try:
        return int(fields["Pid"]), tuple(int(value) for value in fields["NSpid"].split())
    except (KeyError, ValueError) as exc:
        raise OSError("Request lifetime descriptor is not a live pidfd") from exc


def _pidfd_exited(descriptor: int, timeout_ms: int = 0) -> bool:
    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    for _, flags in poller.poll(timeout_ms):
        if flags & (select.POLLERR | select.POLLNVAL):
            raise OSError(errno.EBADF, "Request lifetime descriptor is invalid")
        if flags & (select.POLLIN | select.POLLHUP):
            return True
    return False


def _kill_pinned(descriptor: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)


class BwrapRequestControl:
    """Own a trusted init and its launcher after receiving kernel-bound readiness.

    Control uses AF_UNIX SOCK_SEQPACKET. The launcher owner enables SO_PASSCRED
    before spawn and retains its outer pidfd until receive succeeds. Only run
    closes the transferred resources. The owner must retain its credentials and
    signaling authority until run finishes retiring both pinned lifetimes.
    """

    def __init__(
        self,
        control: socket.socket,
        process: subprocess.Popen[bytes],
        token: bytes,
        init_pidfd: int,
        launcher_pidfd: int,
    ) -> None:
        _validate_control_socket(control)
        self._control = control
        self._process = process
        self._token = token
        self._init_pidfd = init_pidfd
        self._launcher_pidfd = launcher_pidfd
        self._owned_pidfds: tuple[int, int] | None = (init_pidfd, launcher_pidfd)
        self._lock = threading.Lock()
        self._stop_reason: str | None = None
        self._started = False
        self._admitted = False
        self.done = threading.Event()

    @classmethod
    def receive(
        cls,
        control: socket.socket,
        process: subprocess.Popen[bytes],
        token: bytes,
        *,
        expected_init_pid: int,
        launcher_pidfd: int,
    ) -> BwrapRequestControl:
        """Take ownership only after validating the init's actual transferred pidfd."""
        _validate_control_socket(control)
        if len(token) != 64 or any(value not in b"0123456789abcdef" for value in token):
            raise ValueError("Request admission token is invalid")
        if expected_init_pid <= 0 or _pidfd_identity(launcher_pidfd)[0] != process.pid:
            raise OSError("Request launcher identity is invalid")
        integer_size = array.array("i").itemsize
        payload, ancillary, flags, _ = control.recvmsg(
            len(_READY) + len(token) + 1,
            socket.CMSG_SPACE(integer_size) + socket.CMSG_SPACE(_CREDENTIALS.size),
            socket.MSG_CMSG_CLOEXEC,
        )
        descriptors: list[int] = []
        credentials: list[tuple[int, int, int]] = []
        unexpected_metadata = False
        try:
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(data[: len(data) - len(data) % integer_size])
                    descriptors.extend(values)
                    unexpected_metadata |= len(data) % integer_size != 0
                elif (
                    level == socket.SOL_SOCKET
                    and kind == socket.SCM_CREDENTIALS
                    and len(data) == _CREDENTIALS.size
                ):
                    credentials.append(_CREDENTIALS.unpack(data))
                else:
                    unexpected_metadata = True
            if unexpected_metadata:
                raise OSError("Unexpected request readiness metadata")
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or payload != _READY + token:
                raise OSError("Request readiness was incomplete or belongs to another request")
            if len(descriptors) != 1 or len(credentials) != 1:
                raise OSError("Request readiness needs one init handle and kernel credentials")
            sender_pid, sender_uid, _ = credentials[0]
            init_pid, namespace_pids = _pidfd_identity(descriptors[0])
            if (
                sender_uid != os.geteuid()
                or sender_pid != expected_init_pid
                or init_pid != sender_pid
                or not namespace_pids
                or namespace_pids[-1] != 1
            ):
                raise OSError("Request readiness is not from the pinned namespace init")
            os.set_inheritable(descriptors[0], False)
            instance = cls(control, process, token, descriptors[0], launcher_pidfd)
            descriptors.clear()
            return instance
        finally:
            with contextlib.ExitStack() as cleanup:
                for descriptor in descriptors:
                    cleanup.callback(os.close, descriptor)

    @property
    def admitted(self) -> bool:
        with self._lock:
            return self._admitted

    def cancel(self) -> None:
        """Stop through the pinned handles without waiting on helper IPC or output."""
        self._stop("cancelled")

    def _stop(self, reason: str) -> None:
        with self._lock:
            if self._owned_pidfds is None:
                return
            if self._stop_reason is None:
                self._stop_reason = reason
            for descriptor in self._owned_pidfds:
                _kill_pinned(descriptor)

    def _admit(self, deadline: float) -> None:
        with self._lock:
            if self._stop_reason is not None:
                raise RequestCancelled("Request was cancelled before admission")
            if monotonic() >= deadline:
                raise TimeoutError("Request admission deadline elapsed")
            if _pidfd_exited(self._init_pidfd) or _pidfd_exited(self._launcher_pidfd):
                raise OSError("A request lifetime ended before admission")
            self._control.setblocking(False)
            # A failed send may still be ambiguous; never make this request replayable.
            self._admitted = True
            admission = _ADMIT + self._token
            if self._control.send(admission) != len(admission):
                raise OSError("Request admission was not fully delivered")

    def _read_result(self) -> int | None:
        try:
            payload = self._control.recv(256)
        except BlockingIOError:
            return None
        if not payload:
            raise OSError("Request result was lost; the command must not be replayed")
        prefix = _EXIT + self._token + b":"
        try:
            raw_code = payload[len(prefix) :]
            code = int(raw_code)
        except ValueError as exc:
            raise OSError("Request result is invalid") from exc
        if (
            not payload.startswith(prefix)
            or str(code).encode("ascii") != raw_code
            or not -127 <= code <= 255
        ):
            raise OSError("Request result is invalid")
        return code

    def _read_pending_result(self) -> int | None:
        try:
            return self._read_result()
        except OSError:
            # Stopping the init can close its channel before a result arrives.
            with self._lock:
                if self._stop_reason is None:
                    raise
            return None

    def run(self, *, deadline: float) -> BwrapRequestResult:
        """Admit once, then require namespace exit and launcher/output cleanup.

        The deadline is observed between 50 ms polls; completion during the
        current poll wins over expiry within that poll.
        """
        with self._lock:
            if self._started:
                raise RuntimeError("A shell request can only run once")
            self._started = True
        outputs: tuple[bytes, bytes] | None = None
        exit_code: int | None = None
        try:
            self._admit(deadline)
            while True:
                if monotonic() >= deadline:
                    self._stop("timeout")
                if exit_code is None and self._stop_reason is None:
                    exit_code = self._read_pending_result()
                if outputs is None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        stdout, stderr = self._process.communicate(timeout=_POLL_S)
                        outputs = (stdout or b"", stderr or b"")
                init_exited = _pidfd_exited(self._init_pidfd)
                launcher_exited = _pidfd_exited(self._launcher_pidfd)
                if init_exited and launcher_exited and outputs is not None:
                    break
                if outputs is not None:
                    pending = self._launcher_pidfd if init_exited else self._init_pidfd
                    _pidfd_exited(pending, round(_POLL_S * 1000))
            with self._lock:
                if self._stop_reason == "cancelled":
                    raise RequestCancelled("Request was cancelled; it must not be replayed")
                timed_out = self._stop_reason == "timeout"
                if not timed_out and exit_code is None:
                    # The final frame may arrive between the last read and init exit.
                    exit_code = self._read_result()
                    if exit_code is None:
                        raise OSError("Request result was lost; the command must not be replayed")
                return BwrapRequestResult(*outputs, None if timed_out else exit_code, timed_out)
        finally:
            self._finish()

    def _finish(self) -> None:
        if not _pidfd_exited(self._init_pidfd) or not _pidfd_exited(self._launcher_pidfd):
            self._stop("cleanup")
        while not _pidfd_exited(self._init_pidfd, round(_POLL_S * 1000)):
            pass
        while not _pidfd_exited(self._launcher_pidfd, round(_POLL_S * 1000)):
            pass
        try:
            self._process.communicate()
        finally:
            # Serialize descriptor retirement with cancellation to prevent fd reuse races.
            with self._lock:
                self._owned_pidfds = None
                try:
                    with contextlib.ExitStack() as cleanup:
                        cleanup.callback(self._control.close)
                        cleanup.callback(os.close, self._launcher_pidfd)
                        cleanup.callback(os.close, self._init_pidfd)
                        for stream in (
                            self._process.stdin,
                            self._process.stdout,
                            self._process.stderr,
                        ):
                            if stream is not None:
                                cleanup.callback(stream.close)
                finally:
                    # Output or close errors must not strand an already settled request.
                    self.done.set()
