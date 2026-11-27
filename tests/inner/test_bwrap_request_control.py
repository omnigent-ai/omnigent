"""Parent admission/lifetime units and real Linux process-boundary checks."""

from __future__ import annotations

import array
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from _thread import LockType
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar
from unittest.mock import MagicMock

import pytest

from omnigent.inner import _bwrap_request_control as request_control
from omnigent.inner import _bwrap_request_init as request_init
from omnigent.inner.bwrap_sandbox import BwrapRequestFds, BwrapSandboxBackend
from omnigent.inner.sandbox import SandboxPolicy
from tests._helpers.polling import poll_until

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux pidfd and credential metadata"
)

_TOKEN = b"5" * 64
_INIT_FD = 41
_LAUNCHER_FD = 42
_Result = TypeVar("_Result")


def _start_worker(
    action: Callable[[], _Result], *, daemon: bool | None = None
) -> tuple[threading.Thread, Future[_Result]]:
    result: Future[_Result] = Future()

    def run() -> None:
        try:
            result.set_result(action())
        except BaseException as error:
            result.set_exception(error)

    worker = threading.Thread(target=run, daemon=daemon)
    worker.start()
    return worker, result


class _ContendedLock:
    def __init__(self, lock: LockType, contention: threading.Event) -> None:
        self._lock = lock
        self._contention = contention

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self._contention.set()
            self._lock.acquire()

    def __exit__(self, *_args: object) -> None:
        self._lock.release()


@pytest.fixture
def readiness(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    control = MagicMock(spec=socket.socket)
    control.family = socket.AF_UNIX
    control.getsockopt.return_value = socket.SOCK_SEQPACKET
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 83
    control.recvmsg.return_value = (
        b"ready:" + _TOKEN,
        [
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [_INIT_FD]).tobytes()),
            (
                socket.SOL_SOCKET,
                socket.SCM_CREDENTIALS,
                request_control._CREDENTIALS.pack(73, os.geteuid(), os.getegid()),
            ),
        ],
        0,
        None,
    )
    identities = {_LAUNCHER_FD: (83, (83,)), _INIT_FD: (73, (73, 1))}
    monkeypatch.setattr(request_control, "_pidfd_identity", identities.__getitem__)
    monkeypatch.setattr(request_control.os, "set_inheritable", MagicMock())
    close = MagicMock()
    monkeypatch.setattr(request_control.os, "close", close)
    return control, process, close


def test_readiness_requires_the_transferred_init_handle_and_kernel_sender(
    readiness: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    control, process, close = readiness
    owner = request_control.BwrapRequestControl.receive(
        control, process, _TOKEN, expected_init_pid=73, launcher_pidfd=_LAUNCHER_FD
    )
    assert not owner.admitted and not owner.done.is_set()
    request_control.os.set_inheritable.assert_called_once_with(_INIT_FD, False)
    assert control.recvmsg.call_args.args[2] == socket.MSG_CMSG_CLOEXEC
    control.send.assert_not_called()
    close.assert_not_called()


@pytest.mark.parametrize(
    "problem",
    [
        "token",
        "missing-credentials",
        "sender",
        "uid",
        "not-pid-one",
        "dead-pidfd",
        "foreign-pidfd",
        "extra-handle",
        "truncated",
        "unexpected-before-rights",
        "family",
        "launcher",
    ],
)
def test_invalid_readiness_never_admits_and_releases_every_received_handle(
    monkeypatch: pytest.MonkeyPatch,
    readiness: tuple[MagicMock, MagicMock, MagicMock],
    problem: str,
) -> None:
    control, process, close = readiness
    payload, metadata, flags, address = control.recvmsg.return_value
    handles = [_INIT_FD]
    if problem == "launcher":
        handles = []
        process.pid = 84
    elif problem == "family":
        handles = []
        control.family = socket.AF_INET
    elif problem == "token":
        payload = b"ready:" + b"6" * 64
    elif problem == "missing-credentials":
        metadata.pop()
    elif problem in {"sender", "uid"}:
        metadata[1] = (
            socket.SOL_SOCKET,
            socket.SCM_CREDENTIALS,
            request_control._CREDENTIALS.pack(
                74 if problem == "sender" else 73,
                os.geteuid() + (problem == "uid"),
                os.getegid(),
            ),
        )
    elif problem in {"not-pid-one", "dead-pidfd", "foreign-pidfd"}:
        init_identity = {
            "not-pid-one": (73, (73, 2)),
            "dead-pidfd": (-1, ()),
            "foreign-pidfd": (74, (74, 1)),
        }[problem]
        identities = {
            _LAUNCHER_FD: (83, (83,)),
            _INIT_FD: init_identity,
        }
        monkeypatch.setattr(request_control, "_pidfd_identity", identities.__getitem__)
    elif problem == "extra-handle":
        handles.append(43)
        metadata[0] = (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", handles).tobytes())
    elif problem == "truncated":
        flags = socket.MSG_CTRUNC
    else:
        metadata.insert(0, (socket.SOL_SOCKET, -1, b"unknown"))
    control.recvmsg.return_value = (payload, metadata, flags, address)
    with pytest.raises((OSError, ValueError)):
        request_control.BwrapRequestControl.receive(
            control, process, _TOKEN, expected_init_pid=73, launcher_pidfd=_LAUNCHER_FD
        )
    assert sorted(call.args[0] for call in close.call_args_list) == sorted(handles)
    control.send.assert_not_called()
    if problem in {"family", "launcher"}:
        control.recvmsg.assert_not_called()


def test_invalid_readiness_retires_other_handles_when_one_close_fails(
    readiness: tuple[MagicMock, MagicMock, MagicMock],
) -> None:
    control, process, close = readiness
    payload, metadata, flags, address = control.recvmsg.return_value
    metadata[0] = (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [41, 43]).tobytes())
    control.recvmsg.return_value = (payload, metadata, flags, address)

    def close_descriptor(descriptor: int) -> None:
        if descriptor == 43:
            raise OSError("Descriptor close failed")

    close.side_effect = close_descriptor
    with pytest.raises(OSError, match="Descriptor close failed"):
        request_control.BwrapRequestControl.receive(
            control, process, _TOKEN, expected_init_pid=73, launcher_pidfd=_LAUNCHER_FD
        )
    assert sorted(call.args[0] for call in close.call_args_list) == [41, 43]
    control.send.assert_not_called()


@dataclass
class _RequestCase:
    owner: request_control.BwrapRequestControl
    control: MagicMock
    process: MagicMock
    exited: dict[int, bool]
    events: list[tuple[str, int | None]]


@pytest.fixture
def request_case(monkeypatch: pytest.MonkeyPatch) -> _RequestCase:
    control = MagicMock(spec=socket.socket)
    control.family = socket.AF_UNIX
    control.getsockopt.return_value = socket.SOCK_SEQPACKET
    control.send.side_effect = len
    control.recv.return_value = b"exit:" + _TOKEN + b":7"
    process = MagicMock(spec=subprocess.Popen)
    process.stdin = MagicMock()
    process.stdout = MagicMock()
    process.stderr = MagicMock()
    exited = {_INIT_FD: False, _LAUNCHER_FD: False}
    events: list[tuple[str, int | None]] = []

    def kill(descriptor: int) -> None:
        events.append(("kill", descriptor))
        exited[descriptor] = True

    def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
        exited.update({_INIT_FD: True, _LAUNCHER_FD: True})
        events.append(("output", None))
        return b"exact stdout\n", b"exact stderr\n"

    process.communicate.side_effect = communicate
    monkeypatch.setattr(request_control, "_kill_pinned", kill)
    monkeypatch.setattr(request_control, "_pidfd_exited", lambda fd, _timeout=0: exited[fd])
    monkeypatch.setattr(request_control, "monotonic", lambda: 0.0)
    monkeypatch.setattr(request_control.os, "close", lambda fd: events.append(("close", fd)))
    owner = request_control.BwrapRequestControl(control, process, _TOKEN, _INIT_FD, _LAUNCHER_FD)
    return _RequestCase(owner, control, process, exited, events)


@pytest.mark.parametrize("last_gate", ["init", "launcher", "output"])
@pytest.mark.parametrize("timed_out", [False, True])
def test_completion_waits_for_each_pinned_lifetime_and_output(
    monkeypatch: pytest.MonkeyPatch,
    request_case: _RequestCase,
    last_gate: str,
    timed_out: bool,
) -> None:
    case = request_case
    clock = iter([0.0])
    monkeypatch.setattr(
        request_control, "monotonic", lambda: next(clock, 2.0 if timed_out else 0.0)
    )
    kills: list[int] = []
    monkeypatch.setattr(request_control, "_kill_pinned", kills.append)
    last_descriptor = {"init": _INIT_FD, "launcher": _LAUNCHER_FD}.get(last_gate)
    waited: list[int] = []
    output_calls = 0
    output_collected = False

    def assert_still_owned() -> None:
        assert not case.owner.done.is_set()
        assert not any(event[0] == "close" for event in case.events)
        case.control.close.assert_not_called()

    def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
        nonlocal output_calls, output_collected
        output_calls += 1
        if output_calls == 1:
            if timed_out:
                assert set(kills) == {_INIT_FD, _LAUNCHER_FD}
            case.exited.update(
                {descriptor: descriptor != last_descriptor for descriptor in case.exited}
            )
            assert_still_owned()
            if last_gate == "output":
                raise subprocess.TimeoutExpired("request-launcher", 0.05)
        if not output_collected:
            assert_still_owned()
            output_collected = True
        return b"exact stdout\n", b"exact stderr\n"

    def exited(descriptor: int, timeout_ms: int = 0) -> bool:
        if timeout_ms and output_collected and not all(case.exited.values()):
            if not waited:
                waited.append(descriptor)
                assert descriptor == last_descriptor
                assert_still_owned()
                assert case.exited[(_LAUNCHER_FD if descriptor == _INIT_FD else _INIT_FD)]
            if descriptor == last_descriptor:
                case.exited[descriptor] = True
        return case.exited[descriptor]

    case.process.communicate.side_effect = communicate
    monkeypatch.setattr(request_control, "_pidfd_exited", exited)
    result = case.owner.run(deadline=1.0)
    assert result == request_control.BwrapRequestResult(
        b"exact stdout\n", b"exact stderr\n", None if timed_out else 7, timed_out
    )
    assert waited == ([] if last_descriptor is None else [last_descriptor])
    assert set(kills) == ({_INIT_FD, _LAUNCHER_FD} if timed_out else set())
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)
    assert case.owner.admitted and case.owner.done.is_set()


@pytest.mark.parametrize("refusal", ["cancelled", "deadline", "init-exited", "launcher-exited"])
def test_refusal_before_admission_executes_nothing_and_retires_only_pinned_handles(
    monkeypatch: pytest.MonkeyPatch,
    request_case: _RequestCase,
    refusal: str,
) -> None:
    case = request_case
    error: type[Exception] = request_control.RequestCancelled
    if refusal == "cancelled":
        case.owner.cancel()
    elif refusal == "deadline":
        monkeypatch.setattr(request_control, "monotonic", lambda: 2.0)
        error = TimeoutError
    else:
        case.exited[_INIT_FD if refusal == "init-exited" else _LAUNCHER_FD] = True
        error = OSError
    with pytest.raises(error):
        case.owner.run(deadline=1.0)
    assert not case.owner.admitted and case.owner.done.is_set()
    case.control.send.assert_not_called()
    events = list(case.events)
    case.owner.cancel()
    assert case.events == events


@pytest.mark.parametrize("failure", ["descriptor", "output", "output-and-stream"])
def test_cleanup_failure_cannot_make_later_cancellation_signal_a_reused_fd(
    monkeypatch: pytest.MonkeyPatch, request_case: _RequestCase, failure: str
) -> None:
    case = request_case

    def close(descriptor: int) -> None:
        case.events.append(("close", descriptor))
        if failure == "descriptor" and descriptor == _LAUNCHER_FD:
            raise OSError("Descriptor close failed")

    monkeypatch.setattr(request_control.os, "close", close)
    error: type[Exception] = OSError
    message = "Descriptor close failed"
    if failure != "descriptor":
        case.process.communicate.side_effect = [
            ValueError("stdin is closed"),
            AttributeError("Output bookkeeping was not initialized"),
        ]
        error = AttributeError
        message = "Output bookkeeping"
        if failure == "output-and-stream":
            case.process.stdout.close.side_effect = OSError("Output stream close failed")
            error = OSError
            message = "Output stream close failed"
    with pytest.raises(error, match=message):
        case.owner.run(deadline=1.0)
    assert case.owner.done.is_set()
    assert sorted(fd for action, fd in case.events if action == "close") == [
        _INIT_FD,
        _LAUNCHER_FD,
    ]
    case.control.close.assert_called_once()
    if failure != "descriptor":
        for stream in (case.process.stdin, case.process.stdout, case.process.stderr):
            stream.close.assert_called_once()
    events = list(case.events)
    case.owner.cancel()
    assert case.events == events


@pytest.mark.parametrize(
    "delay_output", [True, False], ids=["delayed-output", "final-result-read"]
)
def test_output_and_result_polling_can_wait_without_replaying_or_finishing_the_request(
    request_case: _RequestCase, delay_output: bool
) -> None:
    case = request_case
    polls = 0
    case.control.recv.side_effect = [BlockingIOError(), b"exit:" + _TOKEN + b":7"]

    def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
        nonlocal polls
        polls += 1
        if delay_output and polls == 1:
            assert not case.owner.done.is_set()
            raise subprocess.TimeoutExpired("request-launcher", 0.05)
        case.exited.update({_INIT_FD: True, _LAUNCHER_FD: True})
        return b"delayed stdout\n", b""

    case.process.communicate.side_effect = communicate
    result = case.owner.run(deadline=1.0)
    assert result.stdout == b"delayed stdout\n" and result.exit_code == 7
    assert case.control.recv.call_count == 2
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)
    assert case.owner.done.is_set() and not result.timed_out


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ((None, b"stderr"), (b"", b"stderr")),
        ((b"stdout", None), (b"stdout", b"")),
        ((None, None), (b"", b"")),
    ],
)
def test_uncaptured_output_streams_return_empty_bytes(
    request_case: _RequestCase, output, expected
) -> None:
    case = request_case
    if output[0] is None:
        case.process.stdout = None
    if output[1] is None:
        case.process.stderr = None

    def communicate(**_kwargs: object):
        case.exited.update({_INIT_FD: True, _LAUNCHER_FD: True})
        return output

    case.process.communicate.side_effect = communicate
    result = case.owner.run(deadline=1.0)
    assert (result.stdout, result.stderr) == expected
    assert result.exit_code == 7 and case.owner.done.is_set()
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)


@pytest.mark.parametrize("cancel_during", ["admission", "result-read"])
def test_cancellation_contends_with_admission_and_retains_cleanup_without_replay(
    monkeypatch: pytest.MonkeyPatch, request_case: _RequestCase, cancel_during: str
) -> None:
    case = request_case
    sending = threading.Event()
    release_send = threading.Event()
    contention = threading.Event()
    cancelled = threading.Event()
    lock = threading.Lock()

    monkeypatch.setattr(case.owner, "_lock", _ContendedLock(lock, contention))

    def send(payload: bytes) -> int:
        sending.set()
        assert release_send.wait(5)
        return len(payload)

    def receive(_size: int) -> bytes:
        sending.set()
        assert release_send.wait(5)
        return b""

    if cancel_during == "admission":
        case.control.send.side_effect = send
    else:
        case.control.recv.side_effect = receive

    def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
        assert cancelled.wait(5)
        return b"partial stdout\n", b""

    case.process.communicate.side_effect = communicate

    def cancel() -> None:
        case.owner.cancel()
        cancelled.set()

    workers: list[threading.Thread] = []
    try:
        worker, result = _start_worker(lambda: case.owner.run(deadline=1.0))
        workers.append(worker)
        assert sending.wait(5)
        canceller, cancellation = _start_worker(cancel)
        workers.append(canceller)
        if cancel_during == "admission":
            assert contention.wait(5)
            assert not cancelled.is_set()
        else:
            assert cancelled.wait(5)
        release_send.set()
        assert cancelled.wait(5)
    finally:
        release_send.set()
        cancelled.set()
        case.exited.update({_INIT_FD: True, _LAUNCHER_FD: True})
        for worker in workers:
            worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in workers)
    cancellation.result(timeout=1)
    with pytest.raises(request_control.RequestCancelled, match="must not be replayed"):
        result.result(timeout=1)
    assert case.owner.admitted and case.owner.done.is_set()
    assert ("kill", _INIT_FD) in case.events and ("kill", _LAUNCHER_FD) in case.events
    assert ("close", _INIT_FD) in case.events and ("close", _LAUNCHER_FD) in case.events
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)


def test_cancellation_contends_with_retirement_and_never_signals_released_descriptors(
    monkeypatch: pytest.MonkeyPatch, request_case: _RequestCase
) -> None:
    """Cancellation waits for retirement before it can inspect descriptor ownership."""
    case = request_case
    retiring = threading.Event()
    release_close = threading.Event()
    contention = threading.Event()
    lock = threading.Lock()

    monkeypatch.setattr(case.owner, "_lock", _ContendedLock(lock, contention))

    def close(descriptor: int) -> None:
        case.events.append(("close", descriptor))
        if descriptor == _INIT_FD:
            retiring.set()
            assert release_close.wait(5)

    monkeypatch.setattr(request_control.os, "close", close)
    workers: list[threading.Thread] = []
    try:
        worker, result = _start_worker(lambda: case.owner.run(deadline=1.0))
        workers.append(worker)
        assert retiring.wait(5)
        canceller, cancellation = _start_worker(case.owner.cancel)
        workers.append(canceller)
        assert contention.wait(5)
        assert not cancellation.done() and not case.owner.done.is_set()
    finally:
        release_close.set()
        for worker in workers:
            worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in workers)
    cancellation.result(timeout=1)
    assert result.result(timeout=1).exit_code == 7
    assert case.owner.admitted and case.owner.done.is_set()
    assert not any(action == "kill" for action, _fd in case.events)
    assert sorted(fd for action, fd in case.events if action == "close") == [
        _INIT_FD,
        _LAUNCHER_FD,
    ]
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)


@pytest.mark.parametrize("send_failure", ["short", "exception"])
def test_ambiguous_admission_is_never_replayed(
    request_case: _RequestCase, send_failure: str
) -> None:
    case = request_case
    if send_failure == "short":
        case.control.send.side_effect = lambda _payload: 1
    else:
        case.control.send.side_effect = OSError("Reply lost after a possible send")
    with pytest.raises(OSError):
        case.owner.run(deadline=1.0)
    assert case.owner.admitted and case.owner.done.is_set()
    assert case.control.send.call_count == 1
    with pytest.raises(RuntimeError, match="only run once"):
        case.owner.run(deadline=1.0)


@pytest.mark.parametrize("reply", [b"", b"exit:" + b"6" * 64 + b":0", b"exit:" + _TOKEN + b":0x"])
def test_lost_or_wrong_request_results_cannot_report_success_or_replay(
    request_case: _RequestCase, reply: bytes
) -> None:
    case = request_case
    case.control.recv.return_value = reply
    with pytest.raises(OSError):
        case.owner.run(deadline=1.0)
    assert case.owner.admitted and case.owner.done.is_set()
    assert case.control.send.call_count == 1
    assert ("kill", _INIT_FD) in case.events


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"-127", -127),
        (b"-1", -1),
        (b"0", 0),
        (b"255", 255),
        (b"+0", None),
        (b"00", None),
        (b"-128", None),
        (b"256", None),
    ],
)
def test_result_parser_enforces_canonical_exit_code_boundaries(
    request_case: _RequestCase, value: bytes, expected: int | None
) -> None:
    case = request_case
    case.control.recv.return_value = b"exit:" + _TOKEN + b":" + value
    if expected is not None:
        assert case.owner._read_result() == expected
    else:
        with pytest.raises(OSError, match="Request result is invalid"):
            case.owner._read_result()
    case.control.send.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [None, ProcessLookupError("ended"), PermissionError("lost authority")],
    ids=["live", "already-exited", "lost-authority"],
)
def test_pinned_signaling_keeps_the_descriptor_and_reports_lost_authority(
    monkeypatch: pytest.MonkeyPatch, failure: OSError | None
) -> None:
    send = MagicMock(side_effect=failure)
    monkeypatch.setattr(request_control.signal, "pidfd_send_signal", send)
    if isinstance(failure, PermissionError):
        with pytest.raises(PermissionError, match="lost authority"):
            request_control._kill_pinned(_INIT_FD)
    else:
        request_control._kill_pinned(_INIT_FD)
    send.assert_called_once_with(_INIT_FD, request_control.signal.SIGKILL)


@pytest.mark.parametrize(
    ("flags", "expected"),
    [(None, False), ("POLLIN", True), ("POLLHUP", True), ("POLLNVAL", None)],
)
def test_kernel_exit_observation_rejects_invalid_descriptors(
    monkeypatch: pytest.MonkeyPatch, flags: str | None, expected: bool | None
) -> None:
    poller = MagicMock()
    poller.poll.return_value = [(2048, getattr(select, flags))] if flags else []
    monkeypatch.setattr(request_control.select, "poll", lambda: poller)
    if expected is None:
        with pytest.raises(OSError):
            request_control._pidfd_exited(2048)
    else:
        assert request_control._pidfd_exited(2048) is expected
    poller.register.assert_called_once_with(2048, select.POLLIN)


@pytest.mark.parametrize("contents", ["Pid:\t73\nNSpid:\t73\t1\n", "pos:\t0\nflags:\t02\n"])
def test_init_identity_comes_from_kernel_fdinfo(
    monkeypatch: pytest.MonkeyPatch, contents: str
) -> None:
    paths: list[str] = []

    def fdinfo(path: str) -> SimpleNamespace:
        paths.append(path)
        return SimpleNamespace(read_text=lambda: contents)

    monkeypatch.setattr(request_control, "Path", fdinfo)
    if contents.startswith("Pid:"):
        assert request_control._pidfd_identity(_INIT_FD) == (73, (73, 1))
    else:
        with pytest.raises(OSError):
            request_control._pidfd_identity(_INIT_FD)
    assert paths == [f"/proc/self/fdinfo/{_INIT_FD}"]


@dataclass
class _LiveBwrapRequest:
    owner: request_control.BwrapRequestControl
    process: subprocess.Popen[bytes]
    result: Future[request_control.BwrapRequestResult]
    init_lifetime: int
    launcher_lifetime: int
    descendant_ready: Path


@pytest.fixture
def live_bwrap_request(tmp_path: Path, ending: str) -> Iterator[_LiveBwrapRequest]:
    descendant_ready = tmp_path / "descendant-ready"
    descendant = """
import signal, sys
from pathlib import Path
print("descendant-ready", flush=True)
print("descendant-stderr", file=sys.stderr, flush=True)
Path(sys.argv[1]).write_text("ready")
signal.pause()
"""
    command = """
import signal, subprocess, sys, time
from pathlib import Path
subprocess.Popen(
    [sys.executable, "-I", "-S", "-c", sys.argv[3], sys.argv[1]],
    start_new_session=True,
)
ready = Path(sys.argv[1])
deadline = time.monotonic() + 5
while not ready.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("Descendant did not start")
    time.sleep(0.01)
if sys.argv[2] == "complete":
    print("command-complete", flush=True)
    raise SystemExit(7)
signal.pause()
"""
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[tmp_path],
        write_files=[],
        allow_network=False,
        cwd_allow_hidden=[],
        bind_host_proc=False,
    )
    owner = worker = process = None
    observed_lifetimes: list[int] = []
    with ExitStack() as resources:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        resources.enter_context(parent)
        resources.enter_context(child)
        parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        parent.settimeout(10)
        info = resources.enter_context(tempfile.TemporaryFile())
        baseline = resources.enter_context(tempfile.TemporaryFile())
        hardening = resources.enter_context(tempfile.TemporaryFile())
        config = resources.enter_context(tempfile.TemporaryFile())
        config.write(
            json.dumps(
                {
                    "token": _TOKEN.decode("ascii"),
                    "argv": [
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        command,
                        str(descendant_ready),
                        ending,
                        descendant,
                    ],
                    "cwd": str(tmp_path),
                    "env": {"PATH": os.defpath, "LANG": "C.UTF-8"},
                }
            ).encode()
        )
        config.flush()
        config.seek(0)
        descriptors = BwrapRequestFds(info.fileno(), baseline.fileno(), hardening.fileno())
        argv = BwrapSandboxBackend().wrap_launcher_argv(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                Path(request_init.__file__).read_text(),
                str(child.fileno()),
                str(config.fileno()),
            ],
            policy,
            tmp_path,
            _request_fds=descriptors,
        )
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                pass_fds=(
                    child.fileno(),
                    config.fileno(),
                    info.fileno(),
                    baseline.fileno(),
                    hardening.fileno(),
                ),
            )
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    resources.callback(stream.close)
            child.close()
            launcher_lifetime = os.pidfd_open(process.pid)
            resources.callback(os.close, launcher_lifetime)
            observed_lifetimes.append(launcher_lifetime)
            if not select.select([parent], [], [], 10)[0]:
                raise TimeoutError("Request init did not publish readiness")
            info.seek(0)
            init_pid = json.load(info)["child-pid"]
            transferred_launcher = os.dup(launcher_lifetime)
            try:
                owner = request_control.BwrapRequestControl.receive(
                    parent,
                    process,
                    _TOKEN,
                    expected_init_pid=init_pid,
                    launcher_pidfd=transferred_launcher,
                )
            except BaseException:
                os.close(transferred_launcher)
                raise
            init_lifetime = os.dup(owner._init_pidfd)
            resources.callback(os.close, init_lifetime)
            observed_lifetimes.append(init_lifetime)
            worker, result = _start_worker(
                lambda: owner.run(deadline=time.monotonic() + 10), daemon=True
            )
            yield _LiveBwrapRequest(
                owner, process, result, init_lifetime, launcher_lifetime, descendant_ready
            )
        except BaseException as error:
            if owner is None and process is not None and process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                error.add_note(
                    f"Request launcher exited {process.returncode}: "
                    f"stdout={stdout[-2000:]!r}; stderr={stderr[-2000:]!r}"
                )
            raise
        finally:
            stalled_worker = False
            try:
                if owner is not None:
                    with suppress(OSError):
                        owner.cancel()
                for descriptor in observed_lifetimes:
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                if worker is not None:
                    worker.join(timeout=10)
                    stalled_worker = worker.is_alive()
                elif owner is not None:
                    with suppress(request_control.RequestCancelled):
                        owner.run(deadline=time.monotonic())
            finally:
                if process is not None:
                    if not observed_lifetimes and process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
            assert not stalled_worker, "Request worker stalled after pinned and launcher cleanup"


@pytest.mark.skipif(shutil.which("bwrap", path=os.defpath) is None, reason="requires Bubblewrap")
@pytest.mark.parametrize("ending", ["complete", "timeout", "cancel"])
@pytest.mark.timeout(60)
def test_real_request_retires_the_namespace_and_detached_output_holders(
    live_bwrap_request: _LiveBwrapRequest, ending: str
) -> None:
    case = live_bwrap_request
    poll_until(case.descendant_ready.exists, timeout_s=5, interval_s=0.01)
    assert case.owner.admitted
    if ending == "cancel":
        assert not case.result.done()
        case.owner.cancel()
        with pytest.raises(request_control.RequestCancelled):
            case.result.result(timeout=15)
    else:
        result = case.result.result(timeout=15)
        assert result.timed_out == (ending == "timeout")
        assert result.exit_code == (7 if ending == "complete" else None)
        assert result.stdout == (
            b"descendant-ready\ncommand-complete\n"
            if ending == "complete"
            else b"descendant-ready\n"
        )
        assert b"descendant-stderr\n" in result.stderr
    assert case.owner.done.is_set()
    assert case.process.poll() is not None
    assert request_control._pidfd_exited(case.init_lifetime)
    assert request_control._pidfd_exited(case.launcher_lifetime)
