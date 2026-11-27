"""Launch one admitted shell while its reusable environment owns the brokers."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import _bwrap_request_init
from ._bwrap_request_control import BwrapRequestControl, BwrapRequestResult, RequestCancelled
from .bwrap_sandbox import BwrapRequestFds, BwrapSandboxBackend
from .sandbox import SandboxPolicy

_STARTUP_TIMEOUT_S = 10.0
_REQUEST_OPTIONS = ("--as-pid-1", "--info-fd", "--add-seccomp-fd")


def _launcher_env() -> dict[str, str]:
    return {"PATH": os.defpath, "LANG": "C"}


@functools.lru_cache(maxsize=16)
def _has_request_options(executable: str, identity: tuple[int, ...]) -> bool:
    del identity  # An executable replacement must invalidate the cached probe.
    result = subprocess.run(
        [executable, "--help"],
        env=_launcher_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=2,
        check=False,
    )
    result.check_returncode()
    return all(option in result.stdout for option in _REQUEST_OPTIONS)


def request_launcher(policy: SandboxPolicy, *, executable: str | None = None) -> str | None:
    """Select a host launcher or revalidate the one retained by the lease."""
    if (
        not sys.platform.startswith("linux")
        or not policy.active
        or policy.backend_type != "linux_bwrap"
        or policy.copy_on_write_roots
        or policy.copy_on_write_namespace is not None
        or policy.credential_proxy is not None
        or not hasattr(os, "pidfd_open")
        or not hasattr(os, "memfd_create")
        or not hasattr(signal, "pidfd_send_signal")
    ):
        return None
    if executable is None:
        executable = shutil.which("bwrap", path=os.environ.get("PATH", os.defpath))
    if executable is None or not Path(executable).is_absolute():
        return None
    try:
        status = os.stat(executable)
        descriptor = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(descriptor, 0)
        finally:
            os.close(descriptor)
        descriptor = os.memfd_create("shell-capability", os.MFD_CLOEXEC)
        os.close(descriptor)
        identity = (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)
        return executable if _has_request_options(executable, identity) else None
    except (OSError, subprocess.SubprocessError):
        return None


@functools.lru_cache(maxsize=1)
def _init_source() -> str:
    return Path(_bwrap_request_init.__file__).read_text(encoding="utf-8")


class BwrapShellRequest:
    """Keep cancellation separate from the helper lock that protects broker ownership."""

    def __init__(self, *, launcher: str | None = None) -> None:
        self.cancelled = threading.Event()
        self._launcher = launcher
        self._lock = threading.Lock()
        self._owner: BwrapRequestControl | None = None
        self._admitted = False

    @property
    def admitted(self) -> bool:
        """Include a possibly delivered admission even after its owner retires."""
        with self._lock:
            return self._admitted or (self._owner is not None and self._owner.admitted)

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            if self._owner is not None:
                self._owner.cancel()

    def run(
        self,
        *,
        argv: list[str],
        cwd: Path,
        policy: SandboxPolicy,
        command_env: dict[str, str],
        timeout: int,
        max_output: int,
        output_encoding: str,
        spawn: Callable[[list[str], dict[str, str], tuple[int, ...]], subprocess.Popen[bytes]],
    ) -> BwrapRequestResult:
        launcher = request_launcher(policy, executable=self._launcher)
        if launcher is None:
            raise OSError("Isolated shell request support changed before launch")
        if self.cancelled.is_set():
            raise RequestCancelled("Shell request was cancelled before launch")
        token = secrets.token_hex(32)
        config: dict[str, object] = {
            "token": token,
            "argv": argv,
            "cwd": str(cwd),
            "env": command_env,
            "report_encoding": True,
            "output_encoding": output_encoding,
        }
        if policy.egress_relay_port is not None or policy.egress_socket_path is not None:
            if policy.egress_relay_port is None or policy.egress_socket_path is None:
                raise ValueError("Shell request needs complete egress relay configuration")
            config["relay"] = {
                "source": str(Path(__file__).resolve().with_name("egress") / "relay.py"),
                "port": policy.egress_relay_port,
                "socket_path": policy.egress_socket_path,
            }

        owner = None
        process = None
        launcher_pidfd = None
        with contextlib.ExitStack() as resources:
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            resources.enter_context(parent)
            resources.enter_context(child)
            parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            parent.settimeout(_STARTUP_TIMEOUT_S)
            info, baseline, hardening, configuration = (
                resources.enter_context(os.fdopen(os.memfd_create(name, os.MFD_CLOEXEC), "w+b"))
                for name in ("shell-info", "shell-baseline", "shell-hardening", "shell-config")
            )
            configuration.write(json.dumps(config).encode("utf-8"))
            configuration.flush()
            configuration.seek(0)
            descriptors = BwrapRequestFds(info.fileno(), baseline.fileno(), hardening.fileno())
            wrapped = BwrapSandboxBackend().wrap_launcher_argv(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _init_source(),
                    str(child.fileno()),
                    str(configuration.fileno()),
                ],
                policy,
                cwd,
                _request_fds=descriptors,
            )
            # Probe and execute the same binary even if PATH changes concurrently.
            wrapped[0] = launcher
            try:
                process = spawn(
                    wrapped,
                    _launcher_env(),
                    (
                        child.fileno(),
                        configuration.fileno(),
                        info.fileno(),
                        baseline.fileno(),
                        hardening.fileno(),
                    ),
                )
                child.close()
                launcher_pidfd = os.pidfd_open(process.pid)
                resources.callback(os.close, launcher_pidfd)
                poller = select.poll()
                poller.register(parent.fileno(), select.POLLIN)
                startup_deadline = time.monotonic() + _STARTUP_TIMEOUT_S
                while not self.cancelled.is_set():
                    remaining = startup_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Shell request init did not publish readiness")
                    if poller.poll(max(1, round(min(remaining, 0.05) * 1000))):
                        break
                if self.cancelled.is_set():
                    raise RequestCancelled("Shell request was cancelled during startup")
                info.seek(0)
                try:
                    metadata = json.load(info)
                except json.JSONDecodeError as exc:
                    raise OSError(
                        "Shell request launcher did not publish valid startup metadata; "
                        "check Bubblewrap startup and sandbox support"
                    ) from exc
                init_pid = metadata.get("child-pid") if isinstance(metadata, dict) else None
                if type(init_pid) is not int or init_pid <= 0:
                    raise OSError("Shell request launcher did not identify its init")
                transferred_launcher = os.dup(launcher_pidfd)
                try:
                    owner = BwrapRequestControl.receive(
                        parent,
                        process,
                        token.encode("ascii"),
                        expected_init_pid=init_pid,
                        launcher_pidfd=transferred_launcher,
                        encoding_required=True,
                    )
                except BaseException:
                    os.close(transferred_launcher)
                    raise
                with self._lock:
                    self._owner = owner
                    if self.cancelled.is_set():
                        owner.cancel()
                return owner.run(deadline=time.monotonic() + timeout, max_output=max_output)
            finally:
                with self._lock:
                    if owner is not None and owner.admitted:
                        self._admitted = True
                    self._owner = None
                with contextlib.ExitStack() as streams:
                    if process is not None:
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                streams.callback(stream.close)
                    if owner is None and process is not None:
                        # No command was admitted. Closing the owner channel ends the init.
                        parent.close()
                        try:
                            process.communicate(timeout=1)
                        except subprocess.TimeoutExpired:
                            if launcher_pidfd is not None:
                                with contextlib.suppress(ProcessLookupError):
                                    signal.pidfd_send_signal(launcher_pidfd, signal.SIGKILL)
                            else:
                                process.kill()
                            process.communicate()
