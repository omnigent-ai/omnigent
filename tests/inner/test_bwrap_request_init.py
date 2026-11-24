"""Admission and result contracts with all process and kernel calls replaced."""

from __future__ import annotations

import array
import errno
import io
import json
import signal
import socket
import subprocess
from unittest.mock import MagicMock

import pytest

from omnigent.inner import _bwrap_request_init as request_init

pytestmark = pytest.mark.posix_only

_TOKEN = b"5" * 64
_CONFIG = request_init._RequestConfig(_TOKEN, ("/bin/sh", "-c", "true"), "/workspace", {})


@pytest.fixture
def init_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    protection = MagicMock()
    monkeypatch.setattr(request_init, "_protect_init", protection)
    monkeypatch.setattr(request_init.os, "pidfd_open", MagicMock(return_value=41), raising=False)
    monkeypatch.setattr(request_init.os, "close", MagicMock())
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 73
    launch = MagicMock(return_value=process)
    monkeypatch.setattr(request_init.subprocess, "Popen", launch)
    return protection, launch


@pytest.mark.parametrize("admission", [b"", b"admit:" + b"6" * 64, b"admit:" + _TOKEN + b"x"])
def test_init_never_starts_a_command_without_its_exact_admission(
    monkeypatch: pytest.MonkeyPatch,
    init_calls: tuple[MagicMock, MagicMock],
    admission: bytes,
) -> None:
    control = MagicMock(spec=socket.socket)
    control.sendmsg.return_value = len(request_init._READY + _TOKEN)
    control.recv.return_value = admission
    monkeypatch.setattr(request_init.os, "waitpid", lambda *_: pytest.fail("No command exists"))
    with pytest.raises(ConnectionError, match="not admitted"):
        request_init._run_request(control, _CONFIG)
    init_calls[0].assert_called_once()
    init_calls[1].assert_not_called()
    request_init.os.close.assert_called_once_with(41)


@pytest.mark.parametrize("status", [7 << 8, signal.SIGTERM])
def test_init_exports_its_handle_before_admission_and_reports_the_exact_child_result(
    monkeypatch: pytest.MonkeyPatch,
    init_calls: tuple[MagicMock, MagicMock],
    status: int,
) -> None:
    events: list[str] = []
    protection, launch = init_calls
    protection.side_effect = lambda: events.append("protected")
    control = MagicMock(spec=socket.socket)

    def ready(buffers: list[bytes], ancillary: list[tuple[int, int, array.array]]) -> int:
        assert events == ["protected"]
        assert buffers == [b"ready:" + _TOKEN]
        assert [(level, kind, list(values)) for level, kind, values in ancillary] == [
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, [41])
        ]
        launch.assert_not_called()
        events.append("ready")
        return len(buffers[0])

    def admit(_size: int) -> bytes:
        assert events == ["protected", "ready"]
        request_init.os.close.assert_called_once_with(41)
        launch.assert_not_called()
        events.append("admitted")
        return b"admit:" + _TOKEN

    control.sendmsg.side_effect = ready
    control.recv.side_effect = admit
    expected_exit = 7 if status == 7 << 8 else -signal.SIGTERM
    result = b"exit:" + _TOKEN + b":" + str(expected_exit).encode()
    control.send.return_value = len(result)
    monkeypatch.setattr(request_init.os, "waitpid", MagicMock(side_effect=[(91, 0), (73, status)]))
    request_init._run_request(control, _CONFIG)
    launch.assert_called_once_with(_CONFIG.argv, cwd="/workspace", env={}, close_fds=True)
    assert events == ["protected", "ready", "admitted"]
    control.set_inheritable.assert_called_once_with(False)
    control.send.assert_called_once_with(result)
    assert launch.return_value.returncode == expected_exit


@pytest.mark.parametrize("failure", ["protection", "handle", "ready", "timeout"])
def test_setup_failure_cannot_admit_a_command(
    monkeypatch: pytest.MonkeyPatch,
    init_calls: tuple[MagicMock, MagicMock],
    failure: str,
) -> None:
    protection, launch = init_calls
    control = MagicMock(spec=socket.socket)
    control.sendmsg.return_value = len(b"ready:" + _TOKEN)
    control.recv.return_value = b"admit:" + _TOKEN
    if failure == "protection":
        protection.side_effect = OSError(errno.EPERM, "protection failed")
    elif failure == "handle":
        request_init.os.pidfd_open.side_effect = OSError(errno.ENOSYS, "unsupported")
    elif failure == "ready":
        control.sendmsg.return_value = 1
    else:
        control.recv.side_effect = TimeoutError("No admission")
    with pytest.raises(OSError):
        request_init._run_request(control, _CONFIG)
    launch.assert_not_called()
    control.send.assert_not_called()
    if failure in {"ready", "timeout"}:
        request_init.os.close.assert_called_once_with(41)


@pytest.mark.parametrize("frame", [b"", b"admit:" + _TOKEN])
def test_owner_channel_loss_or_repeated_admission_exits_the_init(
    monkeypatch: pytest.MonkeyPatch, frame: bytes
) -> None:
    control = MagicMock(spec=socket.socket)
    control.recv.return_value = frame
    monkeypatch.setattr(request_init.os, "waitpid", lambda *_: (0, 0))
    monkeypatch.setattr(request_init.select, "select", lambda *_: ([control], [], []))
    with pytest.raises(ConnectionError, match="owner channel ended"):
        request_init._wait_for_command(control, 73)
    control.send.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [{"token": "z" * 64}, {"argv": []}, {"cwd": "relative"}, {"env": {"BAD=KEY": "x"}}],
)
def test_config_is_validated_before_readiness(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    config = {"token": _TOKEN.decode(), "argv": ["/bin/sh", ""], "cwd": "/workspace", "env": {}}
    config.update(change)
    monkeypatch.setattr(
        request_init.os, "fdopen", lambda *_a, **_k: io.StringIO(json.dumps(config))
    )
    with pytest.raises(ValueError):
        request_init._read_request_config(41)


def test_config_preserves_exact_command_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {
        "token": _TOKEN.decode(),
        "argv": ["/bin/sh", "-c", "  printf 'line 1\\nline 2'  "],
        "cwd": "/workspace/with space",
        "env": {"EXPLICIT_VALUE": "  value\nnext line  ", "EMPTY_VALUE": ""},
    }
    monkeypatch.setattr(
        request_init.os, "fdopen", lambda *_a, **_k: io.StringIO(json.dumps(config))
    )
    result = request_init._read_request_config(41)
    assert result.argv == tuple(config["argv"])
    assert result.cwd == config["cwd"] and result.env == config["env"]


def test_init_cannot_run_outside_namespace_pid_one(monkeypatch: pytest.MonkeyPatch) -> None:
    library = MagicMock()
    monkeypatch.setattr(request_init.os, "getpid", lambda: 73)
    monkeypatch.setattr(request_init.ctypes, "CDLL", library)
    with pytest.raises(OSError, match="own Linux PID namespace"):
        request_init._protect_init()
    library.assert_not_called()


@pytest.mark.parametrize("failed_option", [None, 1, 4])
def test_parent_death_and_control_protection_precede_readiness(
    monkeypatch: pytest.MonkeyPatch, failed_option: int | None
) -> None:
    library = MagicMock()
    library.prctl.side_effect = lambda option, *_: -1 if option == failed_option else 0
    monkeypatch.setattr(request_init.sys, "platform", "linux")
    monkeypatch.setattr(request_init.os, "getpid", lambda: 1)
    monkeypatch.setattr(request_init.ctypes, "CDLL", lambda *_a, **_k: library)
    monkeypatch.setattr(request_init.ctypes, "get_errno", lambda: errno.EPERM)
    if failed_option is None:
        request_init._protect_init()
    else:
        with pytest.raises(OSError) as caught:
            request_init._protect_init()
        assert caught.value.errno == errno.EPERM
    calls = [call.args for call in library.prctl.call_args_list]
    assert calls == [(1, signal.SIGKILL, 0, 0, 0)] + (
        [] if failed_option == 1 else [(4, 0, 0, 0, 0)]
    )
