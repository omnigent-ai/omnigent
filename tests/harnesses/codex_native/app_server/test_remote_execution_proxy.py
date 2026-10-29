"""Remote-execution projection for native Codex TUI requests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import serve

from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from omnigent.harnesses.codex_native.execution_environment import CodexRemoteExecution


def _server(tmp_path: Path, upstream_url: str) -> CodexNativeAppServer:
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
