"""Creation cohorts include rejected requests and retain post-persistence failures."""

from __future__ import annotations

import httpx
import pytest

from omnigent import debug_logging
from omnigent.server import creation_logging
from tests.debug_log_helpers import capture_debug_rows
from tests.server.helpers import create_test_agent


def test_creation_stage_accumulates_repeated_measurements(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated work in one stage is reported as one accumulated duration."""
    debug_logging.reset_request_audit_attrs()
    moments = iter([10.0, 10.002, 20.0, 20.003])
    monkeypatch.setattr(creation_logging.time, "perf_counter", lambda: next(moments))

    with creation_logging.creation_stage("create_persistence_ms"):
        pass
    with creation_logging.creation_stage("create_persistence_ms"):
        pass

    attrs = debug_logging.current_request_audit_attrs()
    assert float(attrs["create_persistence_ms"]) == pytest.approx(5.0)


def test_creation_stage_records_failed_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stage that raises still contributes timing to the request error row."""
    debug_logging.reset_request_audit_attrs()
    moments = iter([30.0, 30.004])
    monkeypatch.setattr(creation_logging.time, "perf_counter", lambda: next(moments))

    with pytest.raises(RuntimeError, match="boom"):
        with creation_logging.creation_stage("create_acl_ms"):
            raise RuntimeError("boom")

    attrs = debug_logging.current_request_audit_attrs()
    assert float(attrs["create_acl_ms"]) == pytest.approx(4.0)


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
        assert float(ends[0]["attributes"]["create_persistence_ms"]) >= 0
    else:
        assert response.status_code >= 400
        assert ends[0]["event_name"] == "session_creation_failed"
    if outcome in {"success", "managed_failure"}:
        assert len(created) == 1
        assert created[0]["attributes"]["agent_id"] == agent_id
        assert created[0]["attributes"]["harness"] == "claude-sdk"
        assert created[0]["attributes"]["harness_source"] == "create_selection"
        assert created[0]["attributes"]["session_kind"] == "default"
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
async def test_session_created_logs_parent_session_id_for_child(
    client: httpx.AsyncClient,
) -> None:
    """A sub-agent child's ``session_created`` row carries its parent link.

    This is the join key a debug-log query needs to attribute a child session
    back to its parent; a top-level session has no parent, so the attribute is
    absent (null-valued attributes are dropped at the sink).
    """
    agent = await create_test_agent(client, name="parent-link-test")
    agent_id = agent["id"]

    with capture_debug_rows("server") as parent_rows:
        parent = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert parent.status_code == 201
    parent_id = parent.json()["id"]
    # A top-level session has no parent, so the attribute is omitted entirely
    # (null-valued attributes are dropped at the sink) — absence means no parent.
    parent_created = [
        row
        for row in parent_rows
        if row["event_name"] == "session_created" and row["session_id"] == parent_id
    ]
    assert len(parent_created) == 1
    assert "parent_session_id" not in parent_created[0]["attributes"]
    assert parent_created[0]["attributes"]["harness"] == "claude-sdk"

    child_agent = await create_test_agent(
        client,
        name="child-other-harness",
        executor={"type": "omnigent", "config": {"harness": "codex"}},
    )

    with capture_debug_rows("server") as rows:
        child = await client.post(
            "/v1/sessions",
            json={"agent_id": child_agent["id"], "parent_session_id": parent_id},
        )
    assert child.status_code == 201
    child_id = child.json()["id"]

    created = [
        row
        for row in rows
        if row["event_name"] == "session_created" and row["session_id"] == child_id
    ]
    assert len(created) == 1
    assert created[0]["attributes"]["parent_session_id"] == parent_id
    assert created[0]["attributes"]["creation_kind"] == "child"
    assert created[0]["attributes"]["session_kind"] == "sub_agent"
    assert created[0]["attributes"]["agent_id"] == child_agent["id"]
    assert created[0]["attributes"]["harness"] == "codex"


@pytest.mark.asyncio
async def test_publish_session_created_logs_parent_link_for_native_subagent() -> None:
    """Native-harness sub-agents log ``session_created`` with their parent link.

    They are minted outside the general create path's ``session_created``
    logger, so ``_publish_session_created`` carries the join key for them.
    """
    from unittest.mock import MagicMock

    from omnigent.server.routes._sessions.helpers import _publish_session_created

    # The log fires before the subagent-activity write; a store whose lookup
    # returns None makes that write a clean no-op so the test stays focused.
    store = MagicMock()
    store.get_conversation.return_value = None

    # Bind the parent's ambient session scope; the native-path log must identify
    # the child without rebinding it (it runs in the parent's relay context).
    debug_logging.set_current_session_id("conv_parent")
    try:
        with capture_debug_rows("server") as rows:
            await _publish_session_created(
                "conv_parent", "conv_child", "ag_abc", store, harness="codex-native"
            )
        assert debug_logging.current_session_id() == "conv_parent"
    finally:
        debug_logging.set_current_session_id(None)

    created = [
        row
        for row in rows
        if row["event_name"] == "session_created" and row["session_id"] == "conv_child"
    ]
    assert len(created) == 1
    assert created[0]["attributes"]["parent_session_id"] == "conv_parent"
    assert created[0]["attributes"]["creation_kind"] == "child"
    assert created[0]["attributes"]["agent_id"] == "ag_abc"
    assert created[0]["attributes"]["harness"] == "codex-native"
    assert created[0]["attributes"]["harness_source"] == "subagent_event"


@pytest.mark.asyncio
async def test_acp_child_creation_does_not_infer_harness_from_transport(
    client: httpx.AsyncClient,
) -> None:
    agent = await create_test_agent(
        client,
        name="acp-child-logging",
        executor={"type": "omnigent", "config": {"harness": "devin"}},
    )
    parent = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    with capture_debug_rows("server") as rows:
        response = await client.post(
            f"/v1/sessions/{parent_id}/events",
            json={
                "type": "external_acp_subagent_start",
                "data": {"subagent_id": "worker", "title": "Worker"},
            },
        )

    assert response.status_code == 202, response.text
    child_id = response.json()["child_session_id"]
    created = [row for row in rows if row["event_name"] == "session_created"]
    assert len(created) == 1
    assert created[0]["session_id"] == child_id
    attrs = created[0]["attributes"]
    assert attrs["parent_session_id"] == parent_id
    assert "harness" not in attrs
    assert attrs["harness_resolution"] == "unknown"


@pytest.mark.asyncio
async def test_bundle_child_creation_logs_parent_session_id(
    client: httpx.AsyncClient,
) -> None:
    """A multipart (bundled) child create forwards its parent link too.

    The multipart path persists ``metadata.parent_session_id`` but has its own
    ``session_created`` caller, so it must forward the parent id just like the
    JSON path.
    """
    import json

    from tests.server.helpers import build_agent_bundle

    agent = await create_test_agent(client, name="bundle-parent-link")
    parent = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    with capture_debug_rows("server") as rows:
        child = await client.post(
            "/v1/sessions",
            files={
                "metadata": (None, json.dumps({"parent_session_id": parent_id})),
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle("bundle-child"),
                    "application/gzip",
                ),
            },
        )
    assert child.status_code == 201
    # The multipart create returns CreatedSessionResponse (session_id), not the
    # JSON path's SessionResponse (id).
    child_id = child.json()["session_id"]

    created = [
        row
        for row in rows
        if row["event_name"] == "session_created" and row["session_id"] == child_id
    ]
    assert len(created) == 1
    assert created[0]["attributes"]["parent_session_id"] == parent_id
    assert created[0]["attributes"]["harness"] == "claude-sdk"
    assert created[0]["attributes"]["agent_id"] == child.json()["agent_id"]


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
