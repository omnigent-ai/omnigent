"""
End-to-end integration test for the "cost-aware development" user
journey: session with cost control, multi-turn spend, ASK at soft
limit, approve, DENY at hard limit.

Uses the shared ``client`` fixture (real stores + mock LLM) and drives
the full budget lifecycle through the ``POST /v1/sessions/{id}/policies/evaluate``
endpoint, proving the cost_budget policy's ASK/DENY thresholds fire
correctly as accumulated spend grows.

Tests:

- ``test_cost_budget_ask_then_deny_lifecycle``: below threshold ALLOW,
  at soft threshold ASK, above hard limit DENY.
- ``test_cost_control_toggle_independent_of_policy_evaluation``:
  policy evaluation still returns DENY after toggling
  cost_control_mode_override to "off", because the toggle gates the
  runner-side cost advisor, not the server-side policy engine.
  Re-enabling with "on" round-trips the persisted value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import httpx
import pytest

from omnigent.runtime import session_stream
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Helpers ────────────────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    cost_control_mode_override: str | None = None,
) -> str:
    """
    Create a session bound to an agent and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :param cost_control_mode_override: Optional cost control switch,
        e.g. ``"on"`` or ``"off"``.
    :returns: New session id.
    """
    body: dict[str, Any] = {"agent_id": agent_id}
    if cost_control_mode_override is not None:
        body["cost_control_mode_override"] = cost_control_mode_override
    resp = await client.post("/v1/sessions", json=body)
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


def _tool_call_request(
    tool_name: str = "Bash",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a PHASE_TOOL_CALL EvaluationRequest.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :param arguments: Tool arguments dict.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {
                "name": tool_name,
                "arguments": arguments or {},
            },
            "context": {},
        },
    }


async def _evaluate(
    client: httpx.AsyncClient,
    session_id: str,
    tool_name: str = "Bash",
) -> dict[str, Any]:
    """
    Evaluate the policy engine for a tool call and return the response body.

    :param client: Test HTTP client.
    :param session_id: Session to evaluate against.
    :param tool_name: Tool name for the tool call event.
    :returns: EvaluationResponse JSON body.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request(tool_name),
    )
    assert resp.status_code == 200, f"evaluate failed: {resp.status_code} {resp.text}"
    return resp.json()


async def _drain_elicitation_id(
    session_id: str,
    *,
    subscribed: asyncio.Event | None = None,
    timeout_s: float = 5.0,
) -> str:
    """
    Block on the session SSE stream until a
    ``response.elicitation_request`` arrives; return its id.

    :param session_id: Session to subscribe to.
    :param subscribed: When provided, this event is set as soon as
        the SSE subscriber slot is registered (via the
        ``on_subscribed`` hook of :func:`session_stream.subscribe`).
        Callers can ``await subscribed.wait()`` before triggering the
        action that publishes the elicitation, guaranteeing no event
        is lost without relying on a sleep.
    :param timeout_s: Max seconds to wait before failing the test.
    :returns: The published ``elicitation_id``.
    """

    async def _signal_subscribed() -> Iterable[dict[str, Any]]:
        """``on_subscribed`` hook: fires after the slot is registered."""
        if subscribed is not None:
            subscribed.set()
        return ()

    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(
            session_id,
            on_subscribed=_signal_subscribed,
        ):
            if event.get("type") == "response.elicitation_request":
                eid = event.get("elicitation_id")
                assert isinstance(eid, str) and eid, f"missing id: {event!r}"
                return eid
    raise AssertionError("subscribe loop ended without an elicitation event")


# ── Tests ──────────────────────────────────────────────────────────


async def test_cost_budget_ask_then_deny_lifecycle(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Full budget lifecycle: ALLOW → ASK (approve) → DENY at hard limit.

    Creates a session with a cost_budget policy configured with low
    thresholds (ask at $0.01, deny at $0.05). Seeds the session's
    cumulative spend at increasing levels and evaluates the policy,
    verifying:

    1. Below the soft threshold → ALLOW (no gate fires).
    2. At the soft threshold → ASK (the server-side gate parks for
       approval; the test accepts via the elicitation resolve endpoint,
       collapsing to ALLOW).
    3. Above the hard limit on an expensive model → DENY (the
       downgrade gate blocks the tool call).
    """
    store = SqlAlchemyConversationStore(db_uri)

    # Agent with a cost_budget policy: ask at $0.01, deny at $0.05.
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "session_cost_guard": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.policies.builtins.cost.cost_budget",
                        "arguments": {
                            "max_cost_usd": 0.05,
                            "ask_thresholds_usd": [0.01],
                            # The test agent's model is "test-agent" (from
                            # the bundle); include it in the expensive set
                            # so the hard DENY gate fires over budget.
                            "expensive_models": ["test-agent"],
                        },
                    },
                }
            }
        },
    )
    session_id = await _create_session(client, agent["id"])

    # ── Step 1: below soft threshold → ALLOW ──────────────────────
    store.set_session_usage(session_id, {"total_cost_usd": 0.005})
    result = await _evaluate(client, session_id)
    assert result["result"] == "POLICY_ACTION_ALLOW", (
        f"Spend $0.005 (below $0.01 ask threshold) should ALLOW, got {result['result']}"
    )

    # ── Step 2: at soft threshold → ASK → approve → ALLOW ────────
    store.set_session_usage(session_id, {"total_cost_usd": 0.013})

    # The evaluate POST parks until the verdict arrives — run it
    # concurrently and learn the elicitation id from the stream.
    # Use an asyncio.Event so the drain task can signal when its
    # subscriber slot is registered, replacing the old sleep(0.05).
    sub_ready = asyncio.Event()
    drain = asyncio.create_task(
        _drain_elicitation_id(session_id, subscribed=sub_ready),
    )
    await sub_ready.wait()
    evaluate_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_call_request("Bash"),
        )
    )

    elicitation_id = await drain
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text

    ask_resp = await evaluate_task
    assert ask_resp.status_code == 200, ask_resp.text
    ask_body = ask_resp.json()
    assert ask_body["result"] == "POLICY_ACTION_ALLOW", (
        f"Accepted ASK at $0.013 should collapse to ALLOW, got {ask_body['result']}"
    )

    # ── Step 3: above hard limit on expensive model → DENY ────────
    store.set_session_usage(session_id, {"total_cost_usd": 0.06})
    result = await _evaluate(client, session_id)
    assert result["result"] == "POLICY_ACTION_DENY", (
        f"Spend $0.06 (above $0.05 hard limit) should DENY, got {result['result']}"
    )
    assert "reason" in result, "DENY response must include a reason"
    assert "0.06" in result["reason"], (
        f"DENY reason should mention the current cost $0.06, got: {result['reason']}"
    )

    # ── Verify session is still accessible ──────────────────────────
    get_resp = await client.get(f"/v1/sessions/{session_id}")
    assert get_resp.status_code == 200


async def test_cost_control_toggle_independent_of_policy_evaluation(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Policy evaluation still returns DENY after toggling cost control OFF.

    The cost_control_mode_override is a session-level switch consumed by
    the runner-side cost advisor pipeline (which injects the cost plan
    into the runner), **not** the server-side policy engine. This test
    verifies:

    1. Create session with a cost_budget policy and seed spend above
       the hard limit → evaluate returns DENY.
    2. Toggle cost_control_mode_override to "off" via PATCH →
       policy evaluation **still** returns DENY (the toggle does not
       suppress the policy engine).
    3. Verify the session snapshot reflects the toggle value.
    4. Toggle back to "on" and verify the round-trip persists.
    """
    store = SqlAlchemyConversationStore(db_uri)

    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "session_cost_guard": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.policies.builtins.cost.cost_budget",
                        "arguments": {
                            "max_cost_usd": 0.05,
                            "ask_thresholds_usd": [0.01],
                            # The test agent's model is "test-agent"; include
                            # it in the expensive set so the hard DENY fires.
                            "expensive_models": ["test-agent"],
                        },
                    },
                }
            }
        },
    )
    session_id = await _create_session(client, agent["id"])

    # ── Step 1: seed over-budget spend → DENY ─────────────────────
    store.set_session_usage(session_id, {"total_cost_usd": 0.06})
    result = await _evaluate(client, session_id)
    assert result["result"] == "POLICY_ACTION_DENY", (
        f"Over-budget spend should DENY before toggle, got {result['result']}"
    )

    # ── Step 2: toggle cost control OFF ───────────────────────────
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"cost_control_mode_override": "off"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["cost_control_mode_override"] == "off"

    # ── Step 3: verify the snapshot reflects the toggle ───────────
    get_resp = await client.get(f"/v1/sessions/{session_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["cost_control_mode_override"] == "off", (
        "Session snapshot should reflect cost_control_mode_override = 'off'"
    )

    # The policy evaluate endpoint still runs spec-declared policies —
    # the cost_budget policy fires based on accumulated spend and model,
    # not the toggle. The toggle gates the runner-side cost advisor.
    result_after_toggle = await _evaluate(client, session_id)
    assert result_after_toggle["result"] == "POLICY_ACTION_DENY", (
        "cost_budget policy evaluates independently of the cost_control toggle "
        f"(still over budget), got {result_after_toggle['result']}"
    )

    # ── Step 4: toggle back to ON and verify round-trip ───────────
    patch_on = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"cost_control_mode_override": "on"},
    )
    assert patch_on.status_code == 200, patch_on.text
    assert patch_on.json()["cost_control_mode_override"] == "on"

    get_on = await client.get(f"/v1/sessions/{session_id}")
    assert get_on.status_code == 200
    assert get_on.json()["cost_control_mode_override"] == "on"
