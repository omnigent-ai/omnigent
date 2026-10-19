"""Fixed-destination TCP connector, policy, and sandbox integration."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shlex
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import omnigent.inner.os_env as os_env_module
import omnigent.inner.terminal as terminal_module
from omnigent.errors import OmnigentError
from omnigent.inner.bwrap_sandbox import _dotfile_and_symlink_mask_args
from omnigent.inner.datamodel import (
    GitSshBinding,
    OSEnvSandboxSpec,
    OSEnvSpec,
    TcpEgressRoute,
    TerminalEnvSpec,
)
from omnigent.inner.os_env import create_os_environment
from omnigent.inner.sandbox import (
    SandboxPolicy,
    _restore_broker_socket_roots,
    get_backend,
    resolve_sandbox,
)
from omnigent.inner.tcp_egress import (
    TcpEgressDenied,
    apply_tcp_egress_env,
    resolve_pinned_address,
    start_tcp_egress_broker,
)
from omnigent.inner.tcp_egress_policy import parse_tcp_egress
from omnigent.inner.tcp_egress_wire import _read_frame
from omnigent.inner.terminal import (
    build_terminal_os_env_spec,
    create_terminal_instance,
)
from omnigent.spec.parser import _parse_os_env_sandbox
from tests.inner.git_ssh_fixture import _nonloopback_ip
from tests.inner.sandbox.conftest import run_async


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while data := self.request.recv(65536):
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def echo_server() -> Iterator[tuple[str, int]]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 80))
        except OSError:
            pytest.skip("no non-loopback local address")
        host = probe.getsockname()[0]
    with _EchoServer((host, 0), _EchoHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield host, server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.fixture
def tcp_route(echo_server: tuple[str, int]) -> TcpEgressRoute:
    host, port = echo_server
    return TcpEgressRoute(name="echo", host=host, port=port, allowed_cidrs=(f"{host}/32",))


@pytest.fixture
def broker_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="og-tcp-", dir="/tmp") as folder:
        yield Path(folder)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "bad/name"),
        ("name", "Upper"),
        ("host", "*.example.test"),
        ("host", "example.test;id"),
        ("host", "127.0.0.1"),
        ("host", "169.254.169.254"),
        ("host", "100.100.100.200"),
        ("host", "10.1.2.3"),
        ("host", "fd00:ec2::254"),
        ("host", "fd20:ce::254"),
        ("port", 0),
        ("port", True),
        ("port", 443),
        ("allowed_cidrs", ["0.0.0.0/0"]),
        ("allowed_cidrs", ["0.0.0.0/8"]),
        ("allowed_cidrs", ["127.0.0.0/8"]),
        ("allowed_cidrs", ["10.1.0.0/16;id"]),
        ("allowed_cidrs", ["224.0.0.0/4"]),
        ("allowed_cidrs", ["100.100.100.200/32"]),
        ("allowed_cidrs", ["fd00:ec2::/32"]),
    ],
)
def test_tcp_route_rejects_unsafe_fields(field: str, value: object) -> None:
    entry: dict[str, object] = {"name": "database", "host": "db.example.test", "port": 5432}
    entry[field] = value
    with pytest.raises(OmnigentError):
        parse_tcp_egress([entry])


def test_tcp_route_rejects_duplicate_and_http_overlap() -> None:
    entry = {"name": "db", "host": "db.example.test", "port": 5432}
    with pytest.raises(OmnigentError, match="unique"):
        parse_tcp_egress([entry, entry])
    with pytest.raises(OmnigentError, match="cannot coexist"):
        parse_tcp_egress([entry], egress_rules=["GET *.example.test/**"])


def test_tcp_route_parsers_and_runtime_reject_weak_backend(tmp_path: Path) -> None:
    raw = {
        "type": "linux_bwrap",
        "tcp_egress": [{"name": "db", "host": "db.example.test", "port": 5432}],
    }
    from omnigent.inner.loader import _parse_os_env_sandbox_spec

    for parser in (_parse_os_env_sandbox, _parse_os_env_sandbox_spec):
        sandbox = parser(raw)
        assert sandbox.tcp_egress == [TcpEgressRoute("db", "db.example.test", 5432)]
        with pytest.raises(ValueError, match="tcp_egress"):
            resolve_sandbox(
                OSEnvSpec(
                    cwd=str(tmp_path),
                    sandbox=OSEnvSandboxSpec(type="none", tcp_egress=sandbox.tcp_egress),
                ),
                tmp_path,
            )
        with pytest.raises((OmnigentError, ValueError), match="tcp_egress"):
            parser({**raw, "type": "none"})
    with pytest.raises(ValueError, match="at least one route"):
        resolve_sandbox(
            OSEnvSpec(
                cwd=str(tmp_path),
                sandbox=OSEnvSandboxSpec(type="linux_bwrap", tcp_egress=[]),
            ),
            tmp_path,
        )


def test_tcp_route_rejects_mixed_dns_and_blocked_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = TcpEgressRoute("db", "db.example.test", 5432)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 5432)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 5432)),
        ],
    )
    with pytest.raises(TcpEgressDenied, match="blocked"):
        resolve_pinned_address(route)
    private = TcpEgressRoute("db", "db.example.test", 5432, ("10.0.0.0/16",))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.0.2", 5432)),
        ],
    )
    with pytest.raises(TcpEgressDenied, match="outside"):
        resolve_pinned_address(private)
    assert ipaddress.ip_address("100.100.100.200").is_global is False
    cloud_control = TcpEgressRoute("db", "db.example.test", 5432, ("100.100.100.200/32",))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.100.100.200", 5432))
        ],
    )
    with pytest.raises(TcpEgressDenied, match="blocked"):
        resolve_pinned_address(cloud_control)
    ipv6_metadata = TcpEgressRoute("db", "db.example.test", 5432, ("fd00::/8",))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00:ec2::254", 5432, 0, 0))
        ],
    )
    with pytest.raises(TcpEgressDenied, match="blocked"):
        resolve_pinned_address(ipv6_metadata)


def test_tcp_global_resolver_limit_fails_closed(
    tcp_route: TcpEgressRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver_slot = threading.BoundedSemaphore(1)
    assert resolver_slot.acquire(blocking=False)
    monkeypatch.setattr("omnigent.inner.tcp_egress._DNS_SLOTS", resolver_slot)
    try:
        with pytest.raises(TcpEgressDenied, match="resolver is busy"):
            resolve_pinned_address(tcp_route)
    finally:
        resolver_slot.release()


def test_tcp_broker_socket_is_not_masked_after_policy_round_trip(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    socket_path = scratch / ".tcp-egress.sock"
    socket_path.touch()
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=[],
        write_roots=[scratch],
        write_files=[],
        allow_network=True,
        tcp_egress_socket_path=str(socket_path),
        mask_scan_skip_roots=[scratch],
    )
    decoded = SandboxPolicy.from_jsonable(policy.to_jsonable())
    assert str(socket_path) in _dotfile_and_symlink_mask_args(workspace, [], decoded)
    restored = _restore_broker_socket_roots(decoded)
    assert str(socket_path) not in _dotfile_and_symlink_mask_args(workspace, [], restored)


def test_tcp_connector_round_trip_and_revocation(
    tcp_route: TcpEgressRoute, broker_dir: Path
) -> None:
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    try:
        result = subprocess.run(
            ["omnigent-egress-connect", "echo"],
            input=b"test\x00bytes",
            capture_output=True,
            env=env,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == b"test\x00bytes"
        denied = subprocess.run(
            ["omnigent-egress-connect", "other"],
            input=b"test",
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert denied.returncode != 0 and b"not allowed" in denied.stderr
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": broker.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x00"
            broker.stop()
            client.settimeout(2)
            outcome = client.recv(1)
            assert outcome in (b"", b"D")
            if outcome:
                length = struct.unpack("!I", client.recv(4))[0]
                assert b"closed" in client.recv(length)
    finally:
        broker.stop()


def test_tcp_upstream_half_close_preserves_pending_input(
    echo_server: tuple[str, int], broker_dir: Path
) -> None:
    host, _ = echo_server
    observed: list[bytes] = []
    received = threading.Event()

    class HalfCloseHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.sendall(b"ready")
            self.request.shutdown(socket.SHUT_WR)
            observed.append(self.request.recv(5))
            received.set()

    with _EchoServer((host, 0), HalfCloseHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        route = TcpEgressRoute("halfclose", host, server.server_address[1], (f"{host}/32",))
        broker = start_tcp_egress_broker([route], broker_dir)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(str(broker.socket_path))
                request = json.dumps({"token": broker.token, "route": route.name}).encode()
                client.sendall(struct.pack("!I", len(request)) + request)
                assert client.recv(1) == b"\x00"
                assert _read_frame(client) == (b"O", b"ready")
                assert _read_frame(client) == (b"Z", b"")
                client.sendall(b"I" + struct.pack("!I", 5) + b"later")
                client.sendall(b"Z" + struct.pack("!I", 0))
                assert _read_frame(client) == (b"X", struct.pack("!i", 0))
                assert received.wait(timeout=2)
                assert observed == [b"later"]
        finally:
            broker.stop()
            server.shutdown()
            thread.join(timeout=2)


def test_tcp_broker_pins_dns_answer_before_dialing(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = TcpEgressRoute("echo", "db.example.test", tcp_route.port, tcp_route.allowed_cidrs)
    original_getaddrinfo = socket.getaddrinfo
    looked_up: list[str] = []

    def resolve(host: str, port: int, *args: object, **kwargs: object) -> object:
        looked_up.append(host)
        if host == route.host:
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (tcp_route.host, port)),
            ]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    broker = start_tcp_egress_broker([route], broker_dir)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    try:
        result = subprocess.run(
            ["omnigent-egress-connect", "echo"],
            input=b"pinned",
            capture_output=True,
            env=env,
            timeout=15,
        )
        assert result.returncode == 0 and result.stdout == b"pinned", result.stderr
        assert looked_up.count(route.host) == 1
    finally:
        broker.stop()


@pytest.mark.parametrize("ssh_git_server", ["nonloopback"], indirect=True)
def test_tcp_connector_supports_real_ssh_proxy_command(
    ssh_git_server: tuple[GitSshBinding, str, Path],
    broker_dir: Path,
) -> None:
    binding, url, _ = ssh_git_server
    host = _nonloopback_ip()
    assert host is not None
    route = TcpEgressRoute("git_host", host, binding.port, (f"{host}/32",))
    broker = start_tcp_egress_broker([route], broker_dir)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_SSH_VARIANT"] = "ssh"
    env["GIT_SSH_COMMAND"] = shlex.join(
        [
            "/usr/bin/ssh",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={binding.known_hosts_file}",
            "-o",
            "HostKeyAlias=127.0.0.1",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "ProxyCommand=omnigent-egress-connect git_host",
            "-i",
            binding.identity_file,
        ]
    )
    try:
        result = subprocess.run(
            ["git", "ls-remote", url],
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert result.returncode == 0 and "refs/heads/main" in result.stdout, result.stderr
    finally:
        broker.stop()


def test_tcp_helper_reports_quota_denial(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.inner.tcp_egress._MAX_BYTES", 1)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    try:
        result = subprocess.run(
            ["omnigent-egress-connect", "echo"],
            input=b"blocked",
            capture_output=True,
            env=env,
            timeout=15,
        )
        assert result.returncode != 0
        assert b"transfer limit exceeded" in result.stderr
    finally:
        broker.stop()


def test_tcp_helper_reports_output_quota_denial(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.inner.tcp_egress._MAX_BYTES", 3)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    try:
        result = subprocess.run(
            ["omnigent-egress-connect", "echo"],
            input=b"ab",
            capture_output=True,
            env=env,
            timeout=15,
        )
        assert result.returncode != 0
        assert b"transfer limit exceeded" in result.stderr
    finally:
        broker.stop()


def test_tcp_broker_enforces_byte_quota(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("omnigent.inner.tcp_egress._MAX_BYTES", 1)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    caplog.set_level(logging.INFO, logger="omnigent.inner.tcp_egress")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": broker.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x00"
            client.sendall(b"I" + struct.pack("!I", 7) + b"blocked")
            kind, detail = _read_frame(client)
            if kind == b"Z":
                kind, detail = _read_frame(client)
            assert kind == b"D" and b"transfer limit exceeded" in detail
        broker.stop()
        assert "transfer limit exceeded" in caplog.text
        assert "closed after 7 bytes" in caplog.text
    finally:
        broker.stop()


def test_tcp_broker_rejects_empty_input_frames(
    tcp_route: TcpEgressRoute, broker_dir: Path
) -> None:
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": broker.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x00"
            client.sendall(b"I" + struct.pack("!I", 0))
            kind, detail = _read_frame(client)
            if kind == b"Z":
                kind, detail = _read_frame(client)
            assert kind == b"D" and b"input frame is invalid" in detail
    finally:
        broker.stop()


def test_tcp_route_cannot_use_other_brokers_token(
    tcp_route: TcpEgressRoute, broker_dir: Path
) -> None:
    first_dir, second_dir = broker_dir / "first", broker_dir / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = start_tcp_egress_broker([tcp_route], first_dir)
    second = start_tcp_egress_broker([tcp_route], second_dir)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(second.socket_path))
            request = json.dumps({"token": first.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x01"
    finally:
        first.stop()
        second.stop()


def test_tcp_broker_rejects_non_ascii_capability(
    tcp_route: TcpEgressRoute, broker_dir: Path
) -> None:
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": "not-\u00e9-valid", "route": tcp_route.name}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x01"
            assert b"capability is invalid" in client.recv(1024)
    finally:
        broker.stop()


def test_tcp_broker_does_not_accept_client_selected_port(
    tcp_route: TcpEgressRoute, broker_dir: Path
) -> None:
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(broker.socket_path))
            request = json.dumps(
                {"token": broker.token, "route": "echo", "port": tcp_route.port + 1}
            ).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x01"
    finally:
        broker.stop()


def test_tcp_broker_busy_has_a_useful_error(
    tcp_route: TcpEgressRoute, broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.inner.tcp_egress as tcp_module

    monkeypatch.setattr("omnigent.inner.tcp_egress._MAX_CONNECTIONS", 1)
    original_handler = tcp_module._TcpServer.process_request_thread
    handlers_started: list[int] = []

    def track_handler(
        server: tcp_module._TcpServer, request: socket.socket, address: object
    ) -> None:
        handlers_started.append(1)
        original_handler(server, request, address)

    monkeypatch.setattr(tcp_module._TcpServer, "process_request_thread", track_handler)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": broker.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x00"
            busy = subprocess.run(
                ["omnigent-egress-connect", "echo"],
                input=b"",
                capture_output=True,
                env=env,
                timeout=10,
            )
            assert busy.returncode != 0
            assert b"connection limit reached" in busy.stderr
            for _ in range(20):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as flooded:
                    flooded.connect(str(broker.socket_path))
                    assert flooded.recv(1) == b"\x01"
            assert len(handlers_started) == 1
    finally:
        broker.stop()


def test_tcp_revocation_prevents_dial_after_stalled_dns(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    dials: list[object] = []
    import omnigent.inner.tcp_egress as tcp_module

    def resolve(_route: TcpEgressRoute) -> str:
        started.set()
        assert released.wait(timeout=10)
        return tcp_route.host

    monkeypatch.setattr(tcp_module, "resolve_pinned_address", resolve)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    original_dial = broker._dial_pinned

    def dial(pinned_ip: str, port: int) -> socket.socket:
        if not broker._closed:
            dials.append((pinned_ip, port))
        return original_dial(pinned_ip, port)

    monkeypatch.setattr(broker, "_dial_pinned", dial)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": broker.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert started.wait(timeout=2)
            broker.stop()
            released.set()
        with broker._condition:
            assert broker._condition.wait_for(lambda: not broker._connections, timeout=2)
        assert not dials
    finally:
        released.set()
        broker.stop()


def test_tcp_connector_inside_real_sandbox(
    tcp_route: TcpEgressRoute,
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = active_sandbox_spec_factory(write_paths=["."], tcp_egress=[tcp_route])
    environment = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=sandbox)
    )
    assert environment is not None
    try:
        result = run_async(
            environment.shell("printf hello | omnigent-egress-connect echo", timeout=30)
        )
        assert result.get("exit_code") == 0, result
        assert result.get("stdout") == "hello"
        smoke = run_async(environment.shell("python3 -c 'print(123)'"))
        assert smoke.get("exit_code") == 0 and "123" in smoke.get("stdout", ""), smoke
        probe = (
            f"import socket; socket.create_connection(('{tcp_route.host}', {tcp_route.port}), 1)"
        )
        direct = run_async(environment.shell(f'python3 -c "{probe}"'))
        assert direct.get("exit_code") != 0, direct
        assert any(
            detail in direct.get("stderr", "")
            for detail in ("PermissionError", "Operation not permitted", "Network is unreachable")
        ), direct
    finally:
        environment.close()


def test_tcp_route_cannot_bypass_http_egress_via_alias(tmp_path: Path) -> None:
    route = TcpEgressRoute("database", "db.example.test", 5432)
    http_rules = ["GET restricted.example.test/**"]
    sandbox = OSEnvSandboxSpec(type="linux_bwrap", tcp_egress=[route], egress_rules=http_rules)
    with pytest.raises(OmnigentError, match="cannot coexist"):
        _parse_os_env_sandbox(
            {
                "type": "linux_bwrap",
                "egress_rules": http_rules,
                "tcp_egress": [{"name": route.name, "host": route.host, "port": route.port}],
            }
        )
    with pytest.raises(OmnigentError, match="cannot coexist"):
        resolve_sandbox(OSEnvSpec(sandbox=sandbox), tmp_path)


def test_tcp_terminal_cannot_drop_containment() -> None:
    route = TcpEgressRoute("database", "db.example.test", 5432)
    parent = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap", tcp_egress=[route]))
    terminal = TerminalEnvSpec(command="bash", os_env="inherit", allow_sandbox_override=True)
    with pytest.raises(ValueError, match="tcp_egress"):
        build_terminal_os_env_spec(terminal, parent_os_env_spec=parent, sandbox_override="none")


def test_tcp_native_launcher_rejects_missing_broker(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
) -> None:
    route = TcpEgressRoute("database", "db.example.test", 5432)
    policy = resolve_sandbox(
        OSEnvSpec(sandbox=active_sandbox_spec_factory(tcp_egress=[route])), tmp_path
    )
    assert policy.allow_network is False
    with pytest.raises(ValueError, match="requires its parent broker"):
        get_backend(policy.backend_type).wrap_launcher_argv(["/bin/true"], policy, tmp_path)


def test_tcp_and_git_ssh_brokers_work_together(
    tcp_route: TcpEgressRoute,
    ssh_git_server: tuple[GitSshBinding, str, Path],
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
) -> None:
    binding, url, _ = ssh_git_server
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(
                write_paths=["."], tcp_egress=[tcp_route], git_ssh=[binding]
            ),
        )
    )
    assert environment is not None
    try:
        command = (
            f"git clone --quiet --branch main {shlex.quote(url)} cloned "
            "&& printf both | omnigent-egress-connect echo"
        )
        result = run_async(environment.shell(command, timeout=45))
        assert result.get("exit_code") == 0 and result.get("stdout") == "both", result
        assert (workspace / "cloned" / "README.md").read_text() == "Git SSH fixture\n"
        denied_url = url.replace("repo.git", "other.git")
        denied = run_async(environment.shell(f"git ls-remote {shlex.quote(denied_url)}"))
        assert denied.get("exit_code") != 0 and "not allowed" in denied.get("stderr", "")
    finally:
        environment.close()


def test_tcp_helper_start_failure_closes_broker(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = TcpEgressRoute("database", "db.example.test", 5432)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = active_sandbox_spec_factory(tcp_egress=[route])
    environment = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=sandbox)
    )
    assert environment is not None
    brokers = []
    original_start = os_env_module.start_tcp_egress_broker

    def capture_start(routes: list[TcpEgressRoute], folder: Path):
        broker = original_start(routes, folder)
        brokers.append(broker)
        return broker

    monkeypatch.setattr(os_env_module, "start_tcp_egress_broker", capture_start)
    backend = os_env_module.get_backend(sandbox.type)
    monkeypatch.setattr(
        backend,
        "wrap_launcher_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("wrap failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="wrap failed"):
            run_async(environment.shell("true"))
        assert brokers and brokers[0]._closed
        assert not brokers[0].socket_path.exists()
    finally:
        environment.close()


def test_tcp_terminal_start_failure_closes_broker(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = TcpEgressRoute("database", "db.example.test", 5432)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = TerminalEnvSpec(
        command="/bin/bash",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(tcp_egress=[route]),
        ),
    )
    instance = create_terminal_instance(name="tcp", session_key="test", spec=spec).instance
    brokers = []
    original_start = terminal_module.start_tcp_egress_broker

    def capture_start(routes: list[TcpEgressRoute], folder: Path):
        broker = original_start(routes, folder)
        brokers.append(broker)
        return broker

    monkeypatch.setattr(terminal_module, "start_tcp_egress_broker", capture_start)
    monkeypatch.setattr(
        terminal_module,
        "create_exec_launcher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("launcher failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="launcher failed"):
            run_async(instance.launch(cwd=workspace))
        assert brokers and brokers[0]._closed
        assert not brokers[0].socket_path.exists()
    finally:
        run_async(instance.close())


def test_tcp_connector_inside_sandboxed_terminal(
    tcp_route: TcpEgressRoute,
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = (
        "( set -x; printf terminal | omnigent-egress-connect echo > result "
        '&& test "$(cat result)" = terminal '
        "&& python3 -c 'print(123)' "
        f"&& ! python3 -c \"import socket; socket.create_connection(('{tcp_route.host}', "
        f'{tcp_route.port}), 1)" >/dev/null 2>direct-error '
        '&& grep -Eq "PermissionError|Network is unreachable|Operation not permitted" '
        "direct-error "
        ") > output 2>&1 "
        "&& printf success > marker || printf failure > marker"
    )
    spec = TerminalEnvSpec(
        command="/bin/bash",
        args=["-lc", command],
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(write_paths=["."], tcp_egress=[tcp_route]),
        ),
    )
    instance = create_terminal_instance(name="tcp", session_key="test", spec=spec).instance
    try:
        run_async(instance.launch(cwd=workspace))
        deadline = time.monotonic() + 20
        while not (workspace / "marker").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert (workspace / "marker").exists(), run_async(instance.read()).get("screen")
        assert (workspace / "marker").read_text() == "success", (workspace / "output").read_text()
        assert (workspace / "result").read_text() == "terminal"
    finally:
        run_async(instance.close())
