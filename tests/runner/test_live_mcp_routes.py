"""Route tests for the runner's live MCP endpoints (``omnigent/runner/live_mcp_routes.py``)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner.live_mcp_routes import register_live_mcp_routes
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError

pytestmark = pytest.mark.asyncio

SESSION = "conv_live"
STATUS = f"/v1/sessions/{SESSION}/live-mcp-servers"
RECONNECT = f"{STATUS}/reconnect"


class _HarnessClient:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.response = response or httpx.Response(200, json={"running": True, "servers": []})
        self.error = error
        self.calls: list[tuple[str, str, Any]] = []

    async def _answer(self) -> httpx.Response:
        if self.error is not None:
            raise self.error
        return self.response

    async def get(self, url: str, *, timeout: float) -> httpx.Response:
        self.calls.append(("GET", url, None))
        return await self._answer()

    async def post(self, url: str, *, json: Any, timeout: float) -> httpx.Response:
        self.calls.append(("POST", url, json))
        return await self._answer()


class _ProcessManager:
    def __init__(self, client: _HarnessClient | None = None, error: Exception | None = None):
        self.client = client
        self.error = error
        self.requests: list[tuple[str, str]] = []

    async def get_client(self, session_id: str, harness: str) -> _HarnessClient:
        self.requests.append((session_id, harness))
        if self.error is not None:
            raise self.error
        if self.client is None:
            raise NoLiveHarnessError("no live harness")
        return self.client


async def _call(manager: _ProcessManager | None, method: str, path: str, **kwargs: Any):
    app = FastAPI()
    register_live_mcp_routes(app, process_manager=manager)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        return await client.request(method, path, **kwargs)


async def test_live_mcp_routes_forward_only_to_a_running_harness() -> None:
    statuses = {"running": True, "servers": [{"name": "search", "status": "failed"}]}
    harness = _HarnessClient(httpx.Response(200, json=statuses))
    manager = _ProcessManager(harness)

    response = await _call(manager, "GET", STATUS)
    assert response.status_code == 200
    assert response.json() == statuses
    response = await _call(manager, "POST", RECONNECT, json={"name": "search"})
    assert response.status_code == 200
    assert harness.calls == [
        ("GET", f"/v1/sessions/{SESSION}/mcp-status", None),
        ("POST", f"/v1/sessions/{SESSION}/mcp-reconnect", {"name": "search"}),
    ]
    # "any" reuses whatever harness is live and never spawns one.
    assert manager.requests == [(SESSION, "any"), (SESSION, "any")]

    # Harness error statuses pass through for the server to map.
    harness.response = httpx.Response(404, json={"error": "mcp_server_unknown"})
    response = await _call(manager, "POST", RECONNECT, json={"name": "gone"})
    assert (response.status_code, response.json()) == (404, {"error": "mcp_server_unknown"})


@pytest.mark.parametrize(
    "manager",
    [None, _ProcessManager(), _ProcessManager(error=RuntimeError("shutting down"))],
    ids=["no-manager", "no-live-harness", "manager-unavailable"],
)
async def test_live_mcp_routes_without_a_running_harness(manager: _ProcessManager | None) -> None:
    response = await _call(manager, "GET", STATUS)
    assert (response.status_code, response.json()) == (200, {"running": False, "servers": []})
    response = await _call(manager, "POST", RECONNECT, json={"name": "search"})
    assert (response.status_code, response.json()) == (409, {"error": "harness_not_running"})


@pytest.mark.parametrize("body", [None, {}, {"name": "  "}, {"name": 3}, ["search"]])
async def test_live_mcp_reconnect_requires_a_server_name(body: Any) -> None:
    harness = _HarnessClient()
    kwargs: dict[str, Any] = {"content": b"not json"} if body is None else {"json": body}
    response = await _call(_ProcessManager(harness), "POST", RECONNECT, **kwargs)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert harness.calls == []


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (httpx.ReadTimeout("slow"), 504, "harness_timeout"),
        (httpx.ConnectError("refused"), 502, "harness_unreachable"),
    ],
)
async def test_live_mcp_routes_report_harness_transport_failures(
    error: Exception, status_code: int, code: str
) -> None:
    manager = _ProcessManager(_HarnessClient(error=error))
    for method, path, kwargs in (
        ("GET", STATUS, {}),
        ("POST", RECONNECT, {"json": {"name": "search"}}),
    ):
        response = await _call(manager, method, path, **kwargs)
        assert response.status_code == status_code
        assert response.json()["error"] == code
