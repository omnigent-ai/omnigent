"""Fixed-destination TCP connector, policy, and sandbox integration."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import json
import logging
import os
import select
import shlex
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.inner.os_env as os_env_module
import omnigent.inner.terminal as terminal_module
from omnigent.errors import OmnigentError
from omnigent.inner import tcp_egress_wire
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
    create_exec_launcher,
    get_backend,
    resolve_sandbox,
)
from omnigent.inner.tcp_egress import (
    TcpEgressBroker,
    TcpEgressDenied,
    apply_tcp_egress_env,
    resolve_pinned_addresses,
    start_tcp_egress_broker,
)
from omnigent.inner.tcp_egress_policy import parse_tcp_egress
from omnigent.inner.tcp_egress_wire import read_frame as _read_frame
from omnigent.inner.terminal import (
    TerminalInstance,
    build_terminal_os_env_spec,
    create_terminal_instance,
)
from omnigent.spec.parser import _parse_os_env_sandbox
from tests._helpers.polling import poll_until
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
    host = _nonloopback_ip()
    if host is None:
        pytest.skip("no non-loopback local address")
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
def static_tcp_route() -> TcpEgressRoute:
    return TcpEgressRoute("echo", "db.example.test", 5432)


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
        pytest.param("host", "0." * 100_000 + "!", id="long-repeated-labels"),
        ("host", "a" * 64 + ".example.test"),
        ("host", ".".join(["a" * 63] * 3 + ["b" * 62])),
        ("host", "-db.example.test"),
        ("host", "db-.example.test"),
        ("host", "db..example.test"),
        ("host", "db.example.test."),
        ("host", "db_1.example.test"),
        ("host", "db.\u00e9xample.test"),
        ("host", "127.0.0.1"),
        ("host", "169.254.169.254"),
        ("host", "100.100.100.200"),
        ("host", "10.1.2.3"),
        ("host", "fd00:ec2::254"),
        ("host", "fd20:ce::254"),
        ("host", "fec0::1"),
        ("port", 0),
        ("port", True),
        ("port", 443),
        ("allowed_cidrs", ["0.0.0.0/0"]),
        ("allowed_cidrs", ["0.0.0.0/8"]),
        ("allowed_cidrs", ["127.0.0.0/8"]),
        ("allowed_cidrs", ["10.1.0.0/16;id"]),
        ("allowed_cidrs", ["224.0.0.0/4"]),
        ("allowed_cidrs", ["100.100.100.200/32"]),
        ("allowed_cidrs", ["100.100.0.0/16"]),
        ("allowed_cidrs", ["fd00::/16"]),
        ("allowed_cidrs", ["fd00:ec2::/32"]),
    ],
)
def test_tcp_route_rejects_unsafe_fields(field: str, value: object) -> None:
    entry: dict[str, object] = {"name": "database", "host": "db.example.test", "port": 5432}
    entry[field] = value
    with pytest.raises(OmnigentError):
        parse_tcp_egress([entry])


def test_tcp_route_rejects_duplicate_names() -> None:
    entry = {"name": "db", "host": "db.example.test", "port": 5432}
    with pytest.raises(OmnigentError, match="unique"):
        parse_tcp_egress([entry, entry])


@pytest.mark.parametrize(
    "host",
    [
        "DB-1.Example.Test",
        "a" * 63,
        ".".join(["a" * 63] * 3 + ["b" * 61]),
        "0." * 120 + "example.test",
    ],
)
def test_tcp_route_accepts_bounded_dns_names(host: str) -> None:
    assert parse_tcp_egress([{"name": "db", "host": host, "port": 5432}]) == [
        TcpEgressRoute("db", host.lower(), 5432)
    ]


def test_tcp_route_parsers_and_runtime_reject_weak_backend(
    tmp_path: Path, broker_dir: Path
) -> None:
    raw = {
        "type": "linux_bwrap",
        "tcp_egress": [{"name": "db", "host": "db.example.test", "port": 5432}],
    }
    from omnigent.inner.loader import _parse_os_env_sandbox_spec

    for parser in (_parse_os_env_sandbox, _parse_os_env_sandbox_spec):
        sandbox = parser(raw)
        assert sandbox.tcp_egress == [TcpEgressRoute("db", "db.example.test", 5432)]
        with pytest.raises((OmnigentError, ValueError), match="tcp_egress"):
            parser({**raw, "type": "none"})
    with pytest.raises(ValueError, match="tcp_egress"):
        resolve_sandbox(
            OSEnvSpec(
                sandbox=OSEnvSandboxSpec(
                    type="none", tcp_egress=[TcpEgressRoute("db", "db.example.test", 5432)]
                )
            ),
            tmp_path,
        )
    with pytest.raises(ValueError, match="at least one route"):
        start_tcp_egress_broker([], broker_dir)
    with pytest.raises(ValueError, match="at least one route"):
        resolve_sandbox(
            OSEnvSpec(
                cwd=str(tmp_path),
                sandbox=OSEnvSandboxSpec(type="linux_bwrap", tcp_egress=[]),
            ),
            tmp_path,
        )


@pytest.mark.parametrize(
    ("addresses", "cidrs", "denial", "grant_private"),
    [
        (["8.8.8.8", "127.0.0.1"], (), "blocked", False),
        (["10.1.0.2"], ("10.0.0.0/16",), "outside", False),
        (["100.100.100.200"], ("100.100.100.200/32",), "blocked", False),
        (["fd00:ec2::254"], ("fd00::/8",), "blocked", False),
        (["10.42.7.9"], (), "outside", True),
        (["fd42::9"], (), "outside", True),
        (["fec0::9"], (), "outside", True),
        (["::ffff:10.42.7.9"], ("10.42.0.0/16",), "blocked", False),
        (["fe80::1%eth0"], ("fe80::/10",), "blocked", False),
    ],
)
def test_tcp_dns_authorizes_every_answer(
    addresses: list[str],
    cidrs: tuple[str, ...],
    denial: str,
    grant_private: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, int]] = []

    def resolve(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        queries.append((host, port))
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    route = TcpEgressRoute("db", "db.example.test", 5432, cidrs)
    with pytest.raises(TcpEgressDenied, match=denial):
        resolve_pinned_addresses(route)
    if grant_private:
        address = ipaddress.ip_address(addresses[0])
        entry = {"name": "db", "host": str(address), "port": 5432}
        with pytest.raises(OmnigentError, match="outside"):
            parse_tcp_egress([entry])
        networks = (f"{address}/{address.max_prefixlen}",)
        assert parse_tcp_egress([{**entry, "allowed_cidrs": list(networks)}]) == [
            TcpEgressRoute("db", str(address), 5432, networks)
        ]
        permitted = TcpEgressRoute("db", route.host, route.port, networks)
        assert resolve_pinned_addresses(permitted) == (str(address),)
    assert queries == [(route.host, route.port)] * (2 if grant_private else 1)


def test_tcp_broker_rejects_mixed_public_and_private_dns_without_dial(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = TcpEgressRoute("db", "db.example.test", 5432)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 5432)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.42.7.9", 5432)),
        ],
    )
    broker = start_tcp_egress_broker([route], broker_dir)
    dials: list[tuple[str, int]] = []

    def dial(address: str, port: int, **_kwargs: object) -> socket.socket:
        dials.append((address, port))
        raise AssertionError("a mixed DNS answer must not reach the upstream dial")

    monkeypatch.setattr(broker, "_dial_pinned", dial)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    try:
        result = subprocess.run(
            ["omnigent-egress-connect", route.name],
            input=b"",
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert result.returncode != 0
        assert b"outside allowed addresses" in result.stderr
        assert not dials
    finally:
        broker.stop()


def test_tcp_connector_wire_rejects_oversized_and_truncated_frames() -> None:
    sender, receiver = socket.socketpair()
    with sender, receiver:
        tcp_egress_wire.write_frame(sender, b"O", b"response")
        assert tcp_egress_wire.read_frame(receiver) == (b"O", b"response")
        with pytest.raises(OSError, match="size limit"):
            tcp_egress_wire.write_frame(sender, b"I", b"x" * (tcp_egress_wire.MAX_FRAME + 1))
        sender.sendall(b"O" + struct.pack("!I", tcp_egress_wire.MAX_FRAME + 1))
        with pytest.raises(OSError, match="size limit"):
            tcp_egress_wire.read_frame(receiver)
        sender.shutdown(socket.SHUT_WR)
        with pytest.raises(OSError, match="disconnected"):
            tcp_egress_wire.read_frame(receiver)
    for partial in (b"O\x00\x00", b"O" + struct.pack("!I", 8) + b"short"):
        sender, receiver = socket.socketpair()
        with sender, receiver:
            sender.sendall(partial)
            sender.shutdown(socket.SHUT_WR)
            with pytest.raises(OSError, match="disconnected"):
                tcp_egress_wire.read_frame(receiver)


def test_tcp_connector_reports_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert tcp_egress_wire.main([]) == 2
    assert "Usage:" in capsys.readouterr().err
    monkeypatch.delenv(tcp_egress_wire.SOCKET_ENV, raising=False)
    monkeypatch.delenv(tcp_egress_wire.TOKEN_ENV, raising=False)
    assert tcp_egress_wire.main(["echo"]) == 1
    assert "unavailable" in capsys.readouterr().err


def test_tcp_global_resolver_limit_fails_closed(
    static_tcp_route: TcpEgressRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver_slot = threading.BoundedSemaphore(1)
    assert resolver_slot.acquire(blocking=False)
    monkeypatch.setattr("omnigent.inner.tcp_egress._DNS_SLOTS", resolver_slot)
    try:
        with pytest.raises(TcpEgressDenied, match="resolver is busy"):
            resolve_pinned_addresses(static_tcp_route)
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


@pytest.fixture
def high_descriptor_socket() -> Iterator[Callable[[socket.socket], socket.socket]]:
    if sys.platform == "win32":
        pytest.skip("high descriptor duplication requires POSIX")
    import fcntl
    import resource

    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    if limits[1] != resource.RLIM_INFINITY and limits[1] <= 2048:
        pytest.skip("descriptor hard limit is below the regression threshold")
    if limits[0] != resource.RLIM_INFINITY and limits[0] <= 2048:
        resource.setrlimit(resource.RLIMIT_NOFILE, (2049, limits[1]))

    def duplicate(original: socket.socket) -> socket.socket:
        return socket.socket(fileno=fcntl.fcntl(original, fcntl.F_DUPFD_CLOEXEC, 2048))

    try:
        yield duplicate
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, limits)


def test_tcp_dial_with_high_descriptor(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    high_descriptor_socket: Callable[[socket.socket], socket.socket],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.inner.tcp_egress as tcp_module

    def create_high(*args: object, **kwargs: object) -> socket.socket:
        with socket.socket(*args, **kwargs) as original:
            return high_descriptor_socket(original)

    socket_module = SimpleNamespace(**vars(socket))
    socket_module.socket = create_high
    monkeypatch.setattr(tcp_module, "socket", socket_module)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    upstream = None
    try:
        upstream = broker._dial_pinned(tcp_route.host, tcp_route.port)
        assert upstream.fileno() >= 2048
        upstream.sendall(b"high fd")
        assert upstream.recv(7) == b"high fd"
        with pytest.raises(TimeoutError, match="dial timed out"):
            broker._dial_pinned(tcp_route.host, tcp_route.port, deadline=time.monotonic() - 1)
    finally:
        if upstream is not None:
            upstream.close()
            with broker._condition:
                broker._connections.discard(upstream)
        broker.stop()


@pytest.mark.parametrize("selector_failure", [False, True])
def test_tcp_relay_with_high_descriptor(
    static_tcp_route: TcpEgressRoute,
    broker_dir: Path,
    high_descriptor_socket: Callable[[socket.socket], socket.socket],
    monkeypatch: pytest.MonkeyPatch,
    selector_failure: bool,
) -> None:
    import omnigent.inner.tcp_egress as tcp_module

    if selector_failure:

        def fail_selector() -> None:
            raise ValueError("selector registration failed")

        monkeypatch.setattr(tcp_module.selectors, "DefaultSelector", fail_selector)
    broker = start_tcp_egress_broker([static_tcp_route], broker_dir)
    errors: list[Exception] = []
    transferred = [0]
    completed = threading.Event()
    client, peer = socket.socketpair()
    upstream, remote = socket.socketpair()
    with client, peer, upstream, remote, high_descriptor_socket(client) as high_client:
        for connection in (high_client, peer, upstream, remote):
            connection.settimeout(3)

        def relay() -> None:
            try:
                broker._relay(high_client, upstream, transferred)
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=relay, daemon=True)
        try:
            worker.start()
            if selector_failure:
                assert completed.wait(timeout=3)
                assert len(errors) == 1 and "selector registration failed" in str(errors[0])
            else:
                assert high_client.fileno() >= 2048
                tcp_egress_wire.write_frame(peer, b"I", b"wire")
                tcp_egress_wire.write_frame(peer, b"Z", b"")
                assert remote.recv(4) == b"wire"
                assert remote.recv(1) == b""
                remote.sendall(b"reply")
                remote.shutdown(socket.SHUT_WR)
                assert _read_frame(peer) == (b"O", b"reply")
                assert _read_frame(peer) == (b"Z", b"")
                assert completed.wait(timeout=3)
                assert not errors
                assert transferred == [9]
        finally:
            broker.stop()
            for connection in (high_client, upstream):
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
            worker.join(timeout=3)


def test_tcp_connector_round_trip_and_revocation(
    tcp_route: TcpEgressRoute, broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = broker_dir / "python with spaces"
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(interpreter))
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
            if outcome == b"Z":
                assert tcp_egress_wire.read_exact(client, 4) == b"\x00" * 4
                outcome = client.recv(1)
            assert outcome in (b"", b"D")
            if outcome:
                length = struct.unpack("!I", tcp_egress_wire.read_exact(client, 4))[0]
                assert b"closed" in tcp_egress_wire.read_exact(client, length)
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


@pytest.mark.parametrize("limit", ["_SESSION_SECONDS", "_IDLE_SECONDS"])
@pytest.mark.parametrize("half_close", [False, True])
def test_tcp_connector_timeouts_release_stream(
    echo_server: tuple[str, int],
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    half_close: bool,
) -> None:
    import omnigent.inner.tcp_egress as tcp_module

    host, _ = echo_server
    clock = [time.monotonic()]
    monkeypatch.setattr(tcp_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(tcp_module, limit, 1)

    class ReadyHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.sendall(b"ready")
            if half_close:
                self.request.shutdown(socket.SHUT_WR)
            while data := self.request.recv(65536):
                if not half_close:
                    self.request.sendall(data)

    with _EchoServer((host, 0), ReadyHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        route = TcpEgressRoute("ready", host, server.server_address[1], (f"{host}/32",))
        broker = start_tcp_egress_broker([route], broker_dir)
        env = os.environ.copy()
        apply_tcp_egress_env(env, broker)
        try:
            with subprocess.Popen(
                ["omnigent-egress-connect", "ready"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            ) as process:
                try:
                    assert process.stdin is not None and process.stdout is not None
                    assert process.stderr is not None
                    assert select.select([process.stdout], [], [], 5)[0]
                    assert os.read(process.stdout.fileno(), 5) == b"ready"
                    if half_close:
                        assert select.select([process.stdout], [], [], 3)[0], "missing EOF"
                        assert os.read(process.stdout.fileno(), 1) == b""
                    clock[0] += 2
                    if not half_close:
                        # Wake the upstream read without refreshing the idle clock.
                        server_request = next(
                            sock for sock in broker._connections if sock.family == socket.AF_INET
                        )
                        server_request.shutdown(socket.SHUT_RD)
                    assert process.wait(timeout=5) != 0
                    assert b"timed out" in process.stderr.read()
                    with broker._condition:
                        assert broker._condition.wait_for(
                            lambda: not broker._connections, timeout=3
                        )
                    assert broker._slots.acquire(blocking=False)
                    broker._slots.release()
                finally:
                    if process.poll() is None:
                        process.kill()
        finally:
            broker.stop()
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("revoke", [False, True])
def test_tcp_backpressure_releases_stream(
    static_tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    direction: str,
    revoke: bool,
) -> None:
    import omnigent.inner.tcp_egress as tcp_module

    monkeypatch.setattr(tcp_module, "_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(tcp_module, "resolve_pinned_addresses", lambda _: ("1.1.1.1",))
    caplog.set_level(logging.INFO, logger=tcp_module.__name__)
    broker = start_tcp_egress_broker([static_tcp_route], broker_dir)
    upstream, remote = socket.socketpair()
    observed: list[list[int]] = []
    blocked_peers: list[socket.socket] = []
    payload = b"x" * 65536
    remote.settimeout(1)

    def send_output() -> None:
        with contextlib.suppress(OSError):
            remote.sendall(payload)

    writer = threading.Thread(target=send_output, daemon=True)

    def dial(_address: str, _port: int, **_kwargs: object) -> socket.socket:
        with broker._condition:
            broker._connections.add(upstream)
        return upstream

    original_relay = broker._relay

    def relay(client: socket.socket, upstream: socket.socket, counts: list[int]) -> None:
        for connection in (client, upstream):
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            connection.settimeout(5 if revoke else 0.5)
        observed.append(counts)
        blocked_peers.append(upstream if direction == "input" else client)
        original_relay(client, upstream, counts)

    monkeypatch.setattr(broker, "_dial_pinned", dial)
    monkeypatch.setattr(broker, "_relay", relay)
    stop_finished = threading.Event()

    def stop() -> None:
        try:
            broker.stop()
        finally:
            stop_finished.set()

    stopper = threading.Thread(target=stop, daemon=True)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": broker.token, "route": static_tcp_route.name}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x00"
            if direction == "input":
                tcp_egress_wire.write_frame(client, b"I", payload)
            else:
                writer.start()
            poll_until(lambda: observed and observed[0][0] > 0, timeout_s=3, interval_s=0.01)
            if revoke:
                poll_until(
                    lambda: not select.select([], blocked_peers, [], 0)[1],
                    timeout_s=0.4,
                    interval_s=0.01,
                )
                stopper.start()
                assert stop_finished.wait(timeout=2), (
                    "revocation did not interrupt a blocked write"
                )
                assert not broker.socket_path.exists()
            with broker._condition:
                assert broker._condition.wait_for(lambda: not broker._connections, timeout=3)
            assert broker._slots.acquire(blocking=False)
            broker._slots.release()
            if not revoke:
                assert "timed out" in caplog.text
    finally:
        broker.stop()
        upstream.close()
        remote.close()
        if writer.ident is not None:
            writer.join(timeout=2)
        if stopper.ident is not None:
            stopper.join(timeout=2)


@pytest.mark.parametrize("fallback", [False, True])
def test_tcp_broker_pins_dns_answer_before_dialing(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    fallback: bool,
) -> None:
    unreachable = "192.0.2.1"
    route = TcpEgressRoute(
        "echo", "db.example.test", tcp_route.port, (*tcp_route.allowed_cidrs, f"{unreachable}/32")
    )
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect_ex
    looked_up: list[str] = []
    dialed: list[str] = []
    deadlines: list[float | None] = []

    def connect(sock: socket.socket, address: tuple[str, int]) -> int:
        dialed.append(address[0])
        if address[0] == unreachable:
            return errno.ENETUNREACH
        return original_connect(sock, address)

    def resolve(host: str, port: int, *args: object, **kwargs: object) -> object:
        looked_up.append(host)
        if host == route.host:
            addresses = [unreachable, tcp_route.host] if fallback else [tcp_route.host]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                for address in addresses
            ]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)
    broker = start_tcp_egress_broker([route], broker_dir)
    original_dial = broker._dial_pinned

    def dial(address: str, port: int, *, deadline: float | None = None) -> socket.socket:
        deadlines.append(deadline)
        return original_dial(address, port, deadline=deadline)

    monkeypatch.setattr(broker, "_dial_pinned", dial)
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
        assert dialed == ([unreachable, tcp_route.host] if fallback else [tcp_route.host])
        assert deadlines[0] is not None and len(set(deadlines)) == 1
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *_args, **_kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", route.port)),
            ],
        )
        dials: list[object] = []
        monkeypatch.setattr(broker, "_dial_pinned", lambda *args, **kwargs: dials.append(args))
        denied = subprocess.run(
            ["omnigent-egress-connect", "echo"],
            input=b"rebound",
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert denied.returncode != 0 and b"blocked address" in denied.stderr
        assert not dials
    finally:
        broker.stop()


@pytest.mark.parametrize("ssh_git_server", ["nonloopback"], indirect=True)
def test_tcp_connector_supports_real_ssh_proxy_command(
    ssh_git_server: tuple[GitSshBinding, str, Path],
    broker_dir: Path,
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
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
        workspace = tmp_path / "ssh-terminal"
        workspace.mkdir()
        environment = OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(
                write_paths=["."],
                tcp_egress=[route],
                extra_read_paths=[str(Path(binding.identity_file).parent)],
            ),
        )
        command = (
            f"git ls-remote {shlex.quote(url)} > result 2> error; "
            "printf '%s' $? > status; exec sleep 30"
        )
        terminal = create_terminal_instance(
            name="tcp-ssh",
            session_key="test",
            spec=TerminalEnvSpec(
                command="/bin/bash",
                args=["-lc", command],
                os_env="inherit",
                env={key: value for key, value in env.items() if key.startswith("GIT_")},
            ),
            parent_os_env_spec=environment,
        ).instance
        try:
            run_async(terminal.launch(cwd=workspace))
            status = workspace / "status"
            poll_until(lambda: status.exists() and bool(status.read_text()), timeout_s=20)
            assert status.read_text() == "0", (workspace / "error").read_text()
            assert "refs/heads/main" in (workspace / "result").read_text()
        finally:
            run_async(terminal.close())
    finally:
        broker.stop()


@pytest.mark.parametrize(
    ("payload", "quota", "accounted"),
    [(b"blocked", 1, 7), (b"ab", 3, 4)],
    ids=["input", "output"],
)
def test_tcp_quota_denial_reports_frame_and_accounting(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    payload: bytes,
    quota: int,
    accounted: int,
) -> None:
    monkeypatch.setattr("omnigent.inner.tcp_egress._MAX_BYTES", quota)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    env = os.environ.copy()
    apply_tcp_egress_env(env, broker)
    caplog.set_level(logging.INFO, logger="omnigent.inner.tcp_egress")
    try:
        result = subprocess.run(
            ["omnigent-egress-connect", "echo"],
            input=payload,
            capture_output=True,
            env=env,
            timeout=15,
        )
        assert result.returncode != 0
        assert b"transfer limit exceeded" in result.stderr
        assert not result.stdout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(broker.socket_path))
            request = json.dumps({"token": broker.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x00"
            tcp_egress_wire.write_frame(client, b"I", payload)
            kind, detail = _read_frame(client)
            if kind == b"Z":
                kind, detail = _read_frame(client)
            assert kind == b"D" and b"transfer limit exceeded" in detail
        broker.stop()
        assert "transfer limit exceeded" in caplog.text
        assert f"closed after {accounted} bytes" in caplog.text
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
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
) -> None:
    second_dir = broker_dir / "second"
    second_dir.mkdir()
    workspace = tmp_path / "isolated"
    workspace.mkdir()
    environment = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(write_paths=["."], tcp_egress=[tcp_route]),
        )
    )
    assert isinstance(environment, os_env_module.CallerProcessOSEnvironment)
    second = start_tcp_egress_broker([tcp_route], second_dir)
    try:
        own = run_async(environment.shell("printf own | omnigent-egress-connect echo"))
        assert own.get("exit_code") == 0 and own.get("stdout") == "own", own
        first = environment._helper._tcp_egress_broker
        assert first is not None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(second.socket_path))
            request = json.dumps({"token": first.token, "route": "echo"}).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x01"
        borrowed = run_async(
            environment.shell(
                f"printf borrowed | {tcp_egress_wire.SOCKET_ENV}="
                f"{shlex.quote(str(second.socket_path))} "
                f"{tcp_egress_wire.TOKEN_ENV}={shlex.quote(second.token)} "
                "omnigent-egress-connect echo"
            )
        )
        assert borrowed.get("exit_code") != 0, borrowed
        assert borrowed.get("stdout") == "", borrowed
    finally:
        environment.close()
        second.stop()


def test_tcp_broker_rejects_non_ascii_capability(
    static_tcp_route: TcpEgressRoute, broker_dir: Path
) -> None:
    broker = start_tcp_egress_broker([static_tcp_route], broker_dir)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(broker.socket_path))
            request = json.dumps(
                {"token": "not-\u00e9-valid", "route": static_tcp_route.name}
            ).encode()
            client.sendall(struct.pack("!I", len(request)) + request)
            assert client.recv(1) == b"\x01"
            assert b"capability is invalid" in client.recv(1024)
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


@pytest.mark.parametrize(
    "request_bytes",
    [
        struct.pack("!I", 0),
        struct.pack("!I", 2049),
        struct.pack("!I", 1) + b"{",
        struct.pack("!I", 1) + b"\xff",
        pytest.param(None, id="client-selected-port"),
    ],
)
def test_tcp_broker_rejects_malformed_initial_requests(
    request_bytes: bytes | None, broker_dir: Path
) -> None:
    broker = start_tcp_egress_broker(
        [TcpEgressRoute("echo", "echo.example.test", 5432)], broker_dir
    )
    try:
        if request_bytes is None:
            request = json.dumps({"token": broker.token, "route": "echo", "port": 5433}).encode()
            request_bytes = struct.pack("!I", len(request)) + request
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(broker.socket_path))
            client.sendall(request_bytes)
            assert client.recv(1) == b"\x01"
    finally:
        broker.stop()


@pytest.mark.parametrize("partial_header", [False, True])
def test_tcp_handshake_trickle_has_an_absolute_deadline(
    partial_header: bool,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.inner.tcp_egress._HANDSHAKE_SECONDS", 0.15)
    broker = start_tcp_egress_broker(
        [TcpEgressRoute("echo", "echo.example.test", 5432)], broker_dir
    )
    finished = threading.Event()
    header = struct.pack("!I", 2048)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(broker.socket_path))
        client.sendall(header[:1] if partial_header else header)

        def trickle() -> None:
            try:
                for value in (header[1:] if partial_header else b"") + b" " * 2048:
                    if finished.wait(0.04):
                        return
                    client.sendall(bytes([value]))
            except OSError:
                pass

        writer = threading.Thread(target=trickle, daemon=True)
        writer.start()
        try:
            assert client.recv(1) == b"\x01"
            assert b"connection failed" in client.recv(1024)
        finally:
            finished.set()
            writer.join(timeout=2)
            broker.stop()


def test_tcp_revocation_during_slow_quota_denial(
    tcp_route: TcpEgressRoute, broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.inner.tcp_egress._MAX_CONNECTIONS", 1)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    stop_finished = threading.Event()
    trickling = threading.Event()
    finish_trickle = threading.Event()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as active:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            try:
                active.settimeout(3)
                active.connect(str(broker.socket_path))
                request = json.dumps({"token": broker.token, "route": "echo"}).encode()
                active.sendall(struct.pack("!I", len(request)) + request)
                assert active.recv(1) == b"\x00"
                slow.connect(str(broker.socket_path))
                slow.sendall(struct.pack("!I", 2048))

                def trickle() -> None:
                    try:
                        while not finish_trickle.wait(0.05):
                            slow.sendall(b"x")
                            trickling.set()
                    except OSError:
                        pass

                def stop() -> None:
                    broker.stop()
                    stop_finished.set()

                writer = threading.Thread(target=trickle, daemon=True)
                writer.start()
                assert trickling.wait(timeout=2)
                stopper = threading.Thread(target=stop, daemon=True)
                stopper.start()
                assert stop_finished.wait(timeout=2), "quota denial blocked revocation"
            finally:
                finish_trickle.set()
                slow.close()
                broker.stop()
                if "writer" in locals():
                    writer.join(timeout=2)
                if "stopper" in locals():
                    stopper.join(timeout=2)


@pytest.mark.parametrize("failure", ["chmod", "thread"])
def test_tcp_broker_initialization_failure_removes_socket(
    static_tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    def fail(*_args: object) -> None:
        raise OSError("injected initialization failure")

    if failure == "chmod":
        monkeypatch.setattr(os, "chmod", fail)
    else:
        monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(OSError, match="injected initialization failure"):
        start_tcp_egress_broker([static_tcp_route], broker_dir)
    assert not (broker_dir / ".tcp-egress.sock").exists()


def test_tcp_revocation_prevents_dial_after_stalled_dns(
    tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    dials: list[object] = []
    import omnigent.inner.tcp_egress as tcp_module

    def resolve(_route: TcpEgressRoute) -> tuple[str, ...]:
        started.set()
        assert released.wait(timeout=10)
        return (tcp_route.host,)

    monkeypatch.setattr(tcp_module, "resolve_pinned_addresses", resolve)
    broker = start_tcp_egress_broker([tcp_route], broker_dir)
    original_dial = broker._dial_pinned

    def dial(pinned_ip: str, port: int, *, deadline: float | None = None) -> socket.socket:
        if not broker._closed:
            dials.append((pinned_ip, port))
        return original_dial(pinned_ip, port, deadline=deadline)

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


def _assert_owner_revokes_stream(broker: TcpEgressBroker, close: Callable[[], None]) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(broker.socket_path))
        request = json.dumps({"token": broker.token, "route": "echo"}).encode()
        client.sendall(struct.pack("!I", len(request)) + request)
        assert client.recv(1) == b"\x00"
        tcp_egress_wire.write_frame(client, b"I", b"x")
        assert _read_frame(client) == (b"O", b"x")
        close()
        with pytest.raises(OSError, match="disconnected"):
            while True:
                _read_frame(client)
        assert not broker.socket_path.exists()


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
    assert isinstance(environment, os_env_module.CallerProcessOSEnvironment)
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
        broker = environment._helper._tcp_egress_broker
        assert broker is not None
        _assert_owner_revokes_stream(broker, environment.close)
    finally:
        environment.close()


def test_tcp_route_cannot_bypass_http_egress_via_alias(tmp_path: Path) -> None:
    route = TcpEgressRoute("database", "db.example.test", 5432)
    http_rules = ["GET restricted.example.test/**"]
    with pytest.raises(OmnigentError, match="cannot coexist"):
        parse_tcp_egress(
            [{"name": route.name, "host": route.host, "port": route.port}],
            egress_rules=http_rules,
        )
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
        create_exec_launcher("/bin/true", policy)
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


@pytest.mark.parametrize("restart", [False, True])
def test_tcp_helper_start_failure_closes_broker(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
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
    try:
        if restart:
            assert run_async(environment.shell("true"))["exit_code"] == 0
            proc = environment._helper._proc
            assert proc is not None
            proc.kill()
            proc.wait(timeout=5)
            assert run_async(environment.shell("true"))["exit_code"] == 0
            assert len(brokers) == 2
            assert brokers[0]._closed and not brokers[0].socket_path.exists()
        else:
            backend = os_env_module.get_backend(sandbox.type)
            monkeypatch.setattr(
                backend,
                "wrap_launcher_argv",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("wrap failed")),
            )
            with pytest.raises(RuntimeError, match="wrap failed"):
                run_async(environment.shell("true"))
            assert len(brokers) == 1
    finally:
        environment.close()
    for broker in brokers:
        assert broker._closed
        assert not broker._thread.is_alive()
        assert not broker.socket_path.parent.exists()


@pytest.mark.parametrize("stage", ["bootstrap", "launcher", "tmux", "close", "os_env_close"])
def test_tcp_terminal_cleanup_does_not_block_event_loop(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
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
    stop_entered = threading.Event()
    release_stop = threading.Event()
    cleanup_timed_out = threading.Event()
    original_start = terminal_module.start_tcp_egress_broker

    def capture_start(routes: list[TcpEgressRoute], folder: Path):
        broker = original_start(routes, folder)
        brokers.append(broker)
        original_stop = broker.stop

        def blocked_stop() -> None:
            stop_entered.set()
            if not release_stop.wait(timeout=2):
                cleanup_timed_out.set()
            original_stop()

        monkeypatch.setattr(broker, "stop", blocked_stop)
        return broker

    monkeypatch.setattr(terminal_module, "start_tcp_egress_broker", capture_start)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected launch failure")

    if stage == "bootstrap":
        monkeypatch.setattr(terminal_module, "apply_tcp_egress_env", fail)
    elif stage == "launcher":
        monkeypatch.setattr(terminal_module, "create_exec_launcher", fail)
    elif stage == "tmux":
        monkeypatch.setattr(terminal_module, "create_exec_launcher", lambda *_args: "/bin/true")

        async def fail_spawn(*_args: object, **_kwargs: object) -> None:
            fail()

        monkeypatch.setattr(
            terminal_module,
            "asyncio",
            SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": fail_spawn}),
        )

    async def exercise() -> None:
        if stage in ("close", "os_env_close"):
            assert instance.sandbox_policy is not None
            instance._bootstrap_tcp_egress(instance.sandbox_policy, {})
            if stage == "os_env_close":
                broker = instance._tcp_egress_broker
                assert broker is not None
                instance._tcp_egress_broker = None
                monkeypatch.setattr(instance, "os_env", SimpleNamespace(close=broker.stop))
            task = asyncio.create_task(instance.close())
        else:
            task = asyncio.create_task(instance.launch(cwd=workspace))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(stop_entered.wait, 3), timeout=4)
            assert not cleanup_timed_out.is_set(), "cleanup stalled the event loop"
        finally:
            release_stop.set()
            if stage in ("close", "os_env_close"):
                await task
            else:
                with pytest.raises(RuntimeError, match="injected launch failure"):
                    await task

    try:
        run_async(exercise())
        assert brokers and brokers[0]._closed
        assert not brokers[0].socket_path.exists()
    finally:
        release_stop.set()
        run_async(instance.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_owner", ["broker", "environment", "tmux"])
async def test_tcp_terminal_cancelled_cleanup_still_revokes_broker(
    static_tcp_route: TcpEgressRoute,
    broker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_owner: str,
) -> None:
    instance = TerminalInstance(
        name="tcp", session_key="test", private_dir=broker_dir, socket_path=broker_dir / "tmux"
    )
    broker = start_tcp_egress_broker([static_tcp_route], broker_dir)
    instance._tcp_egress_broker = broker
    entered = threading.Event()
    release = threading.Event()
    expired = threading.Event()
    original_stop = broker.stop

    def block() -> None:
        entered.set()
        if not release.wait(timeout=2):
            expired.set()
        if blocked_owner == "broker":
            original_stop()

    if blocked_owner == "broker":
        monkeypatch.setattr(broker, "stop", block)
    elif blocked_owner == "environment":
        monkeypatch.setattr(instance, "os_env", SimpleNamespace(close=block))
    else:
        instance.running = True

        async def parked_tmux(*_args: object) -> None:
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(instance, "_tmux", parked_tmux)
    close = asyncio.create_task(instance.close())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
        assert not expired.is_set()
        if blocked_owner == "environment":
            assert broker._closed
        close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
    finally:
        release.set()
        await asyncio.gather(close, return_exceptions=True)
        assert instance._resource_cleanup_task is not None
        await asyncio.wait_for(instance._resource_cleanup_task, timeout=3)
    assert broker._closed and not broker.socket_path.exists()
    assert not broker_dir.exists()


@pytest.mark.parametrize("inherited", [False, True])
def test_tcp_connector_inside_sandboxed_terminal(
    tcp_route: TcpEgressRoute,
    inherited: bool,
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
        "&& printf success > marker || printf failure > marker; exec sleep 30"
    )
    environment = OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=active_sandbox_spec_factory(write_paths=["."], tcp_egress=[tcp_route]),
    )
    spec = TerminalEnvSpec(
        command="/bin/bash",
        args=["-lc", command],
        os_env="inherit" if inherited else environment,
    )
    instance = create_terminal_instance(
        name="tcp",
        session_key="test",
        spec=spec,
        parent_os_env_spec=environment if inherited else None,
    ).instance
    try:
        run_async(instance.launch(cwd=workspace))
        marker = workspace / "marker"
        poll_until(
            lambda: marker.exists() and marker.read_text() in {"success", "failure"},
            timeout_s=20,
            interval_s=0.1,
        )
        assert marker.read_text() == "success", (workspace / "output").read_text()
        assert (workspace / "result").read_text() == "terminal"
        broker = instance._tcp_egress_broker
        assert broker is not None
        _assert_owner_revokes_stream(broker, lambda: run_async(instance.close()))
    finally:
        run_async(instance.close())
