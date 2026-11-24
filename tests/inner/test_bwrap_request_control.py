"""Parent admission/lifetime behavior using inert socket, pidfd and process doubles."""

from __future__ import annotations

import array
import os
import select
import socket
import subprocess
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.inner import _bwrap_request_control as request_control

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux pidfd and credential metadata"
)

_TOKEN = b"5" * 64
_INIT_FD = 41
_LAUNCHER_FD = 42


@pytest.fixture
def readiness(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    control = MagicMock(spec=socket.socket)
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
        "extra-handle",
        "truncated",
        "unexpected-before-rights",
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
    if problem == "token":
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
    elif problem in {"not-pid-one", "dead-pidfd"}:
        identities = {
            _LAUNCHER_FD: (83, (83,)),
            _INIT_FD: (73, (73, 2)) if problem == "not-pid-one" else (-1, ()),
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
    with pytest.raises(OSError):
        request_control.BwrapRequestControl.receive(
            control, process, _TOKEN, expected_init_pid=73, launcher_pidfd=_LAUNCHER_FD
        )
    assert sorted(call.args[0] for call in close.call_args_list) == sorted(handles)
    control.send.assert_not_called()


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
    control.send.side_effect = len
    control.recv.return_value = b"exit:" + _TOKEN + b":7"
    process = MagicMock(spec=subprocess.Popen)
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


def test_outer_exit_and_output_cannot_substitute_for_init_completion(
    monkeypatch: pytest.MonkeyPatch, request_case: _RequestCase
) -> None:
    case = request_case

    def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
        case.exited[_LAUNCHER_FD] = True
        return b"exact stdout\n", b"exact stderr\n"

    def exited(descriptor: int, timeout_ms: int = 0) -> bool:
        if descriptor == _INIT_FD and timeout_ms:
            assert ("close", _INIT_FD) not in case.events
            assert not case.owner.done.is_set()
            case.events.append(("init-complete", descriptor))
            case.exited[descriptor] = True
        return case.exited[descriptor]

    case.process.communicate.side_effect = communicate
    monkeypatch.setattr(request_control, "_pidfd_exited", exited)
    result = case.owner.run(deadline=1.0)
    assert result == request_control.BwrapRequestResult(
        b"exact stdout\n", b"exact stderr\n", 7, False
    )
    assert case.events.index(("init-complete", _INIT_FD)) < case.events.index(("close", _INIT_FD))
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)
    assert case.owner.admitted and case.owner.done.is_set()


def test_cancel_before_admission_executes_nothing_and_retires_only_pinned_handles(
    request_case: _RequestCase,
) -> None:
    case = request_case
    case.owner.cancel()
    with pytest.raises(request_control.RequestCancelled):
        case.owner.run(deadline=1.0)
    assert not case.owner.admitted and case.owner.done.is_set()
    case.control.send.assert_not_called()
    events = list(case.events)
    case.owner.cancel()
    assert case.events == events


def test_descriptor_close_failure_cannot_make_later_cancellation_signal_a_reused_fd(
    monkeypatch: pytest.MonkeyPatch, request_case: _RequestCase
) -> None:
    case = request_case

    def close(descriptor: int) -> None:
        case.events.append(("close", descriptor))
        if descriptor == _LAUNCHER_FD:
            raise OSError("Descriptor close failed")

    monkeypatch.setattr(request_control.os, "close", close)
    with pytest.raises(OSError, match="Descriptor close failed"):
        case.owner.run(deadline=1.0)
    assert case.owner.done.is_set()
    assert ("close", _INIT_FD) in case.events
    case.control.close.assert_called_once()
    events = list(case.events)
    case.owner.cancel()
    assert case.events == events


def test_timeout_preserves_partial_output_and_waits_for_both_pinned_lifetimes(
    monkeypatch: pytest.MonkeyPatch, request_case: _RequestCase
) -> None:
    case = request_case
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(request_control, "monotonic", lambda: next(clock))
    result = case.owner.run(deadline=1.0)
    assert result.timed_out and result.exit_code is None
    assert (result.stdout, result.stderr) == (b"exact stdout\n", b"exact stderr\n")
    assert ("kill", _INIT_FD) in case.events and ("kill", _LAUNCHER_FD) in case.events
    assert case.owner.admitted and case.owner.done.is_set()
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)


def test_output_polling_can_wait_without_replaying_or_finishing_the_request(
    request_case: _RequestCase,
) -> None:
    case = request_case
    polls = 0

    def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
        nonlocal polls
        polls += 1
        if polls == 1:
            assert not case.owner.done.is_set()
            raise subprocess.TimeoutExpired("request-launcher", 0.05)
        case.exited.update({_INIT_FD: True, _LAUNCHER_FD: True})
        return b"delayed stdout\n", b""

    case.process.communicate.side_effect = communicate
    result = case.owner.run(deadline=1.0)
    assert result.stdout == b"delayed stdout\n" and result.exit_code == 7
    case.control.send.assert_called_once_with(b"admit:" + _TOKEN)
    assert case.owner.done.is_set() and not result.timed_out


def test_caller_cancellation_during_output_wait_retains_cleanup_without_replay(
    request_case: _RequestCase,
) -> None:
    case = request_case

    def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
        case.owner.cancel()
        return b"partial stdout\n", b""

    case.process.communicate.side_effect = communicate
    with pytest.raises(request_control.RequestCancelled, match="must not be replayed"):
        case.owner.run(deadline=1.0)
    assert case.owner.admitted and case.owner.done.is_set()
    assert ("kill", _INIT_FD) in case.events and ("kill", _LAUNCHER_FD) in case.events
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
    ("flags", "expected"),
    [(0, False), (select.POLLIN, True), (select.POLLHUP, True), (select.POLLNVAL, None)],
)
def test_kernel_exit_observation_rejects_invalid_descriptors(
    monkeypatch: pytest.MonkeyPatch, flags: int, expected: bool | None
) -> None:
    poller = MagicMock()
    poller.poll.return_value = [(2048, flags)] if flags else []
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
