"""Native Plan snapshots survive a cold Server cache without starting work."""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.routes._sessions.common import _session_todos_cache
from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
async def test_native_plan_survives_server_cache_restart(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201
    session_id = created.json()["id"]
    todos = [
        {"content": "Inspect the example", "status": "completed", "activeForm": "Inspecting"},
        {"content": "Verify the result", "status": "in_progress", "activeForm": "Verifying"},
    ]
    try:
        reported = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_todos", "data": {"todos": todos}},
        )
        assert reported.status_code == 202
        live = (await client.get(f"/v1/sessions/{session_id}")).json()
        assert live["todos"] == todos
        # A replacement Server process starts with no process-local plan cache.
        _session_todos_cache.pop(session_id, None)
        restored = (await client.get(f"/v1/sessions/{session_id}")).json()
        assert restored["todos"] == todos
        assert restored["items"] == live["items"] == []
        assert restored["status"] == live["status"]
    finally:
        _session_todos_cache.pop(session_id, None)


@pytest.mark.asyncio
async def test_explicit_empty_plan_is_durable_and_oversize_does_not_replace_it(
    client: httpx.AsyncClient,
) -> None:
    agent = await create_test_agent(client)
    session_id = (await client.post("/v1/sessions", json={"agent_id": agent["id"]})).json()["id"]
    todos = [{"content": "Example", "status": "pending", "activeForm": "Preparing"}]
    try:
        for value in (todos, []):
            posted = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "external_session_todos", "data": {"todos": value}},
            )
            assert posted.status_code == 202
        rejected = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_todos", "data": {"todos": todos * 101}},
        )
        assert rejected.status_code == 400
        _session_todos_cache.pop(session_id, None)
        assert (await client.get(f"/v1/sessions/{session_id}")).json()["todos"] == []
    finally:
        _session_todos_cache.pop(session_id, None)
