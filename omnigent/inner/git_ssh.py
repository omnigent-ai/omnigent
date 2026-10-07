"""Repository-scoped Git SSH broker and sandbox-side Git transport helper."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, cast

from . import git_ssh_wire
from .datamodel import GitSshBinding
from .egress.proxy import _CLOUD_TRAP_NETWORKS
from .git_ssh_wire import (
    MAX_FRAME as _MAX_FRAME,
)
from .git_ssh_wire import (
    SOCKET_ENV as _SOCKET_ENV,
)
from .git_ssh_wire import (
    TOKEN_ENV as _TOKEN_ENV,
)
from .git_ssh_wire import (
    GitSshDenied,
)
from .git_ssh_wire import (
    read_frame as _read_frame,
)
from .git_ssh_wire import (
    write_frame as _write_frame,
)

logger = logging.getLogger(__name__)

_SOCKET_NAME = ".gitssh.sock"
_MAX_REQUEST = 8192
_MAX_HOST_KEY_FILE = 16384
_MAX_TRANSFER = 2 * 1024 * 1024 * 1024
_MAX_CONNECTIONS = 8
_SESSION_TIMEOUT = 1800
_HANDSHAKE_TIMEOUT = 10
_BLOCKED_CONTROL_ADDRESSES = (ipaddress.ip_network("100.100.100.200/32"),)


def normalize_git_ssh_bindings(bindings: Sequence[GitSshBinding]) -> list[GitSshBinding]:
    """Apply the same validation to YAML and programmatic bindings."""
    from .git_ssh_policy import parse_git_ssh_bindings

    parsed = parse_git_ssh_bindings(
        [
            {
                "host": binding.host,
                "port": binding.port,
                "username": binding.username,
                "repository": binding.repository,
                "operations": list(binding.operations),
                "identity_file": binding.identity_file,
                "known_hosts_file": binding.known_hosts_file,
                "allowed_cidrs": list(binding.allowed_cidrs),
                "allow_loopback": binding.allow_loopback,
            }
            for binding in bindings
        ]
    )
    return parsed or []


def _parse_git_argv(argv: object, bindings: Sequence[GitSshBinding]) -> tuple[GitSshBinding, str]:
    if not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
        raise GitSshDenied("Git SSH arguments are invalid")
    args = list(argv)
    port: int | None = None
    while args and args[0].startswith("-"):
        if args[:2] == ["-o", "SendEnv=GIT_PROTOCOL"]:
            del args[:2]
        elif len(args) >= 2 and args[0] == "-p" and port is None:
            try:
                port = int(args[1])
            except ValueError as exc:
                raise GitSshDenied("Git SSH port is invalid") from exc
            del args[:2]
        else:
            raise GitSshDenied("Git SSH option is not permitted")
    if len(args) != 2 or "@" not in args[0]:
        raise GitSshDenied("Git SSH expects one user@host and one Git command")
    username, host = args[0].split("@", 1)
    if not host or host.startswith("-"):
        raise GitSshDenied("Git SSH host is invalid")
    try:
        command = shlex.split(args[1], posix=True)
    except ValueError as exc:
        raise GitSshDenied("Git SSH command is malformed") from exc
    if len(command) != 2 or command[0] not in ("git-upload-pack", "git-receive-pack"):
        raise GitSshDenied("Only Git fetch and push commands are permitted")
    operation = "fetch" if command[0] == "git-upload-pack" else "push"
    for binding in bindings:
        if (
            username == binding.username
            and host.lower() == binding.host.lower()
            and (port is None or port == binding.port)
            and command[1] == binding.repository
            and operation in binding.operations
        ):
            return binding, command[0]
    raise GitSshDenied("Git SSH destination, repository, or operation is not allowed")


def _resolve_pinned_ip(binding: GitSshBinding) -> str:
    try:
        answers = socket.getaddrinfo(binding.host, binding.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise GitSshDenied("Git SSH destination could not be resolved") from exc
    cidrs = tuple(ipaddress.ip_network(value) for value in binding.allowed_cidrs)
    chosen: str | None = None
    for family, _, _, _, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = ipaddress.ip_address(cast(str, sockaddr[0]).split("%")[0])
        if (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None)
            or (address.is_loopback and not binding.allow_loopback)
        ):
            raise GitSshDenied("Git SSH destination resolved to a blocked address")
        if any(
            address in network for network in (*_CLOUD_TRAP_NETWORKS, *_BLOCKED_CONTROL_ADDRESSES)
        ):
            raise GitSshDenied("Git SSH destination resolved to a blocked address")
        allowed = any(address in network for network in cidrs) if cidrs else address.is_global
        if not allowed:
            raise GitSshDenied("Git SSH destination resolved outside its allowed addresses")
        if chosen is None:
            chosen = str(address)
    if chosen is None:
        raise GitSshDenied("Git SSH destination has no usable address")
    return chosen


def _ssh_command(binding: GitSshBinding, pinned_ip: str, git_command: str) -> list[str]:
    ssh = Path("/usr/bin/ssh")
    if not ssh.is_file():
        raise GitSshDenied("OpenSSH client is unavailable at /usr/bin/ssh")
    metadata = ssh.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise GitSshDenied("OpenSSH client at /usr/bin/ssh is not trusted")
    return [
        str(ssh),
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={binding.known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"HostKeyAlias={binding.host}",
        "-o",
        "CheckHostIP=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "SendEnv=GIT_PROTOCOL",
        "-i",
        binding.identity_file,
        "-p",
        str(binding.port),
        "--",
        f"{binding.username}@{pinned_ip}",
        f"{git_command} {shlex.quote(binding.repository)}",
    ]


def _validate_pinned_host_key(binding: GitSshBinding) -> None:
    try:
        with Path(binding.known_hosts_file).open("rb") as source:
            data = source.read(_MAX_HOST_KEY_FILE + 1)
        if len(data) > _MAX_HOST_KEY_FILE:
            raise GitSshDenied("Git SSH pinned host key file is too large")
        content = data.decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise GitSshDenied("Git SSH pinned host key file is unavailable") from exc
    lines = [
        line.split()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1 or len(lines[0]) < 3 or lines[0][0].lower() != binding.host.lower():
        raise GitSshDenied("Git SSH requires one exact pinned host key entry")
    if lines[0][1] not in (
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-rsa",
    ):
        raise GitSshDenied("Git SSH pinned host key type is unsupported")
    try:
        if not base64.b64decode(lines[0][2], validate=True):
            raise ValueError("empty host key")
    except (ValueError, binascii.Error) as exc:
        raise GitSshDenied("Git SSH pinned host key is malformed") from exc


def _signal_ssh_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


def _stop_ssh_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    _signal_ssh_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _signal_ssh_group(process, signal.SIGKILL)
        process.wait(timeout=2)


if sys.platform == "win32":

    class _GitSshServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = False
        broker: GitSshBroker

else:

    class _GitSshServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        allow_reuse_address = False
        broker: GitSshBroker


class _GitSshHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        broker = cast(_GitSshServer, self.server).broker
        sock = cast(socket.socket, self.request)
        with broker._condition:
            if broker._closed:
                return
            broker._connections.add(sock)
        sock.settimeout(_HANDSHAKE_TIMEOUT)
        write_lock = threading.Lock()
        acquired = broker._slots.acquire(blocking=False)
        try:
            if not acquired:
                raise GitSshDenied("Git SSH broker is busy")
            first = _read_frame(sock)
            if first is None or first[0] != b"R" or len(first[1]) > _MAX_REQUEST:
                raise GitSshDenied("Git SSH request is invalid")
            request = json.loads(first[1])
            if not isinstance(request, dict):
                raise GitSshDenied("Git SSH request must be an object")
            token = request.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(token, broker.token):
                raise GitSshDenied("Git SSH broker capability is invalid")
            binding, command = _parse_git_argv(request.get("argv"), broker.bindings)
            protocol = request.get("git_protocol")
            if protocol is not None and protocol != "version=2":
                raise GitSshDenied("Git protocol version is invalid")
            _validate_pinned_host_key(binding)
            pinned_ip = _resolve_pinned_ip(binding)
            logger.info(
                "Git SSH granted %s %s:%s %s via %s",
                command,
                binding.host,
                binding.port,
                binding.repository,
                pinned_ip,
            )
            sock.settimeout(_SESSION_TIMEOUT)
            broker.run_ssh(
                sock, write_lock, binding, pinned_ip, command, "version=2" if protocol else None
            )
        except (GitSshDenied, ValueError, json.JSONDecodeError) as exc:
            logger.info("Git SSH denied: %s", exc)
            with contextlib.suppress(OSError):
                _write_frame(sock, b"D", str(exc).encode(), write_lock)
        except (OSError, ConnectionError, subprocess.SubprocessError):
            logger.exception("Git SSH broker connection failed")
            with contextlib.suppress(OSError):
                _write_frame(sock, b"D", b"Git SSH broker connection failed", write_lock)
        finally:
            if acquired:
                broker._slots.release()
            with broker._condition:
                broker._connections.discard(sock)
                broker._condition.notify_all()


class GitSshBroker:
    """Per-sandbox Unix-socket service that holds the SSH authority in the parent."""

    def __init__(self, bindings: Sequence[GitSshBinding], socket_path: Path) -> None:
        if sys.platform == "win32":
            raise GitSshDenied("Git SSH broker requires a Unix sandbox backend")
        self.bindings = tuple(normalize_git_ssh_bindings(bindings))
        self.socket_path = socket_path
        self._server = _GitSshServer(str(socket_path), _GitSshHandler)
        self._server.broker = self
        os.chmod(socket_path, 0o600)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._active: dict[subprocess.Popen[bytes], socket.socket] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._connections: set[socket.socket] = set()
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self.token = secrets.token_urlsafe(32)
        self._closed = False
        self._thread.start()

    def run_ssh(
        self,
        sock: socket.socket,
        write_lock: threading.Lock,
        binding: GitSshBinding,
        pinned_ip: str,
        git_command: str,
        git_protocol: str | None,
    ) -> None:
        with self._lock:
            if self._closed:
                raise GitSshDenied("Git SSH broker is unavailable or busy")
            env = {"PATH": "/usr/bin:/bin"}
            if git_protocol is not None:
                env["GIT_PROTOCOL"] = git_protocol
            process = subprocess.Popen(
                _ssh_command(binding, pinned_ip, git_command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            self._active[process] = sock
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            _write_frame(sock, b"A", b"", write_lock)
        except OSError:
            with self._lock:
                self._active.pop(process, None)
            _stop_ssh_group(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            raise
        quota = [0]
        quota_lock = threading.Lock()

        def pump_input() -> None:
            try:
                while True:
                    frame = _read_frame(sock)
                    if frame is None or frame[0] == b"Z":
                        break
                    if frame[0] != b"I":
                        raise GitSshDenied("Unexpected Git SSH input frame")
                    with quota_lock:
                        quota[0] += len(frame[1])
                        if quota[0] > _MAX_TRANSFER:
                            raise GitSshDenied("Git SSH transfer limit exceeded")
                    process.stdin.write(frame[1])
                    process.stdin.flush()
            except (OSError, ConnectionError, BrokenPipeError, GitSshDenied):
                _signal_ssh_group(process, signal.SIGTERM)
            finally:
                with contextlib.suppress(OSError):
                    process.stdin.close()

        def pump_output(stream: BinaryIO, kind: bytes) -> None:
            try:
                while chunk := os.read(stream.fileno(), _MAX_FRAME):
                    with quota_lock:
                        quota[0] += len(chunk)
                        if quota[0] > _MAX_TRANSFER:
                            _signal_ssh_group(process, signal.SIGTERM)
                            break
                    _write_frame(sock, kind, chunk, write_lock)
            except OSError:
                _signal_ssh_group(process, signal.SIGTERM)

        threads = [
            threading.Thread(target=pump_input, daemon=True),
            threading.Thread(target=pump_output, args=(process.stdout, b"O"), daemon=True),
            threading.Thread(target=pump_output, args=(process.stderr, b"E"), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            try:
                code = process.wait(timeout=_SESSION_TIMEOUT)
            except subprocess.TimeoutExpired:
                _stop_ssh_group(process)
                code = process.returncode or 128
            for thread in threads[1:]:
                thread.join()
            with contextlib.suppress(OSError):
                _write_frame(sock, b"X", struct.pack("!i", code), write_lock)
        finally:
            logger.info("Git SSH transfer closed after %s bytes", quota[0])
            with self._lock:
                self._active.pop(process, None)
            _stop_ssh_group(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()

    def stop(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            active = tuple(self._active)
            connections = tuple(self._connections)
        for sock in connections:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        for process in active:
            _stop_ssh_group(process)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)
        with self._condition:
            if not self._condition.wait_for(lambda: not self._connections, timeout=5):
                raise RuntimeError("Git SSH broker handlers did not stop")


def start_git_ssh_broker(bindings: Sequence[GitSshBinding], tmpdir: Path) -> GitSshBroker:
    """Start one parent-owned Git SSH broker inside the sandbox's scratch view."""
    return GitSshBroker(bindings, tmpdir / _SOCKET_NAME)


def apply_git_ssh_env(env: dict[str, str], broker: GitSshBroker) -> None:
    """Route Git's SSH child through the broker without exposing the host agent."""
    env.pop("SSH_AUTH_SOCK", None)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    helper_path = broker.socket_path.parent / "gitssh-helper.py"
    shutil.copyfile(Path(git_ssh_wire.__file__), helper_path)
    os.chmod(helper_path, 0o700)
    env["GIT_SSH_COMMAND"] = shlex.join([sys.executable, "-I", str(helper_path)])
    env["GIT_SSH_VARIANT"] = "ssh"
    env[_SOCKET_ENV] = str(broker.socket_path)
    env[_TOKEN_ENV] = broker.token
