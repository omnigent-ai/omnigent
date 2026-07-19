"""Integration tests for in-place session revert."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import (
    _create_session,
    _wait_for_idle,
)

pytestmark = pytest.mark.asyncio


def _first_user_message(snapshot: dict[str, Any]) -> dict[str, Any]:
    return next(
        item
        for item in snapshot["items"]
        if item["type"] == "message" and item["data"]["role"] == "user"
    )


async def test_revert_rewinds_the_same_session_before_selected_user_message(
    client: httpx.AsyncClient,
) -> None:
    """Revert mutates the source id and returns the selected prompt as a draft."""
    agent = await create_test_agent(client)
    source = await _create_session(client, agent["id"], initial_message="retry this prompt")
    target = _first_user_message(await _wait_for_idle(client, source["id"]))

    response = await client.post(
        f"/v1/sessions/{source['id']}/revert",
        json={"user_message_id": target["id"]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"draft": "retry this prompt", "file_revert_error": None}
    snapshot = (await client.get(f"/v1/sessions/{source['id']}")).json()
    assert snapshot["id"] == source["id"]
    assert snapshot["items"] == []


async def test_revert_resets_a_live_native_runtime_before_rewinding(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native terminals are closed so discarded native history cannot return."""
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    source = await _create_session(client, agent["id"], initial_message="retry native prompt")
    target = _first_user_message(await _wait_for_idle(client, source["id"]))
    runner_client = AsyncMock()
    runner_client.post.return_value = httpx.Response(
        200,
        json={"reset": True},
        request=httpx.Request("POST", "http://runner/reset-state"),
    )

    monkeypatch.setattr(sessions_module, "_agent_is_native", lambda _agent: True)
    monkeypatch.setattr(
        sessions_module,
        "_agent_carries_native_fork_history",
        lambda _agent: True,
    )
    monkeypatch.setattr(
        sessions_module,
        "_get_runner_client",
        AsyncMock(return_value=runner_client),
    )
    response = await client.post(
        f"/v1/sessions/{source['id']}/revert",
        json={"user_message_id": target["id"]},
    )

    assert response.status_code == 200, response.text
    runner_client.post.assert_awaited_once_with(
        f"/v1/sessions/{source['id']}/reset-state",
        timeout=15.0,
    )
    snapshot = (await client.get(f"/v1/sessions/{source['id']}")).json()
    assert snapshot["labels"]["omnigent.fork.carry_history"] == "1"
