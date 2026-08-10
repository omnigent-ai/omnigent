"""True end-to-end reconnect-redelivery test for OMN-104 (cross-vendor review BLOCKING #3).

``test_manager_webhook_decision_endpoint.py``'s case (b) test only proves the
QUERY side of ``_on_runner_connect``'s reconnect-redelivery loop
(``session_lifecycle_store.list_decided_undelivered`` returns the row the
hook would redeliver) — it never drives an actual WebSocket tunnel
disconnect/reconnect, never exercises the real
``omnigent.runner.pending_approvals`` registry, and never observes a real
``session.resumed`` outbox row produced by a genuine runner acknowledgement.
This file closes that gap using the same real WS-tunnel machinery as
``test_sessions_tunnel_three_layer.py`` (``tunnel_three_layer_stack``,
``_connect_runner_tunnel``, ``_send_hello_and_wait``,
``_forward_requests_to_runner``) — no mocking library, no fake runner stub.

Unlike ``test_on_runner_connect_restarts_relay_via_router`` (which stubs the
router resolver and ``_ensure_runner_relay`` so it can assert on hook
plumbing in isolation), these tests deliberately do NOT stub anything on the
redelivery path: the reconnect-redelivery POST must reach the REAL runner
app's ``/v1/sessions/{id}/events`` handler so ``pending_approvals.resolve()``
runs for real.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.runner import pending_approvals
from omnigent.runner.app import create_runner_app
from omnigent.server import session_outbox
from tests.runner.helpers import NullServerClient
from tests.server.integration.test_sessions_tunnel_three_layer import (
    _RUNNER_ID,
    _TEST_HARNESS_NAME,
    _build_harness_agent_bundle,
    _connect_runner_tunnel,
    _forward_requests_to_runner,
    _send_hello_and_wait,
    _TunnelStack,
    tunnel_three_layer_stack,  # noqa: F401 — re-exported fixture
)

pytestmark = pytest.mark.asyncio


async def _create_bound_session(stack: _TunnelStack) -> str:
    """Create a session and bind it to ``_RUNNER_ID`` via a real PATCH."""
    create_resp = await stack.ap_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _build_harness_agent_bundle(),
                "application/gzip",
            ),
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["session_id"]

    bind_resp = await stack.ap_client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": _RUNNER_ID},
    )
    assert bind_resp.status_code == 200, bind_resp.text
    return session_id


async def _drop_and_reconnect_tunnel(
    stack: _TunnelStack,
) -> tuple[ApplicationCommunicator, asyncio.Task[None]]:
    """Deregister ``_RUNNER_ID``'s tunnel and reconnect it for real.

    This is the production trigger for ``_on_runner_connect`` — the same
    choreography ``test_on_runner_connect_restarts_relay_via_router`` uses,
    minus the resolver/relay stubs, so the redelivery loop's HTTP POST
    actually lands on the fresh runner app spun up here.

    :returns: The new communicator and its forwarder task, both owned by
        the caller for teardown.
    """
    from omnigent.server.routes.sessions import _runner_relay_tasks

    ap_app = stack.ap_app
    fake_pm = stack.fake_pm

    ap_app.state.tunnel_registry.deregister(_RUNNER_ID)

    async def _relays_cleared() -> None:
        while any(not h.task.done() for h in _runner_relay_tasks.values()):
            await asyncio.sleep(0.01)

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_relays_cleared(), timeout=2.0)

    new_communicator = await _connect_runner_tunnel(ap_app, _RUNNER_ID)
    await _send_hello_and_wait(
        new_communicator,
        ap_app,
        _RUNNER_ID,
        harnesses=[_TEST_HARNESS_NAME],
    )

    async def _resolve_spec(agent_id: str) -> Any:
        del agent_id
        return None

    new_runner_app = create_runner_app(
        process_manager=fake_pm,  # type: ignore[arg-type]
        spec_resolver=_resolve_spec,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    new_forwarder_task = asyncio.create_task(
        _forward_requests_to_runner(new_communicator, new_runner_app),
        name="tunnel-redelivery-e2e-forwarder",
    )
    return new_communicator, new_forwarder_task


async def _teardown_reconnect(
    communicator: ApplicationCommunicator | None, forwarder_task: asyncio.Task[None] | None
) -> None:
    if forwarder_task is not None:
        forwarder_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await forwarder_task
    if communicator is not None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await communicator.wait(timeout=2.0)


async def _session_resumed_rows(app: FastAPI, session_id: str) -> list[Any]:
    store = app.state.session_lifecycle_store
    deliveries, _ = store.list_deliveries(session_id, limit=100)
    return [d for d in deliveries if d.event_type == "session.resumed"]


async def test_reconnect_redelivers_policy_ask_and_fires_resumed(
    tunnel_three_layer_stack: _TunnelStack,  # noqa: F811
) -> None:
    """
    Policy/cost-ASK shape: a real ``pending_approvals`` Future registered on
    the runner is genuinely resolved by the reconnect-redelivery POST, and
    the durable ledger observes a real ``session.resumed``.

    No harness turn is in flight for this session (no message was ever
    sent), so the SAME redelivery event's unconditional harness-subprocess
    forward will 404 (``_scaffold.py``'s ``_resolve_elicitation`` finds no
    matching ``_in_flight`` entry) — that is the expected, real-world shape
    of a policy/cost ASK, not a test artifact. The
    ``X-Omnigent-Pending-Approval-Resolved: true`` header alone must be
    sufficient (the fix this test exists to prove).
    """
    stack = tunnel_three_layer_stack
    ap_app = stack.ap_app
    session_id = await _create_bound_session(stack)
    elicitation_id = "elicit_e2e_policy_ask"

    future = pending_approvals.register(elicitation_id)
    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )
    session_outbox.record_decision(
        elicitation_id=elicitation_id,
        decision={"action": "accept"},
        decided_by=None,
    )

    store = ap_app.state.session_lifecycle_store
    assert [e.id for e in store.list_decided_undelivered(session_id)] == [elicitation_id]

    communicator: ApplicationCommunicator | None = None
    forwarder_task: asyncio.Task[None] | None = None
    try:
        communicator, forwarder_task = await _drop_and_reconnect_tunnel(stack)

        approved = await asyncio.wait_for(future, timeout=5.0)
        assert approved is True, "real pending_approvals Future did not resolve to accept"

        resumed: list[Any] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            resumed = await _session_resumed_rows(ap_app, session_id)
            if resumed:
                break
            await asyncio.sleep(0.05)
        assert resumed, "session.resumed did not fire after real reconnect redelivery"

        elicitation = store.get_elicitation(elicitation_id)
        assert elicitation is not None
        assert elicitation.status == "delivered_to_runner"
        assert elicitation.resolved_at is not None
    finally:
        await _teardown_reconnect(communicator, forwarder_task)
        pending_approvals.reset_for_tests()


async def test_reconnect_does_not_resume_untracked_elicitation(
    tunnel_three_layer_stack: _TunnelStack,  # noqa: F811
) -> None:
    """
    Counterpart: an elicitation_id tracked by NEITHER registry (no
    ``pending_approvals`` Future registered, no harness turn in flight) must
    not fire ``session.resumed`` on reconnect — the real runner's
    unconditional harness-forward genuinely 404s and
    ``pending_approvals.resolve()`` genuinely returns ``False``, so neither
    truthful-consumption signal is present.
    """
    stack = tunnel_three_layer_stack
    ap_app = stack.ap_app
    session_id = await _create_bound_session(stack)
    elicitation_id = "elicit_e2e_untracked"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )
    session_outbox.record_decision(
        elicitation_id=elicitation_id,
        decision={"action": "accept"},
        decided_by=None,
    )

    store = ap_app.state.session_lifecycle_store
    assert [e.id for e in store.list_decided_undelivered(session_id)] == [elicitation_id]

    communicator: ApplicationCommunicator | None = None
    forwarder_task: asyncio.Task[None] | None = None
    try:
        communicator, forwarder_task = await _drop_and_reconnect_tunnel(stack)

        # Give the reconnect hook a real window to (incorrectly) resume —
        # a fixed sleep is appropriate here since we're proving a negative
        # (nothing happens), not racing a positive signal.
        await asyncio.sleep(1.0)

        resumed = await _session_resumed_rows(ap_app, session_id)
        assert resumed == [], (
            "session.resumed fired for an elicitation truthfully consumed by nobody"
        )

        elicitation = store.get_elicitation(elicitation_id)
        assert elicitation is not None
        assert elicitation.status == "decided"
    finally:
        await _teardown_reconnect(communicator, forwarder_task)
        pending_approvals.reset_for_tests()
