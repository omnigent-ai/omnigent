"""Real WebSocket handshakes carry exactly one Omnigent User-Agent.

``websockets`` adds its own ``User-Agent`` on top of ``additional_headers``, so
each tunnel/attach site passes ours as ``user_agent_header``. These tests run a
loopback WebSocket server that records the handshake headers and rejects it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request

from omnigent.cli_auth import DATABRICKS_EXTRA_HEADERS_ENV_VAR
from omnigent.telemetry.request_headers import CLIENT_COMPONENT_ENV_VAR
from omnigent.telemetry.surface import classify_surface

# Spellings a caller's extra-headers selector may use for User-Agent. Each one
# must still leave exactly one User-Agent on the wire.
_EXTRA_UA_KEYS = pytest.mark.parametrize("ua_key", ["User-Agent", "user-agent", "USER-AGENT"])
_EXTRA_UA_VALUE = "extra-caller/1.0"


class _CaptureServer:
    """Loopback WS server recording every handshake's User-Agent values."""

    def __init__(self) -> None:
        self.user_agents: list[list[str]] = []
        self.port = 0

    def process_request(self, connection: ServerConnection, request: Request) -> object:
        self.user_agents.append(request.headers.get_all("User-Agent"))
        return connection.respond(403, "rejected by test\n")


@contextlib.asynccontextmanager
async def _capture_server() -> AsyncIterator[_CaptureServer]:
    server = _CaptureServer()

    async def _handler(_ws: ServerConnection) -> None:
        return None

    async with serve(_handler, "127.0.0.1", 0, process_request=server.process_request) as ws:
        server.port = ws.sockets[0].getsockname()[1]
        yield server


@pytest_asyncio.fixture()
async def capture() -> AsyncIterator[_CaptureServer]:
    async with _capture_server() as server:
        yield server


def _assert_one_omnigent_user_agent(capture: _CaptureServer, surface: str) -> None:
    assert capture.user_agents, "no handshake reached the server"
    for values in capture.user_agents:
        assert len(values) == 1, values
        assert classify_surface(values[0]) == surface, values


@_EXTRA_UA_KEYS
async def test_runner_tunnel_handshake_sends_runner_user_agent(
    capture: _CaptureServer, monkeypatch: pytest.MonkeyPatch, ua_key: str
) -> None:
    from omnigent.runner.transports.ws_tunnel.serve import _serve_tunnel_once

    monkeypatch.setenv(CLIENT_COMPONENT_ENV_VAR, "runner")
    monkeypatch.setenv(DATABRICKS_EXTRA_HEADERS_ENV_VAR, json.dumps({ua_key: _EXTRA_UA_VALUE}))

    async def _app(scope: object, receive: object, send: object) -> None:
        del scope, receive, send

    with pytest.raises(websockets.InvalidStatus):
        await _serve_tunnel_once(
            _app,
            tunnel_url=f"ws://127.0.0.1:{capture.port}/v1/runners/r/tunnel",
            server_url=f"http://127.0.0.1:{capture.port}",
            runner_id="r",
            runner_version="0.1.0",
        )
    _assert_one_omnigent_user_agent(capture, "runner")


@_EXTRA_UA_KEYS
async def test_native_attach_handshake_sends_omnigent_user_agent(
    capture: _CaptureServer, monkeypatch: pytest.MonkeyPatch, ua_key: str
) -> None:
    """The UA is ours even when the caller passes its own (any casing) in headers."""
    from omnigent.harnesses.claude_native.main import _websocket_connect

    monkeypatch.setenv(CLIENT_COMPONENT_ENV_VAR, "runner")
    url = f"ws://127.0.0.1:{capture.port}/v1/sessions/conv_1/terminals/t/attach"
    with pytest.raises(websockets.InvalidStatus):
        async with _websocket_connect(url, headers={ua_key: _EXTRA_UA_VALUE}):
            pass
    _assert_one_omnigent_user_agent(capture, "runner")


@_EXTRA_UA_KEYS
def test_host_tunnel_handshake_sends_host_user_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ua_key: str
) -> None:
    """Replays the host's real connect kwargs against a capture server."""
    import websockets.asyncio.client as ws_client

    import omnigent.runner._entry as entry_mod
    from omnigent.host.connect import run_host_process

    real_connect = ws_client.connect
    recorded: list[dict[str, object]] = []

    def _spy(url: str, **kwargs: object) -> object:
        del url
        recorded.append(kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / ".omnigent"))
    monkeypatch.setenv(CLIENT_COMPONENT_ENV_VAR, "cli")
    monkeypatch.setenv(DATABRICKS_EXTRA_HEADERS_ENV_VAR, json.dumps({ua_key: _EXTRA_UA_VALUE}))
    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)
    monkeypatch.setattr(ws_client, "connect", _spy)
    run_host_process(server_url="https://srv.example.com", config_path=tmp_path / "config.yaml")
    assert recorded, "host never opened its tunnel"
    kwargs = {key: recorded[0][key] for key in ("additional_headers", "user_agent_header")}

    async def _replay() -> _CaptureServer:
        async with _capture_server() as server:
            url = f"ws://127.0.0.1:{server.port}/v1/hosts/h/tunnel"
            with pytest.raises(websockets.InvalidStatus):
                async with real_connect(url, **kwargs):  # type: ignore[arg-type]
                    pass
            return server

    _assert_one_omnigent_user_agent(asyncio.run(_replay()), "host")
