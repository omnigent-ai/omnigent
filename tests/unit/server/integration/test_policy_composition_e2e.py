"""Integration tests for multi-policy composition and precedence.

Verifies the PolicyEngine's composition semantics through the
``POST /v1/sessions/{id}/policies/evaluate`` endpoint:

- DENY takes precedence over ASK on the same phase (engine
  short-circuits on the first DENY).
- Removing a DENY policy lets ASK fire on re-evaluation.
- Two DENY policies: the first in declaration order short-circuits,
  so only its reason is surfaced.
- ALLOW does not override DENY.

Uses ``make_fixed_action_callable`` factories injected via
``default_policies`` monkeypatch so the tests exercise the real
engine composition pipeline end-to-end without depending on the
policy registry allowlist.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runtime import get_caps
from omnigent.runtime.caps import RuntimeCaps
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from tests.unit.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_MAKE_FIXED = "omnigent.policies.function.make_fixed_action_callable"


def _deny_spec(name: str, reason: str) -> FunctionPolicySpec:
    """Build a FunctionPolicySpec that always DENYs.

    :param name: Policy name, e.g. ``"deny_all"``.
    :param reason: Deny reason surfaced in the verdict.
    :returns: A spec ready for ``default_policies``.
    """
    return FunctionPolicySpec(
        name=name,
        on=None,
        function=FunctionRef(
            path=_MAKE_FIXED,
            arguments={"action": "deny", "reason": reason},
        ),
    )


def _ask_spec(name: str, reason: str) -> FunctionPolicySpec:
    """Build a FunctionPolicySpec that always ASKs.

    :param name: Policy name, e.g. ``"ask_all"``.
    :param reason: Ask reason surfaced in the elicitation.
    :returns: A spec ready for ``default_policies``.
    """
    return FunctionPolicySpec(
        name=name,
        on=None,
        function=FunctionRef(
            path=_MAKE_FIXED,
            arguments={"action": "ask", "reason": reason},
        ),
    )


def _allow_spec(name: str, reason: str) -> FunctionPolicySpec:
    """Build a FunctionPolicySpec that always ALLOWs.

    :param name: Policy name, e.g. ``"allow_all"``.
    :param reason: Reason carried on the ALLOW result.
    :returns: A spec ready for ``default_policies``.
    """
    return FunctionPolicySpec(
        name=name,
        on=None,
        function=FunctionRef(
            path=_MAKE_FIXED,
            arguments={"action": "allow", "reason": reason},
        ),
    )


def _install_policies(
    monkeypatch: pytest.MonkeyPatch,
    policies: list[FunctionPolicySpec],
) -> None:
    """Inject policies as runtime default_policies.

    Patches ``get_caps`` in the sessions route module so the
    evaluate endpoint sees the given policies.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param policies: Policies to install.
    """
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=policies,
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )


def _tool_call_request() -> dict[str, Any]:
    """Build a PHASE_TOOL_CALL EvaluationRequest for testing.

    :returns: EvaluationRequest JSON dict targeting a generic tool.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {"name": "Read", "arguments": {}},
            "context": {},
        },
    }


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """Create a session bound to an agent.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


# ── Test 1: DENY + ASK on same phase — DENY wins ──────────────


async def test_deny_takes_precedence_over_ask(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DENY and ASK policies both fire, DENY short-circuits.

    The engine iterates policies in declaration order; a DENY result
    ends evaluation immediately. Even if an ASK policy is declared,
    the DENY fires first and no elicitation prompt is shown. If this
    regresses, the endpoint would return ASK or park for approval
    instead of returning DENY.
    """
    _install_policies(
        monkeypatch,
        [
            _deny_spec("deny_all", "Deny policy"),
            _ask_spec("ask_all", "Ask policy"),
        ],
    )
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_DENY", (
        f"Expected DENY to take precedence over ASK, got {body['result']}. "
        "The engine should short-circuit on the first DENY."
    )
    assert "Deny policy" in body.get("reason", ""), (
        f"Expected the DENY reason in the verdict; got {body}"
    )


# ── Test 2: Remove DENY, ASK fires ────────────────────────────


async def test_ask_fires_when_deny_removed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only an ASK policy (no DENY), the engine returns ASK.

    This complements test 1: after removing the DENY policy, the
    ASK policy is no longer shadowed and fires. The evaluate
    endpoint parks for approval on TOOL_CALL, so we use
    ``PHASE_LLM_RESPONSE`` (a non-blocking phase where ASK is
    returned directly without gate parking) to observe the ASK
    result. The ASK gate parking flow is covered by the dedicated
    elicitation tests.
    """
    _install_policies(
        monkeypatch,
        [_ask_spec("ask_all", "Ask policy")],
    )
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # Use LLM_RESPONSE phase — ASK on this phase is returned
    # directly (no gate parking, which only applies to TOOL_CALL
    # and LLM_REQUEST).
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={
            "event": {
                "type": "PHASE_LLM_RESPONSE",
                "data": {"model": "test", "text_preview": "hi", "tool_calls_count": 0},
                "context": {},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_ASK", (
        f"Expected ASK when only an ASK policy is present, got {body['result']}. "
        "Without a DENY policy, ASK should fire."
    )
    assert "Ask policy" in body.get("reason", ""), (
        f"Expected the ASK reason in the verdict; got {body}"
    )


# ── Test 3: Two DENY policies, reason visible ─────────────────


async def test_two_deny_policies_first_reason_visible(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With two DENY policies, the first one's reason is surfaced.

    The engine short-circuits on the first DENY in declaration
    order. The deciding policy's reason is carried in the verdict.
    This tests that multiple DENY policies compose correctly — the
    second never fires because the first already short-circuited.
    """
    _install_policies(
        monkeypatch,
        [
            _deny_spec("deny_alpha", "Alpha deny reason"),
            _deny_spec("deny_beta", "Beta deny reason"),
        ],
    )
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_DENY", (
        f"Expected DENY with two DENY policies, got {body['result']}."
    )
    # The first DENY in declaration order short-circuits — its reason
    # is the one that surfaces.
    assert "Alpha deny reason" in body.get("reason", ""), (
        f"Expected the first DENY policy's reason; got {body.get('reason')!r}. "
        "The engine should short-circuit on the first DENY in declaration order."
    )


# ── Test 4: ALLOW + DENY — DENY still wins ────────────────────


async def test_deny_overrides_allow(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ALLOW policy does not prevent a subsequent DENY from firing.

    The engine processes policies in order. An ALLOW result does
    not short-circuit — it just means "this policy has no objection."
    A later DENY still fires and overrides. If this regresses, an
    attacker could bypass a DENY by prepending an ALLOW policy.
    """
    _install_policies(
        monkeypatch,
        [
            _allow_spec("allow_all", "Allow policy"),
            _deny_spec("deny_all", "Deny policy"),
        ],
    )
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_DENY", (
        f"Expected DENY even with a preceding ALLOW, got {body['result']}. "
        "ALLOW must not override a subsequent DENY — DENY always wins."
    )
    assert "Deny policy" in body.get("reason", ""), (
        f"Expected the DENY reason; got {body.get('reason')!r}"
    )
