"""Creation cohorts include rejected requests and retain post-persistence failures."""

from __future__ import annotations

import httpx
import pytest

from tests.debug_log_helpers import capture_debug_rows
from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "invalid", "managed_failure", "child_failure"])
async def test_creation_request_correlation_and_classification(
    client: httpx.AsyncClient, outcome: str
) -> None:
    agent = await create_test_agent(client, name="logging-test")
    agent_id = agent["id"]
    body = {"agent_id": agent_id}
    if outcome == "managed_failure":
        body["host_type"] = "managed"
    elif outcome == "child_failure":
        body["parent_session_id"] = "missing-parent"
    elif outcome == "invalid":
        body = {"agent_id": 42}

    with capture_debug_rows("server") as rows:
        response = await client.post("/v1/sessions", json=body)
        # Reads and unrelated requests must not grow the creation denominator.
        await client.get("/v1/sessions")

    starts = [row for row in rows if row["event_name"] == "session_creation_started"]
    ends = [
        row
        for row in rows
        if row["event_name"] in {"session_creation_accepted", "session_creation_failed"}
    ]
    assert len(starts) == len(ends) == 1
    request_id = response.headers["x-request-id"]
    assert starts[0]["attributes"]["request_id"] == request_id
    assert ends[0]["attributes"]["request_id"] == request_id
    created = [row for row in rows if row["event_name"] == "session_created"]
    if outcome == "success":
        assert response.status_code == 201
        assert ends[0]["event_name"] == "session_creation_accepted"
        assert created[0]["session_id"] == response.json()["id"]
    else:
        assert response.status_code >= 400
        assert ends[0]["event_name"] == "session_creation_failed"
    if outcome in {"success", "managed_failure"}:
        assert len(created) == 1
        assert ends[0]["session_id"] == created[0]["session_id"]
        assert created[0]["attributes"]["request_id"] == request_id
    else:
        assert not created
        assert ends[0]["session_id"] is None
    expected_kind = (
        "unknown"
        if outcome == "invalid"
        else "child"
        if outcome == "child_failure"
        else "top_level"
    )
    assert ends[0]["attributes"]["creation_kind"] == expected_kind


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_bundle_creation_links_persistence_before_launch_failure(
    client: httpx.AsyncClient,
    managed: bool,
) -> None:
    import json

    from tests.server.helpers import build_agent_bundle

    with capture_debug_rows("server") as rows:
        response = await client.post(
            "/v1/sessions",
            files={
                "metadata": (
                    None,
                    json.dumps({"host_type": "managed" if managed else "external"}),
                ),
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle("logging-bundle"),
                    "application/gzip",
                ),
            },
        )
    created = [row for row in rows if row["event_name"] == "session_created"]
    assert len(created) == 1
    assert created[0]["attributes"]["request_id"] == response.headers["x-request-id"]
    assert created[0]["attributes"]["creation_kind"] == "top_level"
    final_name = "session_creation_failed" if managed else "session_creation_accepted"
    end = next(row for row in rows if row["event_name"] == final_name)
    assert end["session_id"] == created[0]["session_id"]
    assert (response.status_code >= 400) is managed
