"""Remote-execution projection for native Codex TUI requests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import serve

from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from omnigent.harnesses.codex_native.execution_environment import (
    CodexRemoteExecution,
    ManagedExecServerConfig,
)


class _FakeLifecycle:
    def __init__(self) -> None:
        self.acquire_started = asyncio.Event()
        self.ready = asyncio.Event()
        self.released = asyncio.Event()
        self.closed = False

    async def acquire(self, probe: Callable[[], Awaitable[None]]) -> str:
        del probe
        self.acquire_started.set()
        await self.ready.wait()
        return "lease-1"

    async def release(self, lease: str) -> None:
        assert lease == "lease-1"
        self.released.set()

    async def close(self) -> None:
        self.closed = True


class _FailingLifecycle(_FakeLifecycle):
    async def acquire(self, probe: Callable[[], Awaitable[None]]) -> str:
        del probe
        self.acquire_started.set()
        raise RuntimeError("remote environment could not resume")


def _server(
    tmp_path: Path,
    upstream_url: str,
    *,
    managed: bool = False,
) -> CodexNativeAppServer:
    """Build the smallest app-server wrapper needed by the TUI proxy tests."""
    shared_cwd = tmp_path / "shared-cwd"
    shared_cwd.mkdir(exist_ok=True)
    return CodexNativeAppServer(
        codex_path="/opt/codex/bin/codex",
        socket_path=tmp_path / "app-server.sock",
        codex_home=tmp_path / "codex-home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        listen_url=upstream_url,
        remote_execution=CodexRemoteExecution(
            exec_server_url="ws://127.0.0.1:43111",
            cwd=str(shared_cwd),
            managed_exec_server=(
                ManagedExecServerConfig(command=("managed-exec-server",)) if managed else None
            ),
        ),
    )


@pytest.mark.parametrize("method", ["thread/start", "turn/start"])
def test_tui_request_projection_selects_remote_primary_and_local_secondary(
    tmp_path: Path,
    method: str,
) -> None:
    """A TUI turn keeps remote as default while retaining explicit local tools."""
    app_server = _server(tmp_path, "ws://127.0.0.1:9876")

    projected = app_server.project_tui_request(
        json.dumps(
            {
                "id": 7,
                "method": method,
                "params": {
                    "threadId": "thread_123",
                    "cwd": "/stale/client/path",
                    "runtimeWorkspaceRoots": ["/stale/client/path"],
                    "environments": [
                        {
                            "environmentId": "local",
                            "cwd": "/stale/client/path",
                            "runtimeWorkspaceRoots": ["/stale/client/path"],
                        }
                    ],
                },
            }
        )
    )

    payload = json.loads(projected)
    assert payload["params"]["cwd"] == str(tmp_path)
    assert "runtimeWorkspaceRoots" not in payload["params"]
    assert payload["params"]["environments"] == [
        {
            "environmentId": "remote",
            "cwd": str(tmp_path / "shared-cwd"),
            "runtimeWorkspaceRoots": [str(tmp_path / "shared-cwd")],
        },
        {
            "environmentId": "local",
            "cwd": str(tmp_path),
            "runtimeWorkspaceRoots": [str(tmp_path)],
        },
    ]


def test_tui_resume_projection_restores_host_thread_cwd(tmp_path: Path) -> None:
    """A resumed TUI keeps host-owned thread metadata local."""
    app_server = _server(tmp_path, "ws://127.0.0.1:9876")

    projected = app_server.project_tui_request(
        json.dumps(
            {
                "id": 8,
                "method": "thread/resume",
                "params": {
                    "threadId": "thread_123",
                    "cwd": str(tmp_path / "shared-cwd"),
                    "runtimeWorkspaceRoots": [str(tmp_path / "shared-cwd")],
                },
            }
        )
    )

    assert json.loads(projected)["params"] == {
        "threadId": "thread_123",
        "cwd": str(tmp_path),
    }


def test_tui_request_projection_leaves_non_lifecycle_frames_unchanged(tmp_path: Path) -> None:
    """The proxy is transparent to responses, notifications, and binary frames."""
    app_server = _server(tmp_path, "ws://127.0.0.1:9876")
    response = json.dumps({"id": 7, "result": {"ok": True}})

    assert app_server.project_tui_request(response) == response
    assert app_server.project_tui_request(b"opaque") == b"opaque"


def test_unmanaged_clients_do_not_create_a_lifecycle_controller(tmp_path: Path) -> None:
    """Manual remote bindings and local sessions preserve their existing transport."""

    async def scenario() -> None:
        upstream_url = "ws://127.0.0.1:9876"
        remote_server = _server(tmp_path, upstream_url)
        await remote_server.start_tui_execution_proxy()
        try:
            assert remote_server.client_url == upstream_url
            assert remote_server.execution_lifecycle is None
        finally:
            await remote_server.stop_tui_execution_proxy()

        local_server = _server(tmp_path, upstream_url)
        local_server.remote_execution = None
        await local_server.start_tui_execution_proxy()
        assert local_server.client_url == upstream_url
        assert local_server.execution_lifecycle is None
        assert local_server.tui_listen_url is None

    asyncio.run(scenario())


def test_managed_readiness_probes_registered_executor_without_replacing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness reconnects the thread's registered environment instead of replacing it."""
    requests: list[tuple[str, dict[str, object]]] = []
    transports: list[str] = []

    class _Client:
        async def connect(self) -> None:
            return None

        async def request(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            requests.append((method, params))
            return {"result": {}}

        async def close(self) -> None:
            return None

    def client_for_transport(transport: str, **_kwargs: object) -> _Client:
        transports.append(transport)
        return _Client()

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.client_for_transport",
        client_for_transport,
    )
    app_server = _server(tmp_path, "ws://127.0.0.1:9876", managed=True)
    app_server.listen_url = None

    asyncio.run(app_server._probe_remote_execution())

    assert transports == [str(app_server.socket_path)]
    assert requests == [
        ("environment/info", {"environmentId": "remote"}),
    ]


def test_tui_execution_proxy_forwards_and_projects_turn_start(tmp_path: Path) -> None:
    """A real WebSocket client reaches the upstream app-server through the shim."""

    async def scenario() -> tuple[dict[str, object], str]:
        received: list[dict[str, object]] = []

        async def upstream(connection: object) -> None:
            message = await connection.recv()  # type: ignore[attr-defined]
            received.append(json.loads(message))
            await connection.send(json.dumps({"id": 11, "result": {"accepted": True}}))  # type: ignore[attr-defined]

        async with serve(upstream, "127.0.0.1", 0, compression=None) as upstream_server:
            socket = upstream_server.sockets[0]
            upstream_url = f"ws://127.0.0.1:{socket.getsockname()[1]}"
            app_server = _server(tmp_path, upstream_url)
            await app_server.start_tui_execution_proxy()
            try:
                async with websockets.connect(app_server.tui_url, compression=None) as client:
                    await client.send(
                        json.dumps(
                            {
                                "id": 11,
                                "method": "turn/start",
                                "params": {"threadId": "thread_123"},
                            }
                        )
                    )
                    response = await client.recv()
            finally:
                await app_server.stop_tui_execution_proxy()
        return received[0], str(response)

    request, response = asyncio.run(scenario())

    assert request["params"] == {
        "threadId": "thread_123",
        "cwd": str(tmp_path),
        "environments": [
            {
                "environmentId": "remote",
                "cwd": str(tmp_path / "shared-cwd"),
                "runtimeWorkspaceRoots": [str(tmp_path / "shared-cwd")],
            },
            {
                "environmentId": "local",
                "cwd": str(tmp_path),
                "runtimeWorkspaceRoots": [str(tmp_path)],
            },
        ],
    }
    assert json.loads(response) == {"id": 11, "result": {"accepted": True}}


@pytest.mark.parametrize("terminal_method", ["turn/completed", "turn/failed"])
def test_execution_proxy_waits_for_managed_server_and_releases_on_terminal_turn(
    tmp_path: Path,
    terminal_method: str,
) -> None:
    """The turn cannot start before compute is ready, and idle releases its SSH owner."""

    async def scenario() -> tuple[list[dict[str, object]], _FakeLifecycle]:
        received: list[dict[str, object]] = []
        lifecycle = _FakeLifecycle()

        async def upstream(connection: object) -> None:
            message = await connection.recv()  # type: ignore[attr-defined]
            received.append(json.loads(message))
            await connection.send(  # type: ignore[attr-defined]
                json.dumps({"id": 21, "result": {"turn": {"id": "turn-21"}}})
            )
            await connection.send(  # type: ignore[attr-defined]
                json.dumps(
                    {
                        "method": terminal_method,
                        "params": {"turn": {"id": "turn-21", "status": "completed"}},
                    }
                )
            )

        async with serve(upstream, "127.0.0.1", 0, compression=None) as upstream_server:
            socket = upstream_server.sockets[0]
            app_server = _server(
                tmp_path,
                f"ws://127.0.0.1:{socket.getsockname()[1]}",
            )
            app_server.execution_lifecycle = lifecycle
            await app_server.start_tui_execution_proxy()
            try:
                async with websockets.connect(app_server.tui_url, compression=None) as client:
                    await client.send(
                        json.dumps(
                            {
                                "id": 21,
                                "method": "turn/start",
                                "params": {"threadId": "thread-1"},
                            }
                        )
                    )
                    await asyncio.wait_for(lifecycle.acquire_started.wait(), timeout=1.0)
                    await asyncio.sleep(0)
                    assert received == []
                    lifecycle.ready.set()
                    assert json.loads(await client.recv())["result"]["turn"]["id"] == "turn-21"
                    assert json.loads(await client.recv())["method"] == terminal_method
                    await asyncio.wait_for(lifecycle.released.wait(), timeout=1.0)
            finally:
                await app_server.stop_tui_execution_proxy()
        return received, lifecycle

    received, lifecycle = asyncio.run(scenario())
    assert received[0]["method"] == "turn/start"
    assert lifecycle.released.is_set()


@pytest.mark.parametrize(
    "method",
    ["thread/start", "thread/resume", "thread/fork", "thread/shellCommand"],
)
def test_execution_proxy_primes_managed_environment_for_request(
    tmp_path: Path,
    method: str,
) -> None:
    """Thread lifecycle and shell requests run only while the executor is connected."""

    async def scenario() -> _FakeLifecycle:
        received: list[dict[str, object]] = []
        lifecycle = _FakeLifecycle()

        async def upstream(connection: object) -> None:
            message = await connection.recv()  # type: ignore[attr-defined]
            received.append(json.loads(message))
            await connection.send(  # type: ignore[attr-defined]
                json.dumps({"id": 25, "result": {"accepted": True}})
            )

        async with serve(upstream, "127.0.0.1", 0, compression=None) as upstream_server:
            socket = upstream_server.sockets[0]
            app_server = _server(
                tmp_path,
                f"ws://127.0.0.1:{socket.getsockname()[1]}",
                managed=True,
            )
            app_server.execution_lifecycle = lifecycle
            await app_server.start_tui_execution_proxy()
            try:
                async with websockets.connect(app_server.client_url, compression=None) as client:
                    await client.send(
                        json.dumps(
                            {
                                "id": 25,
                                "method": method,
                                "params": {"threadId": "thread-25", "cwd": str(tmp_path)},
                            }
                        )
                    )
                    await asyncio.wait_for(lifecycle.acquire_started.wait(), timeout=1.0)
                    await asyncio.sleep(0)
                    assert received == []
                    lifecycle.ready.set()
                    assert json.loads(await client.recv())["result"]["accepted"] is True
                    assert received[0]["method"] == method
                    await asyncio.wait_for(lifecycle.released.wait(), timeout=1.0)
            finally:
                await app_server.stop_tui_execution_proxy()
        return lifecycle

    lifecycle = asyncio.run(scenario())
    assert lifecycle.released.is_set()


def test_managed_client_releases_from_terminal_event_on_another_connection(
    tmp_path: Path,
) -> None:
    """The event listener releases a turn after the short-lived injector disconnects."""

    async def scenario() -> _FakeLifecycle:
        upstream_connections: list[object] = []
        upstream_ready = asyncio.Event()
        lifecycle = _FakeLifecycle()

        async def upstream(connection: object) -> None:
            upstream_connections.append(connection)
            if len(upstream_connections) == 2:
                upstream_ready.set()
            async for message in connection:  # type: ignore[attr-defined]
                payload = json.loads(message)
                if payload.get("method") == "turn/start":
                    await connection.send(  # type: ignore[attr-defined]
                        json.dumps({"id": 31, "result": {"turn": {"id": "turn-31"}}})
                    )

        async with serve(upstream, "127.0.0.1", 0, compression=None) as upstream_server:
            socket = upstream_server.sockets[0]
            app_server = _server(
                tmp_path,
                f"ws://127.0.0.1:{socket.getsockname()[1]}",
                managed=True,
            )
            app_server.execution_lifecycle = lifecycle
            await app_server.start_tui_execution_proxy()
            try:
                assert app_server.client_url == app_server.tui_url
                async with websockets.connect(app_server.client_url, compression=None) as listener:
                    async with websockets.connect(
                        app_server.client_url, compression=None
                    ) as injector:
                        await asyncio.wait_for(upstream_ready.wait(), timeout=1.0)
                        await injector.send(
                            json.dumps(
                                {
                                    "id": 31,
                                    "method": "turn/start",
                                    "params": {"threadId": "thread-1"},
                                }
                            )
                        )
                        await asyncio.wait_for(lifecycle.acquire_started.wait(), timeout=1.0)
                        lifecycle.ready.set()
                        response = json.loads(await injector.recv())
                        assert response["result"]["turn"]["id"] == "turn-31"

                    await upstream_connections[0].send(  # type: ignore[attr-defined]
                        json.dumps(
                            {
                                "method": "turn/completed",
                                "params": {"turn": {"id": "turn-31", "status": "completed"}},
                            }
                        )
                    )
                    assert json.loads(await listener.recv())["method"] == "turn/completed"
                    await asyncio.wait_for(lifecycle.released.wait(), timeout=1.0)
            finally:
                await app_server.stop_tui_execution_proxy()
        return lifecycle

    lifecycle = asyncio.run(scenario())
    assert lifecycle.released.is_set()


def test_managed_client_disconnect_does_not_release_an_accepted_turn(
    tmp_path: Path,
) -> None:
    """Drain a dropped injector and reconcile an earlier terminal event with its response."""

    async def scenario() -> _FakeLifecycle:
        upstream_connections: list[object] = []
        upstream_ready = asyncio.Event()
        start_received = asyncio.Event()
        allow_response = asyncio.Event()
        response_sent = asyncio.Event()
        lifecycle = _FakeLifecycle()
        lifecycle.ready.set()

        async def upstream(connection: object) -> None:
            upstream_connections.append(connection)
            if len(upstream_connections) == 2:
                upstream_ready.set()
            async for message in connection:  # type: ignore[attr-defined]
                payload = json.loads(message)
                if payload.get("method") == "turn/start":
                    start_received.set()
                    await allow_response.wait()
                    await connection.send(  # type: ignore[attr-defined]
                        json.dumps({"id": 33, "result": {"turn": {"id": "turn-33"}}})
                    )
                    response_sent.set()

        async with serve(upstream, "127.0.0.1", 0, compression=None) as upstream_server:
            socket = upstream_server.sockets[0]
            app_server = _server(
                tmp_path,
                f"ws://127.0.0.1:{socket.getsockname()[1]}",
                managed=True,
            )
            app_server.execution_lifecycle = lifecycle
            await app_server.start_tui_execution_proxy()
            try:
                async with websockets.connect(app_server.client_url, compression=None) as listener:
                    injector = await websockets.connect(app_server.client_url, compression=None)
                    await asyncio.wait_for(upstream_ready.wait(), timeout=1.0)
                    await injector.send(
                        json.dumps(
                            {
                                "id": 33,
                                "method": "turn/start",
                                "params": {"threadId": "thread-1"},
                            }
                        )
                    )
                    await asyncio.wait_for(start_received.wait(), timeout=1.0)
                    await injector.close()
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(lifecycle.released.wait(), timeout=0.05)

                    await upstream_connections[0].send(  # type: ignore[attr-defined]
                        json.dumps(
                            {
                                "method": "turn/completed",
                                "params": {"turn": {"id": "turn-33", "status": "completed"}},
                            }
                        )
                    )
                    assert json.loads(await listener.recv())["method"] == "turn/completed"
                    assert not lifecycle.released.is_set()

                    allow_response.set()
                    try:
                        await asyncio.wait_for(response_sent.wait(), timeout=1.0)
                    except TimeoutError:
                        response_reached_proxy = False
                    else:
                        response_reached_proxy = True
                    assert response_reached_proxy
                    await asyncio.wait_for(lifecycle.released.wait(), timeout=1.0)
            finally:
                await app_server.stop_tui_execution_proxy()
        return lifecycle

    lifecycle = asyncio.run(scenario())
    assert lifecycle.released.is_set()


def test_execution_proxy_releases_when_app_server_rejects_turn(tmp_path: Path) -> None:
    """An app-server turn refusal cannot leave the managed command running."""

    async def scenario() -> _FakeLifecycle:
        lifecycle = _FakeLifecycle()

        async def upstream(connection: object) -> None:
            await connection.recv()  # type: ignore[attr-defined]
            await connection.send(  # type: ignore[attr-defined]
                json.dumps({"id": 32, "error": {"code": -32000, "message": "busy"}})
            )

        async with serve(upstream, "127.0.0.1", 0, compression=None) as upstream_server:
            socket = upstream_server.sockets[0]
            app_server = _server(
                tmp_path,
                f"ws://127.0.0.1:{socket.getsockname()[1]}",
            )
            app_server.execution_lifecycle = lifecycle
            await app_server.start_tui_execution_proxy()
            try:
                async with websockets.connect(app_server.tui_url, compression=None) as client:
                    await client.send(
                        json.dumps(
                            {
                                "id": 32,
                                "method": "turn/start",
                                "params": {"threadId": "thread-1"},
                            }
                        )
                    )
                    await asyncio.wait_for(lifecycle.acquire_started.wait(), timeout=1.0)
                    lifecycle.ready.set()
                    assert json.loads(await client.recv())["error"]["message"] == "busy"
                    await asyncio.wait_for(lifecycle.released.wait(), timeout=1.0)
            finally:
                await app_server.stop_tui_execution_proxy()
        return lifecycle

    lifecycle = asyncio.run(scenario())
    assert lifecycle.released.is_set()


def test_execution_proxy_rejects_turn_when_managed_server_cannot_start(tmp_path: Path) -> None:
    """Provisioning failure is an RPC refusal and never reaches the Codex app-server."""

    async def scenario() -> tuple[list[dict[str, object]], dict[str, object]]:
        received: list[dict[str, object]] = []

        async def upstream(connection: object) -> None:
            message = await connection.recv()  # type: ignore[attr-defined]
            received.append(json.loads(message))

        async with serve(upstream, "127.0.0.1", 0, compression=None) as upstream_server:
            socket = upstream_server.sockets[0]
            app_server = _server(
                tmp_path,
                f"ws://127.0.0.1:{socket.getsockname()[1]}",
            )
            app_server.execution_lifecycle = _FailingLifecycle()
            await app_server.start_tui_execution_proxy()
            try:
                async with websockets.connect(app_server.tui_url, compression=None) as client:
                    await client.send(
                        json.dumps(
                            {
                                "id": 22,
                                "method": "turn/start",
                                "params": {"threadId": "thread-1"},
                            }
                        )
                    )
                    response = json.loads(await asyncio.wait_for(client.recv(), timeout=1.0))
            finally:
                await app_server.stop_tui_execution_proxy()
        return received, response

    received, response = asyncio.run(scenario())
    assert received == []
    assert response["id"] == 22
    assert "remote environment could not resume" in response["error"]["message"]
