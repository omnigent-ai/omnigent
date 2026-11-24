"""
Integration tests for DENY policies declared in agent YAML
targeting specific tools.

Verifies that ``guardrails.policies`` in the agent spec correctly
scope DENY decisions to named tools via the ``/policies/evaluate``
endpoint, and that REQUEST-phase DENY blocks all input before the
LLM is called.

Tests cover:

- DENY on a specific tool_call: agent has a policy scoped to
  ``tool_call:echo`` → evaluate returns DENY for ``echo``.
- DENY doesn't block other tools: the same agent allows a
  different tool (``grep``).
- DENY on request phase from YAML: agent has a REQUEST-phase
  DENY → any user input is blocked (LLM never called).
- A timer's wake meets the same REQUEST-phase policies: DENY, or an
  ASK no one can answer, refuses it before any runner is reached.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.server.scheduled import fire as fire_mod
from omnigent.server.scheduled.dispatch_scheduler import StartupLease
from omnigent.server.scheduled.fire import ClaimedRunExecutor, FireDeps
from omnigent.server.scheduled.schedule import next_scheduled_time, timer_rule
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.helpers import create_session_for_agent as _create_session
from tests.server.helpers import create_test_agent
from tests.server.helpers import policy_tool_call_request as _tool_call_request

pytestmark = pytest.mark.asyncio


POLICY_REGISTRY = [
    {
        "handler": "omnigent.policies.function.make_fixed_action_callable",
        "kind": "factory",
    }
]


@pytest.fixture(autouse=True)
def _register_test_policy_handlers() -> Iterator[None]:
    from omnigent.policies.registry import load_registry

    # HTTP uploads accept custom policies only when the server registers them.
    load_registry([__name__])
    yield
    load_registry()


_MAKE_FIXED = "omnigent.policies.function.make_fixed_action_callable"


# ── Helpers ─────────────────────────────────────────────────


# ── Tests ───────────────────────────────────────────────────


async def test_deny_on_specific_tool_call(
    client: httpx.AsyncClient,
) -> None:
    """
    Agent YAML with a DENY policy on ``tool_call:echo`` blocks
    the echo tool via the evaluate endpoint.

    The guardrails block declares a function policy whose callable
    inspects the tool name and returns DENY for ``echo``. The
    evaluate endpoint must compose the agent's policies into the
    engine and return ``POLICY_ACTION_DENY`` with the correct
    reason.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "deny_echo": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {
                        "path": _MAKE_FIXED,
                        "arguments": {
                            "action": "deny",
                            "reason": "echo tool is blocked by YAML policy.",
                            "on_phases": ["tool_call"],
                            "on_tools": ["echo"],
                        },
                    },
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("echo"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_DENY", (
        f"Expected DENY for echo tool call; got {body['result']}. "
        "The YAML-declared policy did not fire."
    )
    assert body["reason"] == "echo tool is blocked by YAML policy.", (
        f"Unexpected reason: {body.get('reason')!r}"
    )


async def test_deny_does_not_block_other_tools(
    client: httpx.AsyncClient,
) -> None:
    """
    The same agent with a DENY policy on ``echo`` allows other
    tools through.

    A tool call to ``grep`` must return ALLOW because the policy
    is scoped to ``on_tools: ["echo"]``. If the policy over-fires,
    this test catches it.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "deny_echo": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {
                        "path": _MAKE_FIXED,
                        "arguments": {
                            "action": "deny",
                            "reason": "echo tool is blocked by YAML policy.",
                            "on_phases": ["tool_call"],
                            "on_tools": ["echo"],
                        },
                    },
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("grep"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_ALLOW", (
        f"Expected ALLOW for grep tool call; got {body['result']}. "
        "The DENY policy for echo is over-firing on unrelated tools."
    )


async def test_deny_on_request_phase_blocks_input(
    client: httpx.AsyncClient,
) -> None:
    """
    Agent YAML with a REQUEST-phase DENY blocks all user input.

    The evaluate endpoint receives a PHASE_TOOL_CALL event, but the
    policy only fires on ``request`` events. A separate request-phase
    evaluation must return DENY. This proves that REQUEST-phase
    policies in the YAML are wired and can block input before the
    LLM is ever called.

    Note: the evaluate endpoint dispatches by event type, so we send
    a ``PHASE_TOOL_CALL`` to verify the request policy does NOT fire
    on tool calls (ALLOW), and then verify the request-phase policy
    fires on actual request-phase events through the session events
    endpoint.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "deny_all_input": {
                    "type": "function",
                    "on": ["request"],
                    "function": {
                        "path": _MAKE_FIXED,
                        "arguments": {
                            "action": "deny",
                            "reason": "All input is blocked by request-phase policy.",
                            "on_phases": ["request"],
                        },
                    },
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])

    # The request-phase policy does not fire on TOOL_CALL events.
    resp_tc = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("echo"),
    )
    assert resp_tc.status_code == 200, resp_tc.text
    assert resp_tc.json()["result"] == "POLICY_ACTION_ALLOW", (
        "Request-phase policy should not fire on PHASE_TOOL_CALL events."
    )

    # Send user message via session events — the REQUEST-phase policy
    # must intercept and deny synchronously.
    resp_msg = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello"}],
            },
        },
    )
    assert resp_msg.status_code == 202, (
        f"unexpected status: {resp_msg.status_code} {resp_msg.text[:300]}"
    )
    verdict = resp_msg.json()
    assert verdict.get("denied") is True, (
        f"Expected synchronous DENY from request-phase policy; got {verdict}"
    )
    assert "blocked by request-phase policy" in verdict.get("reason", ""), (
        f"Unexpected reason: {verdict.get('reason')!r}"
    )


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ("allow", None),
        ("deny", "All input is blocked by request-phase policy."),
        ("ask", "Every message needs approval."),
        # A policy that can't be checked refuses the wake, even one that would allow it.
        ("error", "policy evaluation error: RuntimeError"),
    ],
)
async def test_request_phase_policies_check_a_timers_wake(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    reason: str | None,
) -> None:
    """A timer's wake meets the session's REQUEST-phase policies before reaching a runner."""
    from omnigent.server.routes import sessions

    policy_action = "allow" if action == "error" else action
    arguments: dict[str, object] = {"action": policy_action, "on_phases": ["request"]}
    if reason is not None and action != "error":
        arguments["reason"] = reason
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "check_input": {
                    "type": "function",
                    "on": ["request"],
                    "function": {"path": _MAKE_FIXED, "arguments": arguments},
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])
    # The runner side of the wake, recorded instead of reached over a tunnel.
    connect = AsyncMock(side_effect=lambda **kwargs: (object(), kwargs["conv"]))
    send = AsyncMock(
        return_value=sessions._SessionEventDispatchResult(
            item_id=None, pending_id=None, accepted=True
        )
    )
    monkeypatch.setattr(sessions, "ensure_runner_connected", connect)
    monkeypatch.setattr(sessions, "_ensure_runner_session_initialized", AsyncMock())
    monkeypatch.setattr(sessions, "_dispatch_session_event_to_runner", send)
    monkeypatch.setattr(fire_mod, "_agent_revision", AsyncMock(return_value=None))
    if action == "error":
        evaluation_error = AsyncMock(side_effect=RuntimeError("evaluator unavailable"))
        monkeypatch.setattr(sessions, "_evaluate_input_policy", evaluation_error)

    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    now = int(time.time())
    timer = store.create(
        uuid.uuid4().hex,
        "Timer",
        "[System: timer fired]",
        timer_rule(30, repeat=True),
        None,
        agent["id"],
        "UTC",
        session_mode="continue",
        session_id=session_id,
        first_run_at=now,
        kind="timer",
        schedule_anchor=now - 30,
    )
    run = store.claim_scheduled_run(
        timer,
        now=now,
        next_run_at=next_scheduled_time(timer, now),
        host_id=None,
        lease_seconds=120,
    )
    assert run is not None
    deps = FireDeps(
        scheduled_task_store=store,
        agent_store=SqlAlchemyAgentStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        permission_store=None,
        host_store=None,
        host_registry=None,
    )
    await ClaimedRunExecutor(deps).fire(
        timer, StartupLease(store, run, now=time.time, lease_seconds=120)
    )

    [finished] = store.list_runs(timer.id)[0]
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    denials = [item for item in items if "[Denied by policy:" in str(item)]
    if action == "allow":
        assert send.await_count == 1
        assert (finished.status, finished.error_code) == ("succeeded", None)
        assert denials == []
        return
    # Refused before any runner is woken; an ASK can't wait for an approval here.
    connect.assert_not_awaited()
    send.assert_not_awaited()
    assert (finished.status, finished.error_code) == ("skipped", "timer_policy_denied")
    assert reason is not None and reason in (finished.error or "")
    assert ("no one can approve this wake" in (finished.error or "")) is (action == "ask")
    assert len(denials) == 1 and reason in str(denials[0])
