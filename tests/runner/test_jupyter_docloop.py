"""Jupyter admission and forwarding never create another runner or credential."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.jupyter_gateway import jupyter_gateway_router
from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError

BASE = "/v1/sessions/example/docloop/jupyter"


@pytest.mark.parametrize("enabled", [False, True])
def test_runner_flag_and_websocket_auth_precede_lookup(enabled):
    manager = SimpleNamespace(get_client=AsyncMock(), jupyter_channels=Mock())
    manager.get_client.side_effect = NoLiveHarnessError("No fixture process")
    app = create_runner_app(
        server_client=None,
        process_manager=manager,
        auth_token="synthetic-runner",
        docloop_notebook_enabled=enabled,
    )
    client = TestClient(app)
    assert client.get(BASE, headers={"Authorization": "Bearer synthetic-runner"}).status_code == (
        503 if enabled else 404
    )
    manager.get_client.reset_mock()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            BASE + "/api/kernels/kernel/channels", headers={"Origin": "http://testserver"}
        ):
            pytest.fail("Unauthenticated kernel channel opened")
    manager.get_client.assert_not_called()
    manager.jupyter_channels.assert_not_called()


@pytest.mark.asyncio
async def test_absent_harness_websocket_does_not_spawn():
    manager = HarnessProcessManager()
    manager._started = True
    manager._spawn_entry = AsyncMock(side_effect=AssertionError("Do not spawn"))
    with pytest.raises(NoLiveHarnessError):
        async with manager.jupyter_channels("example", BASE + "/api/kernels/kernel/channels"):
            pytest.fail("Missing harness opened")
    manager._spawn_entry.assert_not_called()
    assert not manager._entries


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
async def test_browser_credentials_stay_at_first_hop(method):
    calls = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"fixture"

    async def upstream(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-private-hop"
        assert "cookie" not in request.headers
        assert "x-forwarded-email" not in request.headers
        assert "origin" not in request.headers
        return httpx.Response(200, stream=Body(), headers={"Set-Cookie": "private=never-forward"})

    async def authorize(connection, sid):
        if connection.headers.get("authorization") != "Bearer synthetic-browser":
            raise HTTPException(401)
        assert sid == "example"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        base_url="http://runner",
        headers={"Authorization": "Bearer synthetic-private-hop"},
    ) as private:
        get_client = AsyncMock(return_value=private)
        app = FastAPI()
        app.include_router(jupyter_gateway_router(authorize, get_client, lambda *_: None))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://host"
        ) as browser:
            assert (
                await browser.request(
                    method, BASE + "/api/kernels", content=b"{}", headers={"Origin": "http://host"}
                )
            ).status_code == 401
            get_client.assert_not_called()
            headers = {
                "Authorization": "Bearer synthetic-browser",
                "Origin": "http://host",
                "Cookie": "browser=private",
                "X-Forwarded-Email": "fixture@example.test",
            }
            if method != "GET":
                assert (
                    await browser.request(
                        method,
                        BASE + "/api/kernels",
                        headers={**headers, "Origin": "http://elsewhere"},
                    )
                ).status_code == 403
                get_client.assert_not_called()
            response = await browser.request(
                method, BASE + "/api/kernels", content=b"{}", headers=headers
            )
            assert response.status_code == 200
            assert response.content == b"fixture"
            assert "set-cookie" not in response.headers
            assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1
