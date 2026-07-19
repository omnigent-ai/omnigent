"""Integration tests for in-place session revert."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import (
    _create_session,
    _wait_for_idle,
)

pytestmark = pytest.mark.asyncio


async def _get_session_items(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    response = await client.get(f"/v1/sessions/{session_id}/items")
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_revert_rewinds_the_same_session_before_selected_user_message(
    client: httpx.AsyncClient,
) -> None:
    """Revert mutates the source id and returns the selected prompt as a draft."""
    agent = await create_test_agent(client)
    source = await _create_session(client, agent["id"], initial_message="retry this prompt")
    await _wait_for_idle(client, source["id"])
    source_items = await _get_session_items(client, source["id"])
    target = next(
        item for item in source_items if item["type"] == "message" and item["role"] == "user"
    )

    response = await client.post(
        f"/v1/sessions/{source['id']}/revert",
        json={"user_message_id": target["id"]},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["session"]["id"] == source["id"]
    assert result["draft"] == "retry this prompt"
    assert result["files_reverted"] is False
    assert await _get_session_items(client, source["id"]) == []


async def test_revert_resets_a_live_native_runtime_before_rewinding(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native terminals are closed so discarded native history cannot return."""
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    source = await _create_session(client, agent["id"], initial_message="retry native prompt")
    await _wait_for_idle(client, source["id"])
    source_items = await _get_session_items(client, source["id"])
    target = next(
        item for item in source_items if item["type"] == "message" and item["role"] == "user"
    )
    reset_paths: list[str] = []

    def runner_handler(request: httpx.Request) -> httpx.Response:
        reset_paths.append(request.url.path)
        return httpx.Response(200, json={"reset": True})

    runner_client = httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler),
        base_url="http://runner",
    )

    async def get_runner(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return runner_client

    monkeypatch.setattr(sessions_module, "_agent_is_native", lambda _agent: True)
    monkeypatch.setattr(
        sessions_module,
        "_agent_carries_native_fork_history",
        lambda _agent: True,
    )
    monkeypatch.setattr(sessions_module, "_get_runner_client", get_runner)
    try:
        response = await client.post(
            f"/v1/sessions/{source['id']}/revert",
            json={"user_message_id": target["id"]},
        )
    finally:
        await runner_client.aclose()

    assert response.status_code == 200, response.text
    assert reset_paths == [f"/v1/sessions/{source['id']}/reset-state"]
    result = response.json()
    assert result["session"]["id"] == source["id"]
    assert result["session"]["labels"]["omnigent.fork.carry_history"] == "1"
