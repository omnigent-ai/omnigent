"""Durability coverage for every elicitation-origin call site (OMN-104).

Independent cross-vendor review (round 2) found that OMN-104's durable
outbox writes were wired into only ONE of several code paths that raise or
resolve an elicitation:

* ``_publish_and_wait_for_harness_elicitation`` (claude-native/codex-native
  permission hooks) already called ``session_outbox.record_elicitation_raised``
  — covered by other test files.
* ``_register_policy_elicitation`` (the relay tool-call ASK gate AND the MCP
  ``tools/call`` ASK gate both funnel through this one function) had NO
  durable write at all.
* The generic ``POST /v1/sessions/{id}/events``'s ``mcp_elicitation`` branch
  (an external MCP server's own ``elicitation/create`` relayed through) had
  NO durable write at all.
* The generic ``POST /v1/sessions/{id}/events``'s ``approval`` branch called
  ``_resolve_elicitation`` directly but never ``record_decision`` /
  ``record_elicitation_resumed`` — a second, less-obvious entry point that
  could resolve an elicitation while the durable ledger stayed stuck at
  ``pending`` and no ``session.resumed`` ever fired.

This file drives each of those gaps through the REAL HTTP endpoint (not the
internal function directly) and asserts on the durable
``session_lifecycle_store`` state, closing the coverage gap the review found.
The remaining origin — harness-subprocess ``ctx.elicit()`` relayed via
``_relay_runner_stream_once`` — requires the real WS-tunnel machinery and is
covered separately in
``tests/server/integration/test_manager_webhook_reconnect_redelivery_e2e.py``'s
sibling file for that mechanism.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import (
    _COST_GUARD_SOFT_GUARDRAILS,
    _create_session,
    _evaluate_tool,
)

pytestmark = pytest.mark.asyncio


async def test_relay_tool_call_ask_durably_records_awaiting_decision(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    The relay tool-call ASK gate (``_evaluate_tool_call_policy`` ->
    ``_register_policy_elicitation``) durably records the elicitation and a
    ``session.awaiting_decision`` outbox row BEFORE the SSE event publishes —
    previously this path had no durable write at all. The MCP ``tools/call``
    ASK gate shares this exact same ``_register_policy_elicitation`` function
    body, so this single real-HTTP exercise covers both entry points.
    """
    agent = await create_test_agent(client, guardrails=_COST_GUARD_SOFT_GUARDRAILS)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.1}},
    )
    assert resp.status_code == 202, resp.text

    verdict = await _evaluate_tool(client, sid)
    assert verdict["verdict"] == "pending", verdict
    eid = verdict["elicitation_id"]

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(eid)
    assert elicitation is not None
    assert elicitation.status == "pending"
    assert elicitation.session_id == sid

    deliveries, _ = store.list_deliveries(sid, limit=100)
    awaiting = [d for d in deliveries if d.event_type == "session.awaiting_decision"]
    assert len(awaiting) == 1, deliveries


async def test_mcp_elicitation_event_durably_records_awaiting_decision(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    The generic events endpoint's ``mcp_elicitation`` branch (an external
    MCP server's own ``elicitation/create`` relayed through
    ``routes_events.py``) durably records the elicitation and a
    ``session.awaiting_decision`` outbox row — previously this path had no
    durable write at all, distinct from both the internal policy ASK gate
    and the harness-subprocess ``ctx.elicit()`` relay.
    """
    agent = await create_test_agent(client, "test-mcp-elicit-durability")
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    publish = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "mcp_elicitation",
            "data": {
                "message": "MCP server asks: proceed with deletion?",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"confirm": {"type": "boolean"}},
                },
            },
        },
    )
    assert publish.status_code == 202, publish.text
    eid = publish.json()["elicitation_id"]
    assert isinstance(eid, str) and eid

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(eid)
    assert elicitation is not None
    assert elicitation.status == "pending"
    assert elicitation.session_id == sid

    deliveries, _ = store.list_deliveries(sid, limit=100)
    awaiting = [d for d in deliveries if d.event_type == "session.awaiting_decision"]
    assert len(awaiting) == 1, deliveries


async def test_generic_events_approval_path_durably_resumes(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The generic ``POST /v1/sessions/{id}/events`` ``approval`` branch —
    NOT the dedicated ``.../elicitations/{eid}/resolve`` URL endpoint —
    durably records the decision AND ``session.resumed`` on a truthful
    runner ack. Previously this branch called ``_resolve_elicitation``
    directly with no durable write at all: a client using this generic
    path could consume an elicitation while the ledger stayed stuck at
    ``pending`` and no ``session.resumed`` ever fired.
    """

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        del session_id, runner_router
        fake_runner = FastAPI()

        @fake_runner.post("/v1/sessions/{conversation_id}/events")
        async def _events(conversation_id: str) -> JSONResponse:
            del conversation_id
            return JSONResponse(
                status_code=200,
                content={"ok": True},
                headers={"X-Omnigent-Pending-Approval-Resolved": "true"},
            )

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake_runner), base_url="http://fake-runner"
        )

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._get_runner_client",
        _fake_get_runner_client,
    )

    agent = await create_test_agent(client, guardrails=_COST_GUARD_SOFT_GUARDRAILS)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.1}},
    )
    assert resp.status_code == 202, resp.text

    verdict = await _evaluate_tool(client, sid)
    assert verdict["verdict"] == "pending", verdict
    eid = verdict["elicitation_id"]

    conv_store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner_generic_events_approval"
    assert conv_store.set_runner_id(sid, runner_id)
    conv_store.touch_runner_liveness([runner_id], int(time.time()))

    # POST the verdict to the GENERIC events endpoint, NOT the dedicated
    # resolve URL — this is the exact path the review found bypassed
    # durability.
    approve = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "approval", "data": {"elicitation_id": eid, "action": "accept"}},
    )
    assert approve.status_code == 202, approve.text

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(eid)
    assert elicitation is not None
    assert elicitation.status == "delivered_to_runner"
    assert elicitation.decision_payload is not None

    deliveries, _ = store.list_deliveries(sid, limit=100)
    resumed = [d for d in deliveries if d.event_type == "session.resumed"]
    assert len(resumed) == 1, deliveries
