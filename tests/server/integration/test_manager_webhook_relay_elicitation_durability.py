"""Durability coverage for the runner-relay ``ctx.elicit()`` branch (OMN-104).

The last of the elicitation-origin gaps cross-vendor review (round 2) found:
``_relay_runner_stream_once`` (``omnigent/server/routes/_sessions/orchestration.py``)
is a long-lived background task that opens ``GET /v1/sessions/{id}/stream`` on the
RUNNER and relays every event it reads onto the local ``session_stream.publish`` on
the SERVER side. A harness subprocess's own ``ctx.elicit()`` call (generic
scaffold-backed harnesses) emits ``response.elicitation_request`` on its own stream,
which the runner forwards, which this relay loop picks up — a code path distinct
from both the internal policy ASK gate and an external MCP server's
``elicitation/create`` (both covered in
``test_manager_webhook_elicitation_origin_durability.py``), and distinct from the
claude-native/codex-native permission-hook contract (which never reaches this relay
loop at all).

Driving this end to end through a REAL harness subprocess turn (harness ->
runner's own internal event-queue plumbing -> runner's ``GET /stream`` -> this
relay) was attempted first and abandoned: the harness genuinely calls
``ctx.elicit()`` and parks correctly, but nothing from that turn — not even the
routine turn-lifecycle envelope events — ever reaches the AP-side relay in that
test wiring, which is a pre-existing test-infrastructure gap in getting a fake
in-process harness's SSE output through the runner's own internal queue
(``_publish_event`` / ``_session_event_queues``, both closures private to
``create_runner_app``, not reachable from a test without touching product code),
not something this OMN-104 change introduced or could fix. The general
harness -> runner -> AP relay pipeline itself is already proven end-to-end for
ordinary turn events by ``test_native_session_happy_path_via_ws_tunnel`` and
friends in ``test_sessions_tunnel_three_layer.py``.

This file instead drives ``_relay_runner_stream_once`` directly (the real,
unmodified function under test — not a mock of it) against a REAL HTTP
``GET /v1/sessions/{id}/stream`` endpoint that emits genuine SSE bytes in the
runner's exact wire format (``data: {json}\\n\\n``, see
``omnigent/runner/app.py``'s ``stream_session``/``_event_generator``), carrying a
real ``ElicitationRequestEvent``. This isolates the one thing OMN-104 actually
changed in this function (the durable write on the ``response.elicitation_request``
branch) from the separate, pre-existing question of how an event gets from a live
harness subprocess into the runner's own queue in the first place.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import _create_session

pytestmark = pytest.mark.asyncio


def _build_fake_runner_stream_app(elicitation_id: str) -> FastAPI:
    """A minimal runner stand-in serving one real SSE stream.

    Mirrors ``omnigent/runner/app.py``'s ``stream_session`` wire format exactly
    (heartbeat frame first, then event frames, then ``[DONE]``) so
    ``_relay_runner_stream_once``'s real parsing loop (``resp.aiter_text()`` +
    ``"\\n\\n"``-delimited ``data:`` frames) exercises the same bytes it would
    read from a real runner.
    """
    app = FastAPI()

    @app.get("/v1/sessions/{session_id}/stream")
    async def stream(session_id: str) -> StreamingResponse:
        del session_id

        async def _gen():
            yield b'data: {"type": "session.heartbeat"}\n\n'
            event = {
                "type": "response.elicitation_request",
                "elicitation_id": elicitation_id,
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "harness asks: proceed?",
                    "requestedSchema": {"type": "object"},
                },
            }
            yield ("data: " + json.dumps(event) + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    return app


async def test_relay_ctx_elicit_durably_records_awaiting_decision(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``_relay_runner_stream_once`` durably records the elicitation ledger row
    and a ``session.awaiting_decision`` outbox event when it relays a real
    ``response.elicitation_request`` frame read over real HTTP SSE — the
    harness-subprocess ``ctx.elicit()`` origin, previously the only
    elicitation-origin path with no durable write at all.
    """
    agent = await create_test_agent(client, "test-relay-elicit-durability")
    session = await _create_session(client, agent["id"])
    session_id = session["id"]
    elicitation_id = "elicit_relay_ctx_test"

    conv_store = SqlAlchemyConversationStore(db_uri)
    fake_runner = _build_fake_runner_stream_app(elicitation_id)
    runner_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_runner), base_url="http://fake-runner"
    )
    try:
        ready = asyncio.Event()
        # The real function under test: connects, parses real SSE bytes, and
        # (via this PR's fix) durably records the raised elicitation before
        # returning at [DONE].
        await asyncio.wait_for(
            _relay_runner_stream_once(session_id, runner_client, conv_store, ready=ready),
            timeout=10.0,
        )
    finally:
        await runner_client.aclose()

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(elicitation_id)
    assert elicitation is not None
    assert elicitation.status == "pending"
    assert elicitation.session_id == session_id

    deliveries, _ = store.list_deliveries(session_id, limit=100)
    awaiting = [d for d in deliveries if d.event_type == "session.awaiting_decision"]
    assert len(awaiting) == 1, deliveries
