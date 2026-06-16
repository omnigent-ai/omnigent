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

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Policy callables ────────────────────────────────────────


def _deny_echo_tool(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that denies ``echo`` tool calls.

    :param event: V0 event dict.
    :returns: DENY for echo tool calls, ALLOW otherwise.
    """
    if event.get("type") != "tool_call":
        return {"result": "ALLOW"}
    data = event.get("data")
    tool = data.get("name", "") if isinstance(data, dict) else ""
    if tool == "echo":
        return {
            "result": "DENY",
            "reason": "echo tool is blocked by YAML policy.",
        }
    return {"result": "ALLOW"}


def _deny_all_requests(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that denies all request-phase events.

    :param event: V0 event dict.
    :returns: DENY for request events, ALLOW otherwise.
    """
    if event.get("type") == "request":
        return {
            "result": "DENY",
            "reason": "All input is blocked by request-phase policy.",
        }
    return {"result": "ALLOW"}


# ── Helpers ─────────────────────────────────────────────────


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a session bound to an agent.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


def _tool_call_request(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a PHASE_TOOL_CALL EvaluationRequest.

    :param tool_name: Tool name, e.g. ``"echo"``.
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
                    "function": {
                        "path": f"{__name__}._deny_echo_tool",
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
    callable only inspects ``echo``. If the policy over-fires, this
    test catches it.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "deny_echo": {
                    "type": "function",
                    "function": {
                        "path": f"{__name__}._deny_echo_tool",
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
                    "function": {
                        "path": f"{__name__}._deny_all_requests",
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
