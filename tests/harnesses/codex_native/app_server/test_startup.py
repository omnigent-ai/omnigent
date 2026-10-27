"""Startup tests for Codex app server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import psutil
import pytest
from websockets.asyncio.server import ServerConnection, serve

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_egress import CertificateFailure
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerSilentStartTimeout,
    CodexNativeAppServer,
)
from omnigent.harnesses.codex_native.bridge import (
    read_certificate_failure,
    record_certificate_failure,
)
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _fake_trust_policy_hooks,
    _FakeStartupClient,
    _set_codex_version,
    _test_app_server,
)


async def test_start_reuses_initialized_readiness_client_for_hook_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup trusts hooks over the readiness connection, then closes it."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    startup_client = _FakeStartupClient()
    trusted_with: list[object] = []

    async def _ready(_self: CodexNativeAppServer) -> _FakeStartupClient:
        return startup_client

    async def _trust(
        _self: CodexNativeAppServer, *, client: CodexAppServerClient | None = None
    ) -> None:
        trusted_with.append(client)

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _ready)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _trust)
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    await server.start()
    try:
        assert trusted_with == [startup_client]
        assert startup_client.close_calls == 1
    finally:
        await server.close()


@dataclass
class _RespawnHarness:
    """A start()-ready server whose spawn, readiness and teardown are mocked."""

    server: CodexNativeAppServer
    spawn_calls: list[str] = field(default_factory=list)
    kill_calls: list[object] = field(default_factory=list)
    socket_present_at_spawn: list[bool] = field(default_factory=list)


_SILENT_PID = 4242
_SILENT_STDERR = ["api_key=synthetic-token-marker"]


def _silent_timeout(server: CodexNativeAppServer) -> CodexAppServerSilentStartTimeout:
    """Leave ``server`` holding a live, never-listening process and build its timeout."""
    proc_mock = AsyncMock()
    proc_mock.returncode = None
    proc_mock.pid = _SILENT_PID
    proc_mock.wait = AsyncMock()
    server.proc = proc_mock
    server.recent_stderr = list(_SILENT_STDERR)
    return CodexAppServerSilentStartTimeout(
        "Timed out after 60s waiting for the Codex app-server at ws://127.0.0.1:43121: "
        f"[Errno 111] Connect call failed; stderr={' | '.join(_SILENT_STDERR)}",
        sum(len(line.encode()) for line in _SILENT_STDERR),
        "ConnectionRefusedError",
    )


def _respawn_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readiness: Callable[[CodexNativeAppServer], Awaitable[_FakeStartupClient]],
) -> _RespawnHarness:
    """
    Build the mocked server shared by the silent-start respawn tests.

    :param tmp_path: Test temp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param readiness: Replacement for ``_wait_until_ready`` scripting each attempt.
    :returns: The server plus the spawn and termination calls it made.
    """
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    _set_codex_version(monkeypatch, (0, 147, 0))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", readiness)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _fake_trust_policy_hooks)
    harness = _RespawnHarness(
        _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    )

    async def _spawn_spy(self: CodexNativeAppServer, resolved_listen: str) -> None:
        harness.spawn_calls.append(resolved_listen)
        harness.socket_present_at_spawn.append(self.socket_path.exists())
        proc_mock = AsyncMock()
        proc_mock.returncode = None
        proc_mock.pid = _SILENT_PID + len(harness.spawn_calls)
        proc_mock.wait = AsyncMock()
        self.proc = proc_mock
        self.recent_stderr = []
        self.stderr_task = None

    monkeypatch.setattr(CodexNativeAppServer, "_spawn_app_server_process", _spawn_spy)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._terminate_process_tree",
        harness.kill_calls.append,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._kill_process_tree", lambda *args: None
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.unregister_codex_native_process",
        lambda *args: None,
    )
    return harness


@pytest.mark.parametrize(
    ("listen_url", "capture_enabled"),
    [("ws://127.0.0.1:43121", True), (None, False)],
    ids=["ws-capture-on", "unix-capture-off"],
)
async def test_start_respawns_on_silent_startup_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    listen_url: str | None,
    capture_enabled: bool,
) -> None:
    """One silent timeout respawns on the same transport and drops the first attempt's state."""
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1" if capture_enabled else "0")
    startup_client = _FakeStartupClient()
    readiness_calls: list[int] = []
    stale_failure = CertificateFailure(evidence="certificate expired", expired=True)

    async def _ready_with_respawn(_self: CodexNativeAppServer) -> _FakeStartupClient:
        readiness_calls.append(len(readiness_calls) + 1)
        if len(readiness_calls) > 1:
            return startup_client
        timeout = _silent_timeout(_self)
        # Leftovers of the silent attempt must not outlive it: a TLS line it
        # printed, and a socket file it may have created.
        _self.certificate_failure = stale_failure
        record_certificate_failure(_self.bridge_dir, stale_failure)
        _self.socket_path.write_text("stale")
        raise timeout

    harness = _respawn_harness(tmp_path, monkeypatch, _ready_with_respawn)
    server = harness.server
    server.listen_url = listen_url
    resolved_listen = listen_url or f"unix://{server.socket_path}"

    with caplog.at_level(logging.WARNING):
        await server.start()
    try:
        # Callers hand the pre-start transport to the TUI and forwarder, so the
        # respawn must reuse it.
        assert harness.spawn_calls == [resolved_listen, resolved_listen]
        assert server.listen_url == listen_url
        assert len(readiness_calls) == 2
        assert len(harness.kill_calls) == 1
        assert server.certificate_failure is None
        assert read_certificate_failure(tmp_path / "bridge") is None
        # Only a unix transport owns the socket path, so only it unlinks the
        # stale file before the second spawn.
        assert harness.socket_present_at_spawn == [False, listen_url is not None]
        # Exactly one respawn event, describing the silent process; its stderr
        # reaches the log only through the opt-in, redacted snapshot.
        records = [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "codex_app_server_silent_start_respawn"
        ]
        assert len(records) == 1
        attributes = records[0].attributes
        assert attributes["exception_type"] == "CodexAppServerSilentStartTimeout"
        assert attributes["app_server_state"] == "running"
        assert attributes["app_server_pid"] == _SILENT_PID
        assert attributes["recent_stderr_bytes"] == len(_SILENT_STDERR[0])
        assert attributes["last_error_type"] == "ConnectionRefusedError"
        assert attributes["stderr_capture_enabled"] is capture_enabled
        if capture_enabled:
            assert attributes["stderr_tail"] == "api_key=[REDACTED]"
        else:
            assert "stderr_tail" not in attributes
        serialized = json.dumps({"message": records[0].getMessage(), "attributes": attributes})
        assert "synthetic-token-marker" not in serialized
    finally:
        await server.close()


async def test_start_fails_on_second_silent_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second silent timeout fails start() with a plain RuntimeError and no third spawn."""

    async def _always_timeout(_self: CodexNativeAppServer) -> _FakeStartupClient:
        raise _silent_timeout(_self)

    harness = _respawn_harness(tmp_path, monkeypatch, _always_timeout)

    with pytest.raises(RuntimeError) as excinfo:
        await harness.server.start()

    assert type(excinfo.value) is RuntimeError
    assert isinstance(excinfo.value.__cause__, CodexAppServerSilentStartTimeout)
    assert len(harness.spawn_calls) == 2
    # Both attempts were torn down: the silent one before the respawn, the
    # replacement by start()'s failure cleanup.
    assert len(harness.kill_calls) == 2


_SILENT_THEN_LISTENING_APP_SERVER = '''\
"""Fake ``codex app-server``: alive but silent on spawn 1, listening on spawn 2."""
import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

from websockets.asyncio.server import serve

state_dir = Path(os.environ["FAKE_APP_SERVER_STATE_DIR"])
attempt = len(list(state_dir.glob("attempt-*"))) + 1
(state_dir / f"attempt-{attempt}").write_text(str(os.getpid()))
host, port = sys.argv[sys.argv.index("--listen") + 1].removeprefix("ws://").split(":")
first_mode = os.environ.get("FAKE_APP_SERVER_FIRST_MODE", "no_listener")
if attempt == 1 and first_mode == "no_listener":
    # Bound but never listening: connects are refused; the sleep outlasts the
    # test's readiness budget without lingering if teardown ever regresses.
    with socket.socket() as held:
        held.bind((host, int(port)))
        time.sleep(30)
    sys.exit(0)


async def handle(connection):
    async for raw in connection:
        message = json.loads(raw)
        if "id" in message:
            if attempt == 1 and first_mode == "no_initialize":
                (state_dir / "initialize-received").touch()
                continue
            await connection.send(json.dumps({"id": message["id"], "result": {}}))


async def main():
    async with serve(handle, host, int(port)):
        await asyncio.Future()


asyncio.run(main())
'''


def _free_port_outside_the_ephemeral_range() -> int:
    """
    Pick a free loopback port that ``bind(("127.0.0.1", 0))`` never hands out.

    Linux auto-allocates from 32768 upward and macOS/Windows from 49152, so a
    low, PID-shifted range keeps parallel workers' ephemeral ports away from
    this one and reduces collisions; availability remains racy until the child
    binds.

    :returns: A bindable port between 20000 and 29999.
    """
    for port in range(20000 + os.getpid() % 9000, 30000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free loopback port between 20000 and 29999")


@pytest.mark.parametrize("first_mode", ["no_listener", "no_initialize"])
async def test_start_respawns_a_silent_app_server_on_the_same_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_mode: str
) -> None:
    """An app-server that never initializes is replaced on the URL callers already hold."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setattr(codex_native_app_server, "_APP_SERVER_READY_TIMEOUT_SECONDS", 3.0)
    _set_codex_version(monkeypatch, (0, 147, 0))
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _fake_trust_policy_hooks)

    state_dir = tmp_path / "app-server-state"
    state_dir.mkdir()
    script = tmp_path / "fake_app_server.py"
    script.write_text(_SILENT_THEN_LISTENING_APP_SERVER)
    codex = tmp_path / "codex"
    codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    codex.chmod(0o755)
    listen_url = f"ws://127.0.0.1:{_free_port_outside_the_ephemeral_range()}"

    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
        env={
            "FAKE_APP_SERVER_STATE_DIR": str(state_dir),
            "FAKE_APP_SERVER_FIRST_MODE": first_mode,
        },
    )
    server.codex_path = str(codex)
    server.listen_url = listen_url

    await asyncio.wait_for(server.start(), timeout=15)
    try:
        assert sorted(path.name for path in state_dir.glob("attempt-*")) == [
            "attempt-1",
            "attempt-2",
        ]
        assert (state_dir / "initialize-received").exists() is (first_mode == "no_initialize")
        assert server.listen_url == listen_url
        silent_pid = int((state_dir / "attempt-1").read_text())
        # close() awaited the child, so it is reaped, not merely terminated.
        assert not psutil.pid_exists(silent_pid)
        assert server.proc is not None and server.proc.returncode is None
        # The runner and ``omnigent codex`` connect with the URL they captured
        # before start(); the replacement must answer there.
        client = CodexAppServerClient(ws_url=listen_url, client_name="omnigent-test")
        await client.connect()
        await client.close()
    finally:
        await server.close()


async def test_start_cancellation_closes_reused_client_and_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Cancellation during hook trust closes both startup resources."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    startup_client = _FakeStartupClient()
    trust_started = asyncio.Event()

    async def _ready(_self: CodexNativeAppServer) -> _FakeStartupClient:
        return startup_client

    async def _trust(
        _self: CodexNativeAppServer, *, client: CodexAppServerClient | None = None
    ) -> None:
        assert client is startup_client
        trust_started.set()
        await asyncio.Future()

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _ready)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _trust)
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    task = asyncio.create_task(server.start())
    await trust_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert startup_client.close_calls == 1
    assert server.proc is None
    assert server.stderr_task is None
    assert not any(
        getattr(record, "event_name", None) == "codex_app_server_start_failed"
        for record in caplog.records
    )


@pytest.fixture
def startup_failure_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CodexNativeAppServer, Path]:
    """Use an owned sleeping child so failure cleanup has live process state."""
    from omnigent.harnesses.codex_native import app_server as module

    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source))

    async def version(_path: str) -> tuple[int, int, int]:
        return (0, 146, 0)

    monkeypatch.setattr(module, "_codex_cli_version", version)
    monkeypatch.setattr(
        module,
        "_build_native_codex_app_server_argv",
        lambda **_kwargs: [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "startup-argv-marker",
        ],
    )
    server = _test_app_server(tmp_path, tmp_path / "private", tmp_path / "bridge", tmp_path)
    server.session_id = "startup-snapshot-session"
    server.reconcile_process_registry = False
    return server, source


@pytest.mark.parametrize("capture_enabled", [False, True])
async def test_start_failure_snapshots_owned_process_before_cleanup(
    startup_failure_server: tuple[CodexNativeAppServer, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capture_enabled: bool,
) -> None:
    """Failure telemetry retains live metadata while honoring the stderr opt-in."""
    server, source = startup_failure_server
    (source / "config.toml").write_text('developer_instructions = "source-file-marker"\n')
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1" if capture_enabled else "0")
    failure = RuntimeError("original-startup-error-marker")
    processes: list[asyncio.subprocess.Process] = []

    async def ready(self: CodexNativeAppServer) -> _FakeStartupClient:
        assert self.proc is not None and self.proc.returncode is None
        processes.append(self.proc)
        self.recent_stderr = ["api_key=synthetic-token-marker"]
        raise failure

    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", ready)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as raised:
        await server.start()

    assert raised.value is failure
    assert server.proc is None and server.stderr_task is None
    assert processes[0].returncode is not None
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_app_server_start_failed"
    ]
    assert len(records) == 1
    record = records[0]
    attributes = record.attributes
    assert record.session_id == server.session_id
    assert attributes["harness"] == "codex-native"
    assert attributes["phase"] == "app_server_startup"
    assert attributes["app_server_state"] == "running"
    assert attributes["app_server_pid"] == processes[0].pid
    assert attributes["stderr_reader_state"] == "running"
    assert attributes["codex_version"] == "0.146.0"
    assert attributes["exception_type"] == "RuntimeError"
    assert attributes["stderr_capture_enabled"] is capture_enabled
    if capture_enabled:
        assert attributes["stderr_tail"] == "api_key=[REDACTED]"
    else:
        assert "stderr_tail" not in attributes
    serialized = json.dumps({"message": record.getMessage(), "attributes": attributes})
    for marker in (
        "source-file-marker",
        "startup-argv-marker",
        "original-startup-error-marker",
        "synthetic-token-marker",
    ):
        assert marker not in serialized


@pytest.mark.parametrize("failure_stage", ["collector", "logger"])
async def test_start_failure_diagnostics_cannot_mask_error_or_cleanup(
    startup_failure_server: tuple[CodexNativeAppServer, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
) -> None:
    """Diagnostic collection and logging failures preserve the original startup error."""
    from omnigent.harnesses.codex_native import app_server as module

    server, _source = startup_failure_server
    failure = RuntimeError("original startup error")
    processes: list[asyncio.subprocess.Process] = []
    diagnostic_calls: list[None] = []

    async def ready(self: CodexNativeAppServer) -> _FakeStartupClient:
        assert self.proc is not None
        processes.append(self.proc)
        raise failure

    def fail(*_args: object, **_kwargs: object) -> None:
        diagnostic_calls.append(None)
        raise ValueError("private diagnostic failure detail")

    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", ready)
    if failure_stage == "collector":
        monkeypatch.setattr(module, "collect_codex_startup_diagnostics", fail)
    else:
        monkeypatch.setattr(module._logger, "warning", fail)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as raised:
        await server.start()

    assert raised.value is failure
    assert len(diagnostic_calls) == 1
    assert server.proc is None and server.stderr_task is None
    assert processes[0].returncode is not None
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_app_server_start_failed"
    ]
    if failure_stage == "collector":
        [record] = records
        assert record.attributes["exception_type"] == "RuntimeError"
        assert record.attributes["diagnostics_error_type"] == "ValueError"
        assert "app_server_pid" not in record.attributes
        serialized = json.dumps({"message": record.getMessage(), "attributes": record.attributes})
        assert "private diagnostic failure detail" not in serialized
    else:
        assert records == []


async def test_wait_until_ready_closes_failed_client_before_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused readiness attempt is closed before returning its retry."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    @dataclass
    class _ProbeClient:
        fail_connect: bool
        connect_calls: int = 0
        close_calls: int = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            if self.fail_connect:
                raise OSError("listener not ready")

        async def close(self) -> None:
            self.close_calls += 1

    first = _ProbeClient(fail_connect=True)
    second = _ProbeClient(fail_connect=False)
    attempts = [first, second]

    def _client(*_args: object, **_kwargs: object) -> _ProbeClient:
        return attempts.pop(0)

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _client)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.proc = Mock(returncode=None)

    connected = await server._wait_until_ready()

    assert connected is second
    assert first.connect_calls == 1
    assert first.close_calls == 1
    assert second.connect_calls == 1
    assert second.close_calls == 0


async def test_wait_until_ready_cancellation_closes_connecting_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during initialize closes the half-open startup client."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    connect_started = asyncio.Event()

    @dataclass
    class _ConnectingClient:
        close_calls: int = 0

        async def connect(self) -> None:
            connect_started.set()
            await asyncio.Future()

        async def close(self) -> None:
            self.close_calls += 1

    client = _ConnectingClient()
    monkeypatch.setattr(
        codex_native_app_server,
        "CodexAppServerClient",
        lambda *_args, **_kwargs: client,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.proc = Mock(returncode=None)

    task = asyncio.create_task(server._wait_until_ready())
    await connect_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.close_calls == 1


@pytest.mark.parametrize("outcome", ["timeout", "cancelled"])
async def test_wait_until_ready_releases_stalled_initialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """A stalled initialize cannot outlive its startup deadline or caller cancellation."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    monkeypatch.setattr(codex_native_app_server, "_APP_SERVER_READY_TIMEOUT_SECONDS", 0.2)
    initialize_received = asyncio.Event()
    connection_closed = asyncio.Event()

    async def handle(connection: ServerConnection) -> None:
        message = json.loads(await connection.recv())
        assert message["method"] == "initialize"
        initialize_received.set()
        await connection.wait_closed()
        connection_closed.set()

    clients: list[CodexAppServerClient] = []

    def client_for_probe(*, ws_url: str, client_name: str) -> CodexAppServerClient:
        client = CodexAppServerClient(ws_url=ws_url, client_name=client_name)
        clients.append(client)
        return client

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", client_for_probe)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    server.proc = Mock(returncode=None)
    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        server.listen_url = f"ws://127.0.0.1:{port}"
        task = asyncio.create_task(server._wait_until_ready())
        try:
            await asyncio.wait_for(initialize_received.wait(), timeout=2)
            if outcome == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(CodexAppServerSilentStartTimeout) as excinfo:
                    await asyncio.wait_for(task, timeout=2)
                assert excinfo.value.last_error_type == "TimeoutError"
            await asyncio.wait_for(connection_closed.wait(), timeout=2)
            assert len(clients) == 1
            assert clients[0]._pending_requests == {}
            assert clients[0]._reader_task is None
            assert clients[0]._ws is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_wait_until_ready_accepts_initialize_inside_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delayed initialize reply still succeeds inside the startup budget."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    monkeypatch.setattr(codex_native_app_server, "_APP_SERVER_READY_TIMEOUT_SECONDS", 2.0)
    initialized = asyncio.Event()

    async def handle(connection: ServerConnection) -> None:
        message = json.loads(await connection.recv())
        assert message["method"] == "initialize"
        await asyncio.sleep(0.05)
        await connection.send(json.dumps({"id": message["id"], "result": {}}))
        notification = json.loads(await connection.recv())
        assert notification["method"] == "initialized"
        initialized.set()
        await connection.wait_closed()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    server.proc = Mock(returncode=None)
    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        server.listen_url = f"ws://127.0.0.1:{port}"
        client = await asyncio.wait_for(server._wait_until_ready(), timeout=3)
        try:
            await asyncio.wait_for(initialized.wait(), timeout=2)
        finally:
            await client.close()


async def test_wait_until_ready_deadline_is_the_app_server_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listener ready after the discovery budget but inside the app-server budget succeeds."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock = {"offset": 0.0}
    monkeypatch.setattr(loop, "time", lambda: real_time() + clock["offset"])
    # Each refused probe costs 5 s of virtual time, so the listener accepts after
    # 20 s: past the 10 s discovery budget, inside the 60 s app-server budget.
    ready_at = loop.time() + 20.0

    @dataclass
    class _SlowListenerClient:
        close_calls: int = 0

        async def connect(self) -> None:
            if loop.time() < ready_at:
                clock["offset"] += 5.0
                raise OSError("[Errno 111] Connect call failed ('127.0.0.1', 57045)")

        async def close(self) -> None:
            self.close_calls += 1

    clients: list[_SlowListenerClient] = []

    def _client(*_args: object, **_kwargs: object) -> _SlowListenerClient:
        clients.append(_SlowListenerClient())
        return clients[-1]

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _client)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = "ws://127.0.0.1:57045"
    server.proc = Mock(returncode=None)

    connected = await server._wait_until_ready()

    assert connected is clients[-1]
    assert connected.close_calls == 0
    assert all(client.close_calls == 1 for client in clients[:-1])
    assert clock["offset"] > codex_native_app_server._CONNECT_TIMEOUT_SECONDS
    assert clock["offset"] < codex_native_app_server._APP_SERVER_READY_TIMEOUT_SECONDS


@pytest.mark.parametrize("listen_url", ["ws://127.0.0.1:57045", None], ids=["ws", "unix"])
async def test_wait_until_ready_timeout_reports_listen_target_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listen_url: str | None
) -> None:
    """A listener that never accepts fails after the app-server budget, naming the target."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock = {"offset": 0.0}
    monkeypatch.setattr(loop, "time", lambda: real_time() + clock["offset"])

    @dataclass
    class _RefusingClient:
        close_calls: int = 0

        async def connect(self) -> None:
            # Each refused probe costs 25 s of virtual time: three attempts
            # exhaust the 60 s budget without a real wait.
            clock["offset"] += 25.0
            raise ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 57045)")

        async def close(self) -> None:
            self.close_calls += 1

    clients: list[_RefusingClient] = []

    def _client(*_args: object, **_kwargs: object) -> _RefusingClient:
        clients.append(_RefusingClient())
        return clients[-1]

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _client)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = listen_url
    server.proc = Mock(returncode=None)
    server.recent_stderr = ["sample", "stderr"]

    with pytest.raises(RuntimeError) as excinfo:
        await server._wait_until_ready()

    message = str(excinfo.value)
    budget = codex_native_app_server._APP_SERVER_READY_TIMEOUT_SECONDS
    target = listen_url or f"unix://{server.socket_path}"
    assert message.startswith(
        f"Timed out after {budget:g}s waiting for the Codex app-server at {target}: "
    )
    assert "Connect call failed" in message
    assert (str(server.socket_path) in message) is (listen_url is None)
    assert len(clients) == 3
    assert all(client.close_calls == 1 for client in clients)
    # Alive but never listening: the respawnable subclass carries the stderr tail
    # size and the class of the last failed probe.
    assert type(excinfo.value) is codex_native_app_server.CodexAppServerSilentStartTimeout
    assert excinfo.value.recent_stderr_bytes == len("samplestderr")
    assert excinfo.value.last_error_type == "ConnectionRefusedError"


async def test_wait_until_ready_fails_at_once_when_the_process_exited(tmp_path: Path) -> None:
    """An exited app-server raises a plain RuntimeError, never the respawnable subclass."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.proc = Mock(returncode=1)
    server.recent_stderr = ["Exit error"]

    with pytest.raises(RuntimeError, match="exited early: Exit error") as excinfo:
        await server._wait_until_ready()

    assert type(excinfo.value) is RuntimeError
    assert not isinstance(excinfo.value, codex_native_app_server.CodexAppServerSilentStartTimeout)


async def test_standalone_hook_trust_closes_client_when_connect_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed standalone trust handshake closes its partially-open client."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    @dataclass
    class _FailingClient:
        close_calls: int = 0

        async def connect(self) -> None:
            raise RuntimeError("initialize failed")

        async def close(self) -> None:
            self.close_calls += 1

    client = _FailingClient()
    monkeypatch.setattr(
        codex_native_app_server,
        "CodexAppServerClient",
        lambda *_args, **_kwargs: client,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    with pytest.raises(RuntimeError, match="initialize failed"):
        await server._trust_policy_hooks()

    assert client.close_calls == 1


async def test_start_can_delegate_global_process_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner-owned Codex startup leaves global cleanup to the host janitor."""
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    reconcile_calls = 0

    def _record_reconcile() -> int:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return 0

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.reconcile_codex_native_process_registry",
        _record_reconcile,
    )
    server = _test_app_server(
        tmp_path,
        codex_home,
        tmp_path / "bridge",
        workspace,
    )
    server.reconcile_process_registry = False

    await server.start()
    await server.close()

    assert reconcile_calls == 0


async def test_start_clears_previous_launch_certificate_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate record left by an earlier launch must not fail this launch's turns."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    _disable_codex_startup_rpc(monkeypatch)
    record_certificate_failure(
        bridge_dir, CertificateFailure(evidence="certificate expired", expired=True)
    )
    server = _test_app_server(tmp_path, tmp_path / "codex-home", bridge_dir, workspace)

    await server.start()
    try:
        assert read_certificate_failure(bridge_dir) is None
    finally:
        await server.close()


async def test_start_reports_a_removed_workspace_before_any_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted worktree fails as WORKSPACE_MISSING instead of a bare spawn ENOENT."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    _disable_codex_startup_rpc(monkeypatch)
    codex_home = tmp_path / "codex-home"
    server = _test_app_server(tmp_path, codex_home, tmp_path / "bridge", tmp_path / "removed")

    with pytest.raises(OmnigentError) as failure:
        await server.start()

    assert failure.value.code == ErrorCode.WORKSPACE_MISSING
    assert server.proc is None
    assert not codex_home.exists()


# Reports a version like the real CLI, records its app-server PID, then never
# opens the --listen socket.
_NEVER_READY_CODEX = """#!/bin/sh
case "$1" in
  --version) echo "codex-cli 0.158.0" ;;
  app-server) echo $$ > "$(dirname "$0")/app-server.pid"; exec sleep 600 ;;
esac
"""


async def test_readiness_failure_records_app_server_state_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A child that never becomes ready is reaped, but its state is logged first."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    codex = tmp_path / "codex"
    codex.write_text(_NEVER_READY_CODEX)
    codex.chmod(0o755)
    monkeypatch.setattr(codex_native_app_server, "_APP_SERVER_READY_TIMEOUT_SECONDS", 1.0)
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    server.codex_path = str(codex)

    with (
        caplog.at_level(logging.INFO),
        pytest.raises(
            RuntimeError, match=r"^Timed out after 1s waiting for the Codex app-server at "
        ),
    ):
        await server.start()

    child_pid = int((tmp_path / "app-server.pid").read_text())
    assert not psutil.pid_exists(child_pid), "readiness failure left the app-server running"
    assert server.proc is None
    assert server.stderr_task is None
    snapshots = [
        record.attributes
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_app_server_start_failed"
    ]
    assert snapshots, "readiness failure logged no app-server state before cleanup"
    [snapshot] = snapshots
    assert snapshot["app_server_pid"] == child_pid
    assert snapshot["app_server_state"] == "running"
    assert snapshot["stderr_reader_state"] == "running"
    assert snapshot["codex_version"] == "0.158.0"
    assert "--listen" not in repr(snapshot)
