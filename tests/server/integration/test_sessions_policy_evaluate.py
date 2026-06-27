"""
Integration tests for ``POST /v1/sessions/{id}/policies/evaluate``.

The endpoint receives proto-compatible ``EvaluationRequest`` JSON,
evaluates policies via the policy engine, and returns an
``EvaluationResponse`` with the verdict. Used by Claude Code's
``PreToolUse`` / ``PostToolUse`` command hooks to enforce admin
policies on native tools.

Tests cover:

- TOOL_CALL ALLOW: no matching policy → ``POLICY_ACTION_ALLOW``.
- TOOL_CALL DENY: ``default_policies`` deny → ``POLICY_ACTION_DENY``
  with reason.
- TOOL_RESULT DENY: tool result policy fires.
- Missing session → 404.
- Malformed body → 400.
- Unknown event type → 400.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runtime import get_caps, session_stream
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.routes import sessions as sessions_routes
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import CapturingRunnerClient, create_test_agent

pytestmark = pytest.mark.asyncio


# ── Policy callables ────────────────────────────────────────


def _deny_bash_tool(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that denies Bash tool calls.

    :param event: V0 event dict.
    :returns: DENY for Bash tool calls, ALLOW otherwise.
    """
    if event.get("type") != "tool_call":
        return {"result": "ALLOW"}
    data = event.get("data")
    tool = data.get("name", "") if isinstance(data, dict) else ""
    if tool == "Bash":
        return {
            "result": "DENY",
            "reason": "Bash is blocked by admin policy.",
        }
    return {"result": "ALLOW"}


def _deny_sensitive_output(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that denies tool results containing ``SECRET``.

    :param event: V0 event dict.
    :returns: DENY if tool result contains SECRET, ALLOW otherwise.
    """
    if event.get("type") != "tool_result":
        return {"result": "ALLOW"}
    data = event.get("data")
    result = data.get("result", "") if isinstance(data, dict) else str(data)
    if isinstance(result, str) and "SECRET" in result:
        return {
            "result": "DENY",
            "reason": "Output contains sensitive data.",
        }
    return {"result": "ALLOW"}


def _deny_large_llm_request(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that denies LLM requests with more than 100 messages.

    :param event: V0 event dict.
    :returns: DENY for large requests, ALLOW otherwise.
    """
    if event.get("type") != "llm_request":
        return {"result": "ALLOW"}
    data = event.get("data")
    count = data.get("messages_count", 0) if isinstance(data, dict) else 0
    if isinstance(count, int) and count > 100:
        return {
            "result": "DENY",
            "reason": f"LLM request too large: {count} messages",
        }
    return {"result": "ALLOW"}


def _deny_llm_response_with_pii(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that denies LLM responses containing ``SSN``.

    :param event: V0 event dict.
    :returns: DENY if response text contains SSN, ALLOW otherwise.
    """
    if event.get("type") != "llm_response":
        return {"result": "ALLOW"}
    data = event.get("data")
    text = data.get("text_preview", "") if isinstance(data, dict) else ""
    if isinstance(text, str) and "SSN" in text:
        return {
            "result": "DENY",
            "reason": "LLM response contains PII.",
        }
    return {"result": "ALLOW"}


def _deny_blocked_actor(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that denies if ``run_as`` is ``blocked@test.com``.

    :param event: V0 event dict.
    :returns: DENY when actor matches, ALLOW otherwise.
    """
    actor = event.get("context", {}).get("actor", {})
    if actor.get("run_as") == "blocked@test.com":
        return {"result": "DENY", "reason": "Blocked user"}
    return {"result": "ALLOW"}


def _ask_for_bash(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that requires human approval (ASK) for Bash tool calls.

    :param event: V0 event dict.
    :returns: ASK for Bash tool calls, ALLOW otherwise.
    """
    if event.get("type") != "tool_call":
        return {"result": "ALLOW"}
    data = event.get("data")
    tool = data.get("name", "") if isinstance(data, dict) else ""
    if tool == "Bash":
        return {"result": "ASK", "reason": "Approve running Bash?"}
    return {"result": "ALLOW"}


def _ask_for_sensitive_output(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that requires human approval (ASK) for tool results with ``REVIEW``.

    Exercises the TOOL_RESULT ASK gate: pi-native can intercept a result before
    the model consumes it, so a result-phase ASK must park server-side and
    collapse to ALLOW / DENY (return / suppress the result) — never a raw ASK.

    :param event: V0 event dict.
    :returns: ASK for tool results containing REVIEW, ALLOW otherwise.
    """
    if event.get("type") != "tool_result":
        return {"result": "ALLOW"}
    data = event.get("data")
    result = data.get("result", "") if isinstance(data, dict) else str(data)
    if isinstance(result, str) and "REVIEW" in result:
        return {"result": "ASK", "reason": "Approve releasing this tool result?"}
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


async def _create_pi_native_agent(client: httpx.AsyncClient) -> dict[str, Any]:
    """
    Create a pi-native test agent.

    The TOOL_RESULT ASK gate parks only for the pi-native harness (whose
    ``tool_result`` extension hook can intercept the result before the model
    consumes it). The default ``create_test_agent`` harness is ``claude-sdk``,
    which would NOT park a result-phase ASK, so result-gate tests must bind a
    pi-native agent.

    :param client: Test HTTP client.
    :returns: Parsed agent response body.
    """
    return await create_test_agent(
        client,
        executor={"type": "omnigent", "config": {"harness": "pi-native"}},
    )


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


def _tool_result_request(
    result: str = "",
    tool_name: str = "Bash",
) -> dict[str, Any]:
    """
    Build a PHASE_TOOL_RESULT EvaluationRequest.

    :param result: Tool result string.
    :param tool_name: Original tool name for request_data.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_RESULT",
            "target": "",
            "data": {"result": result},
            "context": {},
            "request_data": {"name": tool_name, "arguments": {}},
        },
    }


def _llm_request_payload(
    model: str = "gpt-4o",
    messages_count: int = 10,
) -> dict[str, Any]:
    """
    Build a PHASE_LLM_REQUEST EvaluationRequest.

    :param model: Model name for the LLM call.
    :param messages_count: Number of messages in the prompt.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_LLM_REQUEST",
            "data": {
                "model": model,
                "messages_count": messages_count,
                "tools_count": 5,
                "system_prompt_preview": "You are a helpful assistant.",
            },
            "context": {},
        },
    }


def _llm_response_payload(
    text_preview: str = "Hello!",
    tool_calls_count: int = 0,
) -> dict[str, Any]:
    """
    Build a PHASE_LLM_RESPONSE EvaluationRequest.

    :param text_preview: Preview of the LLM response text.
    :param tool_calls_count: Number of tool calls in the response.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_LLM_RESPONSE",
            "data": {
                "model": "gpt-4o",
                "text_preview": text_preview,
                "tool_calls_count": tool_calls_count,
            },
            "context": {},
        },
    }


# ── Tests ───────────────────────────────────────────────────


async def test_tool_call_allow_when_no_matching_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A tool call with no matching policy returns ALLOW.

    The agent has no guardrails and no default_policies are configured,
    so the engine returns ALLOW for any tool call. If the endpoint
    crashed or returned the wrong action, this would fail.
    """
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Read"),
    )
    assert resp.status_code == 200
    body = resp.json()
    # No policies → ALLOW (the default engine result).
    assert body["result"] == "POLICY_ACTION_ALLOW"
    assert "reason" not in body


async def test_tool_call_deny_with_default_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A default_policy that denies Bash returns DENY with reason.

    This exercises the full path: route handler → get_conversation →
    agent lookup → build_policy_engine(default_policies=...) → evaluate
    → DENY response. If any link in this chain breaks (e.g. the
    conversation_store.get() bug), this test fails.
    """
    deny_bash_policy = FunctionPolicySpec(
        name="admin__deny_bash",
        on=None,
        function=FunctionRef(path=f"{__name__}._deny_bash_tool"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[deny_bash_policy],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )

    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Bash → DENY by the admin policy.
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Bash"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_DENY"
    assert body["reason"] == "Bash is blocked by admin policy."

    # Read → ALLOW (the policy only denies Bash).
    resp2 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Read"),
    )
    assert resp2.status_code == 200
    assert resp2.json()["result"] == "POLICY_ACTION_ALLOW"


async def test_tool_result_deny_with_default_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A TOOL_RESULT phase policy that denies sensitive output returns DENY.

    Verifies the endpoint correctly handles PHASE_TOOL_RESULT events
    and passes ``request_data`` through to the engine context.
    """
    deny_sensitive = FunctionPolicySpec(
        name="admin__deny_sensitive_output",
        on=None,
        function=FunctionRef(path=f"{__name__}._deny_sensitive_output"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[deny_sensitive],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )

    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Tool result with SECRET → DENY.
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_result_request("output contains SECRET data"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_DENY"
    assert body["reason"] == "Output contains sensitive data."

    # Clean tool result → ALLOW.
    resp2 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_result_request("normal output"),
    )
    assert resp2.status_code == 200
    assert resp2.json()["result"] == "POLICY_ACTION_ALLOW"


async def test_missing_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """
    Evaluating a policy against a non-existent session returns 404.

    If the endpoint silently defaulted to ALLOW on missing sessions,
    an attacker could bypass policies by guessing session ids.
    """
    resp = await client.post(
        "/v1/sessions/nonexistent_session_id/policies/evaluate",
        json=_tool_call_request(),
    )
    assert resp.status_code == 404


async def test_malformed_body_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """
    A malformed body (missing ``event``) returns 400.

    If the endpoint silently accepted bad input, policy bypasses
    could go undetected.
    """
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Missing event field entirely.
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={"not_event": {}},
    )
    assert resp.status_code == 400

    # Unknown event type.
    resp2 = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={"event": {"type": "PHASE_UNKNOWN", "data": {}}},
    )
    assert resp2.status_code == 400


async def test_unknown_event_type_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """
    An unknown event type returns 400.

    Only PHASE_TOOL_CALL, PHASE_TOOL_RESULT, PHASE_LLM_REQUEST, and
    PHASE_LLM_RESPONSE are recognized. Anything else is a client error.
    """
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={"event": {"type": "PHASE_INVALID", "data": {}}},
    )
    assert resp.status_code == 400


# ── Actor wiring tests ────────────────────────────────────────


def test_build_actor_with_user_id() -> None:
    """
    ``_build_actor`` returns ``{"run_as": user_id}`` when a user is
    authenticated. Without this, ``event.context.actor`` is always empty
    and policies that gate on identity are blind.
    """
    from omnigent.server.routes.sessions import _build_actor

    assert _build_actor("alice@example.com") == {"run_as": "alice@example.com"}


def test_build_actor_without_user_id() -> None:
    """
    ``_build_actor`` returns ``None`` when no user is authenticated (tests,
    legacy callers). Policies should see an empty actor dict.
    """
    from omnigent.server.routes.sessions import _build_actor

    assert _build_actor(None) is None


async def test_evaluate_endpoint_passes_actor_to_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The evaluate endpoint threads the authenticated user's identity into
    ``event.context.actor`` so policy callables can inspect it.

    A policy that inspects ``event["context"]["actor"]["run_as"]``
    and denies if the user is ``"blocked@test.com"`` verifies the full
    wiring from HTTP request → ``_build_actor`` → ``EvaluationContext``
    → ``FunctionPolicy`` event dict.
    """
    policy = FunctionPolicySpec(
        name="admin__deny_blocked_actor",
        on=None,
        function=FunctionRef(path=f"{__name__}._deny_blocked_actor"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[policy],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )
    # Patch _get_user_id to simulate an authenticated user.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_user_id",
        lambda _req, _auth: "blocked@test.com",
    )

    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Read"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_DENY"
    assert body["reason"] == "Blocked user"


# ── Native ASK gate (URL-based elicitation) ──────────────────


async def _drain_elicitation_id(session_id: str, *, timeout_s: float = 5.0) -> str:
    """
    Block on the session SSE stream until a
    ``response.elicitation_request`` arrives; return its id.

    :param session_id: Session to subscribe to.
    :param timeout_s: Max seconds to wait before failing the test.
    :returns: The published ``elicitation_id``.
    """
    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_request":
                eid = event.get("elicitation_id")
                assert isinstance(eid, str) and eid, f"missing id: {event!r}"
                return eid
    raise AssertionError("subscribe loop ended without an elicitation event")


def _patch_default_policies(monkeypatch: pytest.MonkeyPatch, fn_path: str) -> None:
    """
    Install a single function policy as the runtime default_policies.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param fn_path: Dotted path to the policy callable, e.g.
        ``"<module>._ask_for_bash"``.
    """
    policy = FunctionPolicySpec(
        name="admin__ask_bash",
        on=None,
        function=FunctionRef(path=fn_path),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[policy],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )


async def test_tool_call_ask_holds_gate_and_returns_allow_on_accept(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A TOOL_CALL ASK holds the gate server-side and collapses to
    ``POLICY_ACTION_ALLOW`` when the human accepts via the resolve URL.

    This is the native anti-bypass: the evaluate endpoint must NOT
    return ``POLICY_ACTION_ASK`` (which the hook would map to ``defer``,
    letting a permissive permission_mode auto-approve). Instead it
    parks, publishes an elicitation, and waits for the URL-based
    verdict. If the parking regressed, the POST would return ASK
    immediately and the drain below would still see the event but the
    final result would be ASK, not ALLOW.
    """
    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_bash")
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # The evaluate POST parks until the verdict arrives — run it
    # concurrently and learn the elicitation id from the stream.
    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
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

    resp = await evaluate
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"


async def test_tool_call_ask_returns_deny_on_decline(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A declined TOOL_CALL ASK collapses to ``POLICY_ACTION_DENY`` —
    fail-closed. If the human refuses at the approve URL, the native
    tool must not run.
    """
    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_bash")
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_call_request("Bash"),
        )
    )

    elicitation_id = await drain
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "decline"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await evaluate
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "POLICY_ACTION_DENY"


async def test_tool_result_ask_holds_gate_and_returns_allow_on_accept(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pi-native TOOL_RESULT ASK parks server-side and collapses to ALLOW on accept.

    pi-native's ``tool_result`` extension hook can replace/suppress a result
    before the model consumes it, so the result checkpoint is a real gate.
    Its only ASK primitive is this same URL-based elicitation, so the endpoint
    must park a result-phase ASK (publish a card, hold the connection) and
    return a hard ALLOW when the human accepts — never a raw ASK. ALLOW means
    the extension returns the real result to the model. The result gate parks
    ONLY for the pi-native harness, so this test binds a pi-native agent.
    """
    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_sensitive_output")
    agent = await _create_pi_native_agent(client)
    session_id = await _create_session(client, agent["id"])

    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_result_request("please REVIEW this output"),
        )
    )

    elicitation_id = await drain
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await evaluate
    assert resp.status_code == 200, resp.text
    # Parked and collapsed to a hard verdict — NOT a raw ASK.
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"


async def test_tool_result_ask_returns_deny_on_decline(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A declined pi-native TOOL_RESULT ASK collapses to DENY so the extension suppresses it.

    If the human refuses at the approve URL, the result must be withheld from
    the model — the DENY verdict drives the extension's result-suppression
    (replace output with a policy error). The result gate parks ONLY for the
    pi-native harness, so this test binds a pi-native agent.
    """
    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_sensitive_output")
    agent = await _create_pi_native_agent(client)
    session_id = await _create_session(client, agent["id"])

    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_result_request("please REVIEW this output"),
        )
    )

    elicitation_id = await drain
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "decline"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await evaluate
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "POLICY_ACTION_DENY"


async def test_tool_result_ask_returns_raw_ask_for_non_pi_native_harness(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A TOOL_RESULT ASK does NOT park for a non-pi-native harness.

    Claude / Codex / Cursor post a *committed* tool result via PostToolUse
    (which maps to PHASE_TOOL_RESULT). They cannot suppress that result — they
    can only annotate it — so parking their result-phase ASK would stall the
    PostToolUse hook on an approval that can never change the output. The
    endpoint must therefore return the raw ASK immediately (no elicitation
    card, no held connection) for any non-pi-native session, exactly as it did
    before the result gate existed; ``evaluation_response_to_hook_output``
    discards it as a no-op on PostToolUse. This guards the regression where
    adding TOOL_RESULT to the ASK-gate phases unconditionally would hang
    Claude/Codex turns.
    """
    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_sensitive_output")
    # Default harness is claude-sdk (NOT pi-native).
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # No elicitation should ever be published; the POST returns immediately.
    resp = await asyncio.wait_for(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_result_request("please REVIEW this output"),
        ),
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    # Raw ASK returned to the caller — the gate did NOT park.
    assert resp.json()["result"] == "POLICY_ACTION_ASK"


async def test_tool_call_ask_forwards_popup_event_to_runner(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A parked TOOL_CALL ASK forwards a ``cost_approval_popup`` to the runner.

    This closes the gap where native tool-policy ASKs moved server-side
    and stopped showing in the TUI. The gate must now also forward a popup
    event so the native terminal can answer it — carrying the SAME
    ``elicitation_id`` it parks on, so resolving via the popup's endpoint
    releases the gate. Without the forward, native-terminal users would see
    nothing and the gate would hold until the web card or timeout.
    """
    _patch_default_policies(monkeypatch, f"{__name__}._ask_for_bash")
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Capture the runner forward. Installed after session creation (which
    # itself touches the runner snapshot path); the forward falls back to
    # the global runner client when no runner is bound for the session.
    capturing = CapturingRunnerClient()
    monkeypatch.setattr("omnigent.runtime._globals._runner_client", capturing)

    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    await asyncio.sleep(0.05)
    evaluate = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_call_request("Bash"),
        )
    )
    elicitation_id = await drain

    # The popup forward fires as a task when the gate parks; wait on the
    # capturing client's event rather than polling a sleep.
    await asyncio.wait_for(capturing.popup_seen.wait(), timeout=5.0)
    popups = [e for e in capturing.posted if e["json"].get("type") == "cost_approval_popup"]
    assert popups, "parked tool-policy ASK forwarded no popup — native terminal sees nothing"
    popup = popups[0]
    assert popup["url"] == f"/v1/sessions/{session_id}/events"
    # Same id the gate parked on, so the popup's resolve releases this gate.
    assert popup["json"]["elicitation_id"] == elicitation_id
    # Carries the policy's reason (the engine prefixes the deciding policy
    # name) so the popup is meaningful.
    assert "Approve running Bash?" in popup["json"]["message"]

    # Resolve via the same endpoint the popup uses → the gate collapses to ALLOW.
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    resp = await evaluate
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"


# ── Concurrent ASK-gate serialization (parallel tool calls) ──


def test_native_ask_gate_lock_keys_by_session_and_policy() -> None:
    """
    ``_native_ask_gate_lock`` returns one lock per (session, policy).

    The same ``(conversation_id, deciding_policy)`` pair must return the
    *same* lock object (so concurrent asks for one checkpoint serialize),
    while a different session OR a different policy returns a *different*
    lock (so unrelated approvals are not forced into a single global
    queue). If the helper ignored either key dimension, two of these
    asserts would see the same object and fail.
    """
    from omnigent.server.routes.sessions import _native_ask_gate_lock

    lock_a = _native_ask_gate_lock("conv_1", "session_cost_guard")
    # Same key → same lock: this is what makes parallel tool calls that
    # all trip one checkpoint share a single gate.
    assert _native_ask_gate_lock("conv_1", "session_cost_guard") is lock_a
    # Different policy on the same session → different lock, so a cost ask
    # and (say) a destructive-file ask can prompt concurrently.
    assert _native_ask_gate_lock("conv_1", "other_policy") is not lock_a
    # Different session → different lock, so sessions stay independent.
    assert _native_ask_gate_lock("conv_2", "session_cost_guard") is not lock_a


async def test_concurrent_cost_asks_serialize_and_collapse_sibling(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Parallel native tool calls that trip one cost checkpoint prompt once.

    Reproduces the reported bug: a claude/codex-native agent fires several
    tool calls at once; each spawns a ``PreToolUse`` hook that lands here
    concurrently, and every one independently crosses the same cost
    warning threshold. Without the per-(session, policy) lock, each would
    publish its own approval gate and the human would be asked N times for
    one decision.

    The gate is stubbed to stand in for the human-approval wait (the real
    gate's elicitation parking is covered by the accept/decline tests
    above). The stub holds the *first* entrant — and therefore the real
    lock the route handler acquires around it — until the test releases
    it, then records the ASKing policy's checkpoint exactly as the real
    gate does on accept. The assertions prove (1) a second concurrent ask
    can NOT enter the gate while the first is pending (the lock serializes
    them) and (2) once the first records approval, the sibling
    re-evaluates to ALLOW and never prompts. Delete the
    ``async with _native_ask_gate_lock(...)`` wrapper (or the re-evaluate
    inside it) and this test fails: ``entries`` reaches 2 and the second
    request would prompt again.
    """
    agent = await create_test_agent(
        client,
        guardrails={
            "policies": {
                "session_cost_guard": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.policies.builtins.cost.cost_budget",
                        "arguments": {
                            # Hard cap far above the seeded cost so only the
                            # soft warning (ASK) fires, never a DENY.
                            "max_cost_usd": 1000.0,
                            "ask_thresholds_usd": [0.10],
                        },
                    },
                }
            }
        },
    )
    session_id = await _create_session(client, agent["id"])

    # Seed cumulative cost just past the $0.10 warning checkpoint (the
    # engine reads total_cost_usd from the conversation's session_usage).
    store = SqlAlchemyConversationStore(db_uri)
    store.set_session_usage(session_id, {"total_cost_usd": 0.13})

    entries = 0
    first_in_gate = asyncio.Event()
    second_in_gate = asyncio.Event()
    release_first = asyncio.Event()

    async def _controllable_gate(
        request: Any,
        *,
        session_id: str,
        phase: Any,
        data: dict[str, Any],
        engine: Any,
        result: Any,
        conversation_store: Any,
        elicitation_id: str | None = None,
    ) -> bool:
        """
        Stand-in for ``_hold_native_ask_gate`` that simulates accept.

        The first entrant blocks (holding the route handler's lock) until
        the test releases it; a second entrant trips ``second_in_gate`` so
        the test can detect a serialization failure. Every entrant then
        records the ASKing policy's ``state_updates`` exactly as the real
        gate does on accept (POLICIES.md §7.2) and returns ``True``.

        :param request: FastAPI request (unused by the stub).
        :param session_id: Session id (unused by the stub).
        :param phase: Enforcement phase (unused by the stub).
        :param data: Proto event data (unused by the stub).
        :param engine: The policy engine — used to persist the approved
            checkpoint so a sibling's rebuild observes it.
        :param result: The composed ASK result carrying ``state_updates``.
        :param conversation_store: Conversation store (unused by the stub).
        :returns: ``True`` (accept) for every entrant.
        """
        nonlocal entries
        entries += 1
        if entries == 1:
            first_in_gate.set()
            await release_first.wait()
        elif entries == 2:
            second_in_gate.set()
        if result.state_updates:
            engine.apply_state_updates(result.state_updates)
        return True

    monkeypatch.setattr(sessions_routes, "_hold_native_ask_gate", _controllable_gate)

    first = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_call_request("Bash"),
        )
    )
    # First request reaches the gate and is now holding the lock.
    await asyncio.wait_for(first_in_gate.wait(), timeout=5.0)

    second = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_tool_call_request("Bash"),
        )
    )
    # The second request crosses the same checkpoint, so without the lock
    # it would enter the gate immediately. With the lock it blocks on lock
    # acquisition and cannot reach the gate while the first holds it. A
    # generous bounded wait: in the broken (no-lock) world the second entry
    # fires in milliseconds; in the correct world it can never fire here.
    second_entered = False
    try:
        await asyncio.wait_for(second_in_gate.wait(), timeout=1.0)
        second_entered = True
    except asyncio.TimeoutError:
        second_entered = False
    assert not second_entered, (
        "A second concurrent cost ask entered the gate while the first was "
        "still pending. The per-(session, policy) lock failed to serialize "
        "them, so the human would be prompted twice for one $0.10 checkpoint."
    )

    # Release the first ask: it records the checkpoint and returns ALLOW.
    release_first.set()
    first_resp = await asyncio.wait_for(first, timeout=5.0)
    second_resp = await asyncio.wait_for(second, timeout=5.0)

    # 1 = the single prompt the human should ever see. If 2, the sibling
    # re-evaluated to ASK instead of ALLOW (the recorded checkpoint was not
    # observed) or the lock did not serialize the two requests.
    assert entries == 1, (
        f"Expected exactly one ASK gate entry for two concurrent tool calls "
        f"crossing the same checkpoint, got {entries}."
    )
    # First ask was accepted → ALLOW.
    assert first_resp.json()["result"] == "POLICY_ACTION_ALLOW", first_resp.text
    # Sibling re-evaluated under the lock against the recorded checkpoint →
    # ALLOW with no second prompt.
    assert second_resp.json()["result"] == "POLICY_ACTION_ALLOW", second_resp.text


# ── LLM_REQUEST / LLM_RESPONSE phase tests ───────────────────


async def test_llm_request_allow_when_no_matching_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    PHASE_LLM_REQUEST with no policies returns ALLOW (unspecified
    pass-through).

    Verifies the proto mapping ``PHASE_LLM_REQUEST`` →
    ``Phase.LLM_REQUEST`` is correct. If the mapping still pointed
    at ``Phase.REQUEST``, the policy engine would route to the wrong
    phase and possibly match session-level REQUEST policies instead.
    """
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_llm_request_payload(messages_count=10),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No policies registered → ALLOW (unspecified pass-through).
    assert body["result"] in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"), (
        f"Expected ALLOW or UNSPECIFIED for no-policy session, got {body['result']}. "
        "If DENY, a default policy may be incorrectly matching LLM_REQUEST."
    )


async def test_llm_request_deny_by_function_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A function policy targeting ``llm_request`` correctly denies
    large prompt payloads.

    The policy checks ``messages_count > 100`` in the data dict.
    This verifies that ``_build_evaluation_context`` passes the
    full data dict as ``content`` for LLM_REQUEST (not just text).
    """
    _patch_default_policies(monkeypatch, f"{__name__}._deny_large_llm_request")
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Small request → ALLOW
    resp_allow = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_llm_request_payload(messages_count=50),
    )
    assert resp_allow.status_code == 200, resp_allow.text
    assert resp_allow.json()["result"] == "POLICY_ACTION_ALLOW", (
        "50 messages should be allowed by the policy (threshold is 100). "
        "If DENY, the policy condition is wrong or the data wasn't passed correctly."
    )

    # Large request → DENY
    resp_deny = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_llm_request_payload(messages_count=200),
    )
    assert resp_deny.status_code == 200, resp_deny.text
    deny_body = resp_deny.json()
    assert deny_body["result"] == "POLICY_ACTION_DENY", (
        "200 messages should be denied by the policy (threshold is 100). "
        "If ALLOW, the messages_count data wasn't passed through to the policy callable."
    )
    assert "200 messages" in deny_body.get("reason", ""), (
        "Reason should mention the message count to confirm data propagation."
    )


async def test_llm_response_deny_by_function_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A function policy targeting ``llm_response`` correctly denies
    responses containing PII markers.

    The policy checks for ``SSN`` in ``text_preview``. This
    verifies the ``_build_evaluation_context`` passes the data dict
    as ``content`` for ``LLM_RESPONSE``.
    """
    _patch_default_policies(monkeypatch, f"{__name__}._deny_llm_response_with_pii")
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Clean response → ALLOW
    resp_allow = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_llm_response_payload(text_preview="Hello, how can I help?"),
    )
    assert resp_allow.status_code == 200, resp_allow.text
    assert resp_allow.json()["result"] == "POLICY_ACTION_ALLOW", (
        "Clean response should be allowed. "
        "If DENY, the policy fired incorrectly on non-PII content."
    )

    # PII response → DENY
    resp_deny = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_llm_response_payload(text_preview="Your SSN is 123-45-6789"),
    )
    assert resp_deny.status_code == 200, resp_deny.text
    deny_body = resp_deny.json()
    assert deny_body["result"] == "POLICY_ACTION_DENY", (
        "Response containing SSN should be denied. "
        "If ALLOW, the text_preview wasn't passed to the policy callable."
    )
    assert "PII" in deny_body.get("reason", ""), (
        "Reason should mention PII to confirm the correct policy fired."
    )


async def test_llm_response_allow_when_no_matching_policy(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    PHASE_LLM_RESPONSE with no policies returns ALLOW.

    Symmetric with the LLM_REQUEST no-policy test.
    """
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_llm_response_payload(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"), (
        f"Expected ALLOW or UNSPECIFIED for no-policy session, got {body['result']}."
    )
