"""
Integration tests for multi-turn contextual policies with label state.

Verifies the realistic scenario where a policy monitors conversation state
(labels) and escalates based on accumulated context across turns:

- A condition-gated DENY policy that fires only when ``tainted: "1"`` is set.
- Label writes via PATCH persist across evaluations.
- The condition gate correctly skips when the label is at its initial value
  (``"0"``) and fires when updated to the matching value (``"1"``).
- Labels survive across multiple evaluation rounds (simulating multi-turn
  conversations).

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route-to-store
pipeline without subprocesses or real LLM calls.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Policy callables ───────────────────────────────────────


def _deny_all_tool_calls(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that unconditionally denies every tool call.

    Intended for use behind a condition gate so the DENY only fires
    when the gate's label condition matches. Without the gate, every
    tool call would be blocked.

    :param event: V0 event dict.
    :returns: DENY with a descriptive reason.
    """
    if event.get("type") != "tool_call":
        return {"result": "ALLOW"}
    return {
        "result": "DENY",
        "reason": "Conversation is tainted from a prior turn.",
    }


# ── Helpers ────────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    labels: dict[str, str] | None = None,
) -> str:
    """
    Create a session bound to an agent and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :param labels: Optional initial labels.
    :returns: New session id.
    """
    payload: dict[str, Any] = {"agent_id": agent_id}
    if labels is not None:
        payload["labels"] = labels
    resp = await client.post("/v1/sessions", json=payload)
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


def _tool_call_request(tool_name: str = "Bash") -> dict[str, Any]:
    """
    Build a PHASE_TOOL_CALL EvaluationRequest.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {"name": tool_name, "arguments": {}},
            "context": {},
        },
    }


# ── Tests ──────────────────────────────────────────────────


async def test_condition_gate_skips_when_label_at_initial_value(
    client: httpx.AsyncClient,
) -> None:
    """
    A condition-gated DENY policy is skipped when the label is at its
    initial value (``"0"``), which does not match the condition.

    This is turn 1 of the multi-turn scenario: the label ``tainted``
    starts at its declared ``initial: "0"`` value, so the condition
    ``tainted: "1"`` does not match and the policy is skipped entirely.
    The evaluate endpoint returns ALLOW.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "labels": {"tainted": {"values": ["0", "1"], "initial": "0"}},
            "policies": {
                "deny_when_tainted": {
                    "type": "function",
                    "condition": {"tainted": "1"},
                    "function": {
                        "path": f"{__name__}._deny_all_tool_calls",
                    },
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Bash"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_ALLOW", (
        f"Condition gate should skip when tainted=0; got {body}"
    )


async def test_condition_gate_fires_after_label_write(
    client: httpx.AsyncClient,
) -> None:
    """
    After labels are updated to match the condition, the gated DENY
    policy fires on the next evaluation.

    Simulates a multi-turn scenario:
    - Turn 1: ``tainted=0`` (default) → condition skips → ALLOW.
    - Label write: ``tainted=1`` via PATCH.
    - Turn 2: ``tainted=1`` → condition matches → DENY.

    This is the core IFC-through-labels pattern: a policy writes a
    label on one turn, and a downstream condition gate fires on the
    next.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "labels": {"tainted": {"values": ["0", "1"], "initial": "0"}},
            "policies": {
                "deny_when_tainted": {
                    "type": "function",
                    "condition": {"tainted": "1"},
                    "function": {
                        "path": f"{__name__}._deny_all_tool_calls",
                    },
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])

    # Turn 1: tainted=0 → ALLOW
    resp1 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Bash"),
    )
    assert resp1.status_code == 200
    assert resp1.json()["result"] == "POLICY_ACTION_ALLOW", "Turn 1 should ALLOW when tainted=0"

    # Simulate label write (as if a prior policy on a previous turn
    # wrote tainted=1 via set_labels).
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {"tainted": "1"}},
    )
    assert patch_resp.status_code == 200

    # Turn 2: tainted=1 → condition matches → DENY
    resp2 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Bash"),
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["result"] == "POLICY_ACTION_DENY", (
        f"Turn 2 should DENY when tainted=1; got {body2}"
    )
    assert "tainted" in body2.get("reason", "").lower(), (
        f"DENY reason should mention the taint; got {body2}"
    )


async def test_labels_persist_across_multiple_evaluations(
    client: httpx.AsyncClient,
) -> None:
    """
    Labels written via PATCH persist across multiple policy evaluations,
    verifiable through both GET /labels and repeated DENY verdicts.

    Turn 1: ALLOW (clean). Label write. Turn 2: DENY. Turn 3: still
    DENY (the label was not cleared). GET /labels confirms the persisted
    value between turns.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "labels": {"tainted": {"values": ["0", "1"], "initial": "0"}},
            "policies": {
                "deny_when_tainted": {
                    "type": "function",
                    "condition": {"tainted": "1"},
                    "function": {
                        "path": f"{__name__}._deny_all_tool_calls",
                    },
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])

    # Turn 1: ALLOW
    resp1 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Read"),
    )
    assert resp1.status_code == 200
    assert resp1.json()["result"] == "POLICY_ACTION_ALLOW"

    # Write the taint label
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"labels": {"tainted": "1"}},
    )
    assert patch_resp.status_code == 200

    # Verify label persistence via GET
    labels_resp = await client.get(f"/v1/sessions/{session_id}/labels")
    assert labels_resp.status_code == 200
    labels_data = labels_resp.json()
    assert labels_data["labels"]["tainted"] == "1", (
        f"Label not persisted after PATCH; got {labels_data['labels']}"
    )

    # Turn 2: DENY (tainted=1)
    resp2 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Bash"),
    )
    assert resp2.status_code == 200
    assert resp2.json()["result"] == "POLICY_ACTION_DENY", (
        "Turn 2 should DENY after taint label write"
    )

    # Turn 3: still DENY — label persists across evaluations
    resp3 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Bash"),
    )
    assert resp3.status_code == 200
    assert resp3.json()["result"] == "POLICY_ACTION_DENY", (
        "Turn 3 should still DENY — label was not cleared"
    )

    # Final label check: still tainted
    labels_resp2 = await client.get(f"/v1/sessions/{session_id}/labels")
    assert labels_resp2.status_code == 200
    assert labels_resp2.json()["labels"]["tainted"] == "1"


async def test_untainted_session_never_triggers_condition_gate(
    client: httpx.AsyncClient,
) -> None:
    """
    A session that never has its taint label set always passes the
    condition-gated policy — the gate never matches and every
    evaluation returns ALLOW.

    Regression guard: if the condition gate defaults to match-all
    instead of match-none, this test catches it.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "labels": {"tainted": {"values": ["0", "1"], "initial": "0"}},
            "policies": {
                "deny_when_tainted": {
                    "type": "function",
                    "condition": {"tainted": "1"},
                    "function": {
                        "path": f"{__name__}._deny_all_tool_calls",
                    },
                },
            },
        },
    )
    session_id = await _create_session(client, agent["id"])

    # Multiple evaluations — all ALLOW because tainted stays at "0"
    for tool in ("Bash", "Read", "Bash"):
        resp = await client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_call_request(tool),
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == "POLICY_ACTION_ALLOW", (
            f"Untainted session should always ALLOW; failed on tool={tool}"
        )
