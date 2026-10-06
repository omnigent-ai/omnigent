"""Startup tests for Codex app server."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
)
from omnigent.runner.native.start_failure import classify_start_failure
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _FakeStartupClient,
    _test_app_server,
)

_INSTALL_ERROR_LINE = (
    "Error: Missing optional dependency @openai/codex-linux-x64. "
    "Reinstall Codex: npm install -g @openai/codex@latest"
)
# Node's uncaught-exception output for a broken npm install: the ``Error:`` line
# sits above a stack long enough that the last five lines no longer show it.
_NODE_INSTALL_STDERR = [
    "/usr/lib/node_modules/@openai/codex/bin/codex.js:79",
    "    throw new Error(",
    "          ^",
    "",
    _INSTALL_ERROR_LINE,
    "    at Object.<anonymous> (/usr/lib/node_modules/@openai/codex/bin/codex.js:79:11)",
    "    at Module._compile (node:internal/modules/cjs/loader:1554:14)",
    "    at Object..js (node:internal/modules/cjs/loader:1706:10)",
    "    at Module.load (node:internal/modules/cjs/loader:1289:32)",
    "    at Function._load (node:internal/modules/cjs/loader:1108:12)",
    "    at TracingChannel.traceSync (node:diagnostics_channel:322:14)",
    "    at wrapModuleLoad (node:internal/modules/cjs/loader:220:24)",
    "    at node:internal/main/run_main_module:36:49",
    "",
    "Node.js v22.14.0",
]


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


async def test_start_cancellation_closes_reused_client_and_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            raise OSError("[Errno 111] Connect call failed ('127.0.0.1', 57045)")

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
    cause = classify_start_failure(excinfo.value)
    assert cause is not None
    assert cause.reason == "app_server_start_timeout"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        pytest.param(None, "", id="no-buffer"),
        pytest.param([], "", id="empty"),
        pytest.param(["a", "b"], "a | b", id="short"),
        pytest.param(
            [f"line {i}" for i in range(8)],
            "line 3 | line 4 | line 5 | line 6 | line 7",
            id="only-the-last-five",
        ),
        pytest.param(
            ["old", "Error: cause", "x", "y", "z", "w"],
            "Error: cause | x | y | z | w",
            id="error-line-in-the-tail-is-not-repeated",
        ),
        pytest.param(
            ["Error: cause", "1", "2", "3", "4", "5"],
            "Error: cause | 1 | 2 | 3 | 4 | 5",
            id="error-line-before-the-tail-is-kept",
        ),
        pytest.param(
            ["ok", "Error: first", "Error: second", "1", "2", "3", "4", "5"],
            "Error: first | 1 | 2 | 3 | 4 | 5",
            id="only-the-first-error-line",
        ),
        pytest.param(
            ["  Error: indented", "1", "2", "3", "4", "5"],
            "  Error: indented | 1 | 2 | 3 | 4 | 5",
            id="indented-error-line",
        ),
        pytest.param(
            ["TypeError: not it", "no Error: prefix", "1", "2", "3", "4", "5"],
            "1 | 2 | 3 | 4 | 5",
            id="other-lines-are-not-error-lines",
        ),
    ],
)
def test_ready_failure_stderr_detail_selection(stderr: list[str] | None, expected: str) -> None:
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    assert codex_native_app_server._ready_failure_stderr_detail(stderr) == expected


def test_ready_failure_stderr_detail_caps_the_kept_error_line() -> None:
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    cap = codex_native_app_server._READY_STDERR_ERROR_LINE_CHARS
    detail = codex_native_app_server._ready_failure_stderr_detail(
        ["Error: " + "x" * 10_000, "1", "2", "3", "4", "5"]
    )

    kept, _, tail = detail.partition(" | 1 | ")
    assert kept == ("Error: " + "x" * 10_000)[:cap] + "...[truncated]"
    assert tail == "2 | 3 | 4 | 5"


async def test_wait_until_ready_early_exit_keeps_the_error_line_a_stack_trace_pushed_out(
    tmp_path: Path,
) -> None:
    """A broken Codex install still names its missing package when the child exits."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    server.proc = Mock(returncode=1)
    server.recent_stderr = list(_NODE_INSTALL_STDERR)

    with pytest.raises(RuntimeError) as excinfo:
        await server._wait_until_ready()

    assert str(excinfo.value) == (
        f"Codex app-server exited early: {_INSTALL_ERROR_LINE} | "
        + " | ".join(_NODE_INSTALL_STDERR[-5:])
    )
    cause = classify_start_failure(excinfo.value)
    assert cause is not None
    assert cause.reason == "codex_install_incomplete"


async def test_captured_node_stderr_reaches_the_readiness_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stderr read from the child's pipe keeps the lines the readiness error quotes."""
    monkeypatch.delenv("OMNIGENT_HARNESS_STDERR_ENABLED", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    stderr = asyncio.StreamReader()
    stderr.feed_data(("\n".join(_NODE_INSTALL_STDERR) + "\n").encode())
    stderr.feed_eof()
    server.proc = Mock(returncode=1, stderr=stderr)
    server.recent_stderr = []
    await server._stderr_loop()

    with pytest.raises(RuntimeError) as excinfo:
        await server._wait_until_ready()

    assert str(excinfo.value).startswith(
        f"Codex app-server exited early: {_INSTALL_ERROR_LINE} | "
    )
    cause = classify_start_failure(excinfo.value)
    assert cause is not None
    assert cause.reason == "codex_install_incomplete"


async def test_wait_until_ready_timeout_keeps_the_error_line_a_stack_trace_pushed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same cause survives when the app-server is still running at the deadline."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    monkeypatch.setattr(codex_native_app_server, "_APP_SERVER_READY_TIMEOUT_SECONDS", 0.0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    server.proc = Mock(returncode=None)
    server.recent_stderr = list(_NODE_INSTALL_STDERR)

    with pytest.raises(RuntimeError) as excinfo:
        await server._wait_until_ready()

    assert str(excinfo.value).endswith(
        f"; stderr={_INSTALL_ERROR_LINE} | " + " | ".join(_NODE_INSTALL_STDERR[-5:])
    )
    cause = classify_start_failure(excinfo.value)
    assert cause is not None
    assert cause.reason == "codex_install_incomplete"


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        pytest.param(
            ['Error: legacy `profile = "ucode"` config is no longer supported'],
            "codex_config_rejected",
            id="legacy-profile",
        ),
        pytest.param(
            ["warning: slow disk", "Error: Model provider `Databricks` not found"],
            "codex_config_rejected",
            id="model-provider",
        ),
        pytest.param(["starting"], "app_server_exited_early", id="unrecognized"),
        pytest.param([], "app_server_exited_early", id="silent"),
    ],
)
async def test_wait_until_ready_early_exit_is_recognized_from_stderr(
    tmp_path: Path, stderr: list[str], reason: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    server.proc = Mock(returncode=1)
    server.recent_stderr = stderr

    with pytest.raises(RuntimeError) as excinfo:
        await server._wait_until_ready()

    cause = classify_start_failure(excinfo.value)
    assert cause is not None
    assert cause.reason == reason


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
