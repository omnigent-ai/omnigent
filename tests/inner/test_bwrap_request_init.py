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


def _control_socket() -> MagicMock:
    control = MagicMock(spec=socket.socket)
    control.family = socket.AF_UNIX
    control.getsockopt.return_value = socket.SOCK_SEQPACKET
    control.fileno.return_value = 17
    return control


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
    control = _control_socket()
    control.sendmsg.return_value = len(request_init._READY + _TOKEN)
    control.recv.return_value = admission
    monkeypatch.setattr(request_init.os, "waitpid", lambda *_: pytest.fail("No command exists"))
    with pytest.raises(ConnectionError, match="not admitted"):
        request_init._run_request(control, _CONFIG)
    init_calls[0].assert_called_once()
    init_calls[1].assert_not_called()
    request_init.os.close.assert_called_once_with(41)


@pytest.mark.parametrize(
    ("control_fd", "status", "delivery"),
    [
        (17, 7 << 8, "complete"),
        (17, signal.SIGTERM, "complete"),
        (4096, 7 << 8, "complete"),
        (17, 7 << 8, "short"),
        (17, 7 << 8, "error"),
    ],
)
def test_init_exports_its_handle_before_admission_and_reports_the_exact_child_result(
    monkeypatch: pytest.MonkeyPatch,
    init_calls: tuple[MagicMock, MagicMock],
    status: int,
    control_fd: int,
    delivery: str,
) -> None:
    events: list[str] = []
    protection, launch = init_calls
    protection.side_effect = lambda: events.append("protected")
    control = _control_socket()
    control.fileno.return_value = control_fd
    poller = MagicMock()
    poller.poll.return_value = []
    monkeypatch.setattr(request_init.select, "poll", lambda: poller, raising=False)

    def limited_select(channels: list[MagicMock], *_args: object) -> tuple[list, list, list]:
        if channels[0].fileno() >= 1024:
            raise ValueError("filedescriptor out of range in select()")
        return [], [], []

    monkeypatch.setattr(request_init.select, "select", limited_select)

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
    control.send.return_value = len(result) if delivery == "complete" else len(result) - 1
    if delivery == "error":
        control.send.side_effect = OSError(errno.EPIPE, "result transport closed")
    monkeypatch.setattr(
        request_init.os, "waitpid", MagicMock(side_effect=[(91, 0), (0, 0), (73, status)])
    )
    if delivery == "complete":
        request_init._run_request(control, _CONFIG)
    else:
        with pytest.raises(OSError, match="result"):
            request_init._run_request(control, _CONFIG)
    launch.assert_called_once_with(_CONFIG.argv, cwd="/workspace", env={}, close_fds=True)
    assert events == ["protected", "ready", "admitted"]
    control.set_inheritable.assert_called_once_with(False)
    control.send.assert_called_once_with(result)
    assert launch.return_value.returncode == expected_exit
    poller.register.assert_called_once_with(control_fd, request_init.select.POLLIN)
    poller.poll.assert_called_once_with(50)


def test_child_interrupt_cannot_replace_the_commands_exact_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = 7 << 8
    handlers: dict[int, object] = {signal.SIGINT: signal.default_int_handler}

    def install_handler(signum: int, handler: object) -> object:
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(request_init.sys, "platform", "linux")
    monkeypatch.setattr(request_init.os, "getpid", lambda: 1)
    library = MagicMock()
    library.prctl.return_value = 0
    monkeypatch.setattr(request_init.ctypes, "CDLL", lambda *_a, **_k: library)
    monkeypatch.setattr(request_init.signal, "signal", install_handler)
    monkeypatch.setattr(request_init.os, "pidfd_open", lambda *_: 41, raising=False)
    monkeypatch.setattr(request_init.os, "close", MagicMock())
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 73
    monkeypatch.setattr(request_init.subprocess, "Popen", MagicMock(return_value=process))
    control = _control_socket()
    monkeypatch.setattr(request_init.select, "poll", lambda: MagicMock(), raising=False)
    control.sendmsg.return_value = len(b"ready:" + _TOKEN)
    control.recv.return_value = b"admit:" + _TOKEN
    exit_code = 7
    result = b"exit:" + _TOKEN + b":" + str(exit_code).encode()
    control.send.return_value = len(result)

    def command_interrupt(*_args: object) -> tuple[int, int]:
        handler = handlers[signal.SIGINT]
        if callable(handler):
            try:
                handler(signal.SIGINT, None)
            except KeyboardInterrupt:
                pytest.fail("Command-originated SIGINT would interrupt init")
        assert handler == signal.SIG_DFL, "Ignoring SIGINT would change the exec'd command"
        return 73, status

    monkeypatch.setattr(request_init.os, "waitpid", command_interrupt)
    request_init._run_request(control, _CONFIG)
    control.send.assert_called_once_with(result)
    assert process.returncode == exit_code


@pytest.mark.parametrize("failure", ["protection", "handle", "ready", "timeout"])
def test_setup_failure_cannot_admit_a_command(
    monkeypatch: pytest.MonkeyPatch,
    init_calls: tuple[MagicMock, MagicMock],
    failure: str,
) -> None:
    protection, launch = init_calls
    control = _control_socket()
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


@pytest.mark.parametrize("transport", ["family", "stream", "datagram"])
def test_control_transport_must_frame_messages_and_report_owner_loss(
    init_calls: tuple[MagicMock, MagicMock], transport: str
) -> None:
    control = _control_socket()
    if transport == "family":
        control.family = socket.AF_INET
    else:
        control.getsockopt.return_value = (
            socket.SOCK_STREAM if transport == "stream" else socket.SOCK_DGRAM
        )
    with pytest.raises(ValueError, match="AF_UNIX SOCK_SEQPACKET"):
        request_init._run_request(control, _CONFIG)
    init_calls[0].assert_not_called()
    init_calls[1].assert_not_called()
    control.sendmsg.assert_not_called()


@pytest.mark.parametrize("frame", [b"", b"admit:" + _TOKEN])
def test_owner_channel_loss_or_repeated_admission_exits_the_init(
    monkeypatch: pytest.MonkeyPatch, frame: bytes
) -> None:
    control = _control_socket()
    control.recv.return_value = frame
    monkeypatch.setattr(request_init.os, "waitpid", lambda *_: (0, 0))
    poller = MagicMock()
    poller.poll.return_value = [(control.fileno(), 1)]
    monkeypatch.setattr(request_init.select, "poll", lambda: poller, raising=False)
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


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "aliased",
        "stdio",
        "overflow",
        "not-integer",
        "config-failure",
        "command-failure",
        "success",
    ],
)
def test_entry_point_rejects_invalid_descriptors_and_always_exits_its_socket_context(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    invalid = {
        "missing": ["41"],
        "aliased": ["41", "41"],
        "stdio": ["0", "42"],
        "overflow": ["41", "2147483648"],
        "not-integer": ["invalid", "42"],
    }
    control = _control_socket()
    control.__enter__.return_value = control
    open_control = MagicMock(return_value=control)
    read_config = MagicMock(return_value=_CONFIG)
    run = MagicMock()
    if mode == "config-failure":
        read_config.side_effect = ValueError("Invalid configuration")
    elif mode == "command-failure":
        run.side_effect = OSError("Request failed")
    monkeypatch.setattr(request_init.socket, "socket", open_control)
    monkeypatch.setattr(request_init, "_read_request_config", read_config)
    monkeypatch.setattr(request_init, "_run_request", run)
    assert request_init.main(invalid.get(mode, ["41", "42"])) == (0 if mode == "success" else 125)
    if mode in invalid:
        open_control.assert_not_called()
        read_config.assert_not_called()
        run.assert_not_called()
        return
    open_control.assert_called_once_with(fileno=41)
    control.__enter__.assert_called_once()
    control.__exit__.assert_called_once()
    read_config.assert_called_once_with(42)
    if mode == "config-failure":
        run.assert_not_called()
    else:
        run.assert_called_once_with(control, _CONFIG)


def test_init_cannot_run_outside_namespace_pid_one(monkeypatch: pytest.MonkeyPatch) -> None:
    library = MagicMock()
    configure_signal = MagicMock()
    monkeypatch.setattr(request_init.signal, "signal", configure_signal)
    monkeypatch.setattr(request_init.os, "getpid", lambda: 73)
    monkeypatch.setattr(request_init.ctypes, "CDLL", library)
    with pytest.raises(OSError, match="own Linux PID namespace"):
        request_init._protect_init()
    library.assert_not_called()
    configure_signal.assert_not_called()


@pytest.mark.parametrize("failed_option", [None, 1, 4])
def test_parent_death_and_control_protection_precede_readiness(
    monkeypatch: pytest.MonkeyPatch, failed_option: int | None
) -> None:
    library = MagicMock()
    configure_signal = MagicMock()

    def protect(option: int, *_args: object) -> int:
        configure_signal.assert_called_once_with(signal.SIGINT, signal.SIG_DFL)
        return -1 if option == failed_option else 0

    library.prctl.side_effect = protect
    monkeypatch.setattr(request_init.signal, "signal", configure_signal)
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
