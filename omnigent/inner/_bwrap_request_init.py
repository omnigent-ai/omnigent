"""Trusted PID-namespace init for an explicitly admitted shell request.

This bootstrap uses only the standard library so its source can be passed to
an isolated interpreter without importing the application's dependency tree.
"""

from __future__ import annotations

import array
import codecs
import ctypes
import json
import locale
import logging
import os
import runpy
import select
import signal
import socket
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

_READY = b"ready:"
_ADMIT = b"admit:"
_EXIT = b"exit:"
_ADMISSION_TIMEOUT_S = 10.0
_OWNER_POLL_S = 0.05
_INIT_FAILURE = 125
_PR_SET_PDEATHSIG = 1
_PR_SET_DUMPABLE = 4
_RELAY_MODULE = "_omnigent_shell_request_relay"


def _validate_control_socket(control: socket.socket) -> None:
    """Require packet framing and connection-oriented owner-loss notification."""
    if (
        control.family != socket.AF_UNIX
        or control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
    ):
        raise ValueError("Request control requires AF_UNIX SOCK_SEQPACKET")


@dataclass(frozen=True)
class _RelayConfig:
    source: str
    port: int
    socket_path: str


@dataclass(frozen=True)
class _RequestConfig:
    token: bytes
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    relay: _RelayConfig | None = None
    report_encoding: bool = False
    output_encoding: str | None = None


def _read_request_config(descriptor: int) -> _RequestConfig:
    with os.fdopen(descriptor, encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError("Request configuration must be an object")
    token = config.get("token")
    argv = config.get("argv")
    cwd = config.get("cwd")
    env = config.get("env")
    relay = config.get("relay")
    report_encoding = config.get("report_encoding", False)
    output_encoding = config.get("output_encoding")
    if (
        not isinstance(token, str)
        or len(token) != 64
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise ValueError("Request admission token is invalid")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(argument, str) and "\0" not in argument for argument in argv)
        or not argv[0]
    ):
        raise ValueError("Request command is invalid")
    if not isinstance(cwd, str) or "\0" in cwd or not os.path.isabs(cwd):
        raise ValueError("Request working directory must be absolute")
    if not isinstance(env, dict) or not all(
        isinstance(key, str)
        and key
        and "=" not in key
        and "\0" not in key
        and isinstance(value, str)
        and "\0" not in value
        for key, value in env.items()
    ):
        raise ValueError("Request environment is invalid")
    if not isinstance(report_encoding, bool):
        raise ValueError("Request output encoding option is invalid")
    if output_encoding is not None:
        if (
            not isinstance(output_encoding, str)
            or not output_encoding.isascii()
            or not 1 <= len(output_encoding) <= 128
        ):
            raise ValueError("Request output encoding is invalid")
        try:
            codecs.lookup(output_encoding)
        except LookupError as exc:
            raise ValueError("Request output encoding is invalid") from exc
    relay_config = None
    if relay is not None:
        if not isinstance(relay, dict):
            raise ValueError("Request relay configuration is invalid")
        source, port, socket_path = (
            relay.get("source"),
            relay.get("port"),
            relay.get("socket_path"),
        )
        if (
            not isinstance(source, str)
            or "\0" in source
            or not os.path.isabs(source)
            or type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(socket_path, str)
            or "\0" in socket_path
            or not os.path.isabs(socket_path)
        ):
            raise ValueError("Request relay configuration is invalid")
        relay_config = _RelayConfig(source, port, socket_path)
    return _RequestConfig(
        token.encode("ascii"),
        tuple(argv),
        cwd,
        dict(env),
        relay_config,
        report_encoding,
        output_encoding,
    )


def _protect_init() -> None:
    if not sys.platform.startswith("linux") or os.getpid() != 1:
        raise OSError("Request init requires its own Linux PID namespace")
    # Keep PID1's default protection without inheriting SIG_IGN into the command.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    library = ctypes.CDLL(None, use_errno=True)
    prctl = library.prctl
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl.restype = ctypes.c_int
    # Parent death ends the namespace; non-dumpability protects the control fd.
    for option, value in ((_PR_SET_PDEATHSIG, signal.SIGKILL), (_PR_SET_DUMPABLE, 0)):
        if prctl(option, value, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Could not protect request init")


def _wait_for_command(control: socket.socket, command_pid: int) -> int:
    descriptor = os.pidfd_open(command_pid)
    try:
        poller = select.poll()
        poller.register(control.fileno(), select.POLLIN)
        poller.register(descriptor, select.POLLIN)
        while True:
            while True:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == command_pid:
                    return os.waitstatus_to_exitcode(status)
                if pid == 0:
                    break
            # Command exit wakes immediately; the timeout still reaps other orphans.
            for ready, flags in poller.poll(round(_OWNER_POLL_S * 1000)):
                if ready == control.fileno():
                    # EOF or any additional frame means the sole admission channel ended.
                    control.recv(1)
                    raise ConnectionError("Request owner channel ended")
                if flags & (select.POLLERR | select.POLLNVAL):
                    raise OSError("Command lifetime descriptor is invalid")
    finally:
        os.close(descriptor)


def _run_request(control: socket.socket, config: _RequestConfig) -> None:
    _validate_control_socket(control)
    _protect_init()
    control.set_inheritable(False)
    control.settimeout(_ADMISSION_TIMEOUT_S)
    if config.relay is not None:
        # Relay diagnostics share these pipes with command output.
        relay_logger = logging.getLogger(_RELAY_MODULE)
        relay_logger.handlers = [logging.NullHandler()]
        relay_logger.propagate = False
        # Reuse the maintained stdlib relay inside this request's network namespace.
        start_relay = runpy.run_path(config.relay.source, run_name=_RELAY_MODULE)["start_relay"]
        relay_ready = start_relay(config.relay.port, config.relay.socket_path)
        if not relay_ready.wait(_ADMISSION_TIMEOUT_S):
            raise TimeoutError("Shell request relay did not become ready")
    # The owner receives the actual init's handle, without a numeric-PID race.
    descriptor = os.pidfd_open(1)
    try:
        ready = _READY + config.token
        if config.report_encoding:
            encoding = config.output_encoding or (
                "utf-8" if sys.flags.utf8_mode else locale.getencoding()
            )
            ready += b":" + encoding.encode("ascii")
        sent = control.sendmsg(
            [ready],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))],
        )
        if sent != len(ready):
            raise OSError("Request readiness was not delivered")
    finally:
        os.close(descriptor)
    admission = control.recv(len(_ADMIT) + len(config.token) + 1)
    if admission != _ADMIT + config.token:
        raise ConnectionError("Request was not admitted")
    control.settimeout(None)
    process = subprocess.Popen(config.argv, cwd=config.cwd, env=config.env, close_fds=True)
    exit_code = _wait_for_command(control, process.pid)
    process.returncode = exit_code
    result = _EXIT + config.token + b":" + str(exit_code).encode("ascii")
    if control.send(result) != len(result):
        raise OSError("Request result was not delivered")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) != 2:
            raise ValueError("Request init needs control and configuration descriptors")
        control_fd, config_fd = (int(argument) for argument in arguments)
        if control_fd == config_fd or any(
            not 3 <= descriptor <= 2_147_483_647 for descriptor in (control_fd, config_fd)
        ):
            raise ValueError("Request init descriptors are invalid")
        with socket.socket(fileno=control_fd) as control:
            config = _read_request_config(config_fd)
            _run_request(control, config)
    except (OSError, ValueError):
        return _INIT_FAILURE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
