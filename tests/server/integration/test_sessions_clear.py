"""Integration tests for the explicit ``clear`` control event.

The web-UI ``/clear`` command POSTs ``{"type": "clear"}`` to
``POST /v1/sessions/{id}/events``. Like ``compact``, the server forwards the
control to the bound runner, which types the harness's own ``/clear`` into the
session terminal (200), no-ops for harnesses without one (204), or reports a
failed injection (5xx).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a bare session bound to *agent_id* and return its id.

    :param client: The test HTTP client.
    :param agent_id: Agent id to bind, e.g. ``"ag_abc123"``.
    :returns: The new session id, e.g. ``"conv_abc123"``.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id, "initial_items": []})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _fake_runner_returning(clear_status: int) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """
    Build a mock runner client that returns *clear_status* for ``clear``.

    :param clear_status: HTTP status for a forwarded ``clear`` control, e.g.
        ``200`` (native handled), ``204`` (no-op) or ``503`` (pane not ready).
    :returns: The mock client and the list capturing forwarded clear bodies.
    """
    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record clear POSTs and return the configured status."""
        body = (
            json.loads(request.content) if request.method == "POST" and request.content else None
        )
        if isinstance(body, dict) and body.get("type") == "clear":
            captured.append(body)
            return httpx.Response(clear_status)
        return httpx.Response(204)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://runner"
    ), captured


async def _post_clear(
    client: httpx.AsyncClient, clear_status: int
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    """
    POST a ``clear`` control with the runner answering *clear_status*.

    :param client: The test HTTP client.
    :param clear_status: Status the fake runner returns for the control.
    :returns: The server response and the forwarded clear bodies.
    """
    from omnigent.runtime import set_runner_client

    runner, captured = _fake_runner_returning(clear_status)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(f"/v1/sessions/{sid}/events", json={"type": "clear", "data": {}})
    finally:
        await runner.aclose()
        set_runner_client(None)
    return resp, captured


async def test_clear_is_forwarded_to_the_runner(client: httpx.AsyncClient) -> None:
    """A runner that typed the harness's /clear makes the control succeed."""
    resp, captured = await _post_clear(client, 200)

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}
    assert captured == [{"type": "clear"}]


async def test_clear_reports_unsupported_session_types(client: httpx.AsyncClient) -> None:
    """A runner no-op (harness without a native /clear) is a clear 400."""
    resp, captured = await _post_clear(client, 204)

    assert resp.status_code == 400, resp.text
    assert "/clear is not available" in resp.text
    assert captured == [{"type": "clear"}]


async def test_clear_surfaces_a_failed_injection(client: httpx.AsyncClient) -> None:
    """A runner that could not type /clear surfaces an error, not success."""
    resp, captured = await _post_clear(client, 503)

    assert resp.status_code >= 500, resp.text
    assert "Clear failed" in resp.text
    assert captured == [{"type": "clear"}]
