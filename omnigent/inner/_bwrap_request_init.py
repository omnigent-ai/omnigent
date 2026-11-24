"""Trusted PID-namespace init for an explicitly admitted shell request.

This bootstrap uses only the standard library so its source can be passed to
an isolated interpreter. It is not an entry point for the current shell path.
"""

from __future__ import annotations

import array
import ctypes
import json
import os
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


def _validate_control_socket(control: socket.socket) -> None:
    """Require packet framing and connection-oriented owner-loss notification."""
    if (
        control.family != socket.AF_UNIX
        or control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
    ):
        raise ValueError("Request control requires AF_UNIX SOCK_SEQPACKET")


@dataclass(frozen=True)
class _RequestConfig:
    token: bytes
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]


def _read_request_config(descriptor: int) -> _RequestConfig:
    with os.fdopen(descriptor, encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError("Request configuration must be an object")
    token = config.get("token")
    argv = config.get("argv")
    cwd = config.get("cwd")
    env = config.get("env")
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
    return _RequestConfig(token.encode("ascii"), tuple(argv), cwd, dict(env))


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
    poller = select.poll()
    poller.register(control.fileno(), select.POLLIN)
    while True:
        while True:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == command_pid:
                return os.waitstatus_to_exitcode(status)
            if pid == 0:
                break
        if poller.poll(round(_OWNER_POLL_S * 1000)):
            # EOF or any additional frame means the sole admission channel ended.
            control.recv(1)
            raise ConnectionError("Request owner channel ended")


def _run_request(control: socket.socket, config: _RequestConfig) -> None:
    _validate_control_socket(control)
    _protect_init()
    control.set_inheritable(False)
    control.settimeout(_ADMISSION_TIMEOUT_S)
    # The owner receives the actual init's handle, without a numeric-PID race.
    descriptor = os.pidfd_open(1)
    try:
        ready = _READY + config.token
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
