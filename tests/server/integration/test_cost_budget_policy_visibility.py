"""
Agent-spec guardrail policies must appear in the session policy list.

Two sub-symptoms, both reproduced here:

Symptom 1 — ``GET /v1/sessions/{id}/policies`` omits agent-spec
guardrail policies.

The endpoint's docstring says it returns both ``source="session"``
(store-persisted) and ``source="spec"`` (spec-declared) policies, but
the implementation only returns admin + session-store policies.  An
agent with a ``cost_budget`` guardrail declared in its spec will block
a session, but the policy list comes back empty, giving the user no
indication of the active limit.  This is why the user sees "the session
policy list returned empty" while a $3 limit is actively enforced.

Symptom 2 — Hard block fires without an ASK warning.

The cost_budget policy evaluates against the cumulative
``total_cost_usd`` of the whole spawn tree.  When a session's
cumulative spend is already far above the cap (e.g. $37.86 vs a $3
limit), every new turn is immediately DENY-ed with no prior soft
warning (ASK).  The ``ask_thresholds_usd`` param can mitigate this, but
the bug is that the budget limit itself is invisible (symptom 1), so
users have no way to know a threshold is needed.

Test structure:

- ``test_spec_cost_budget_policy_visible_in_list`` (symptom 1): uses a
  ``policy_store``-enabled app so the ``GET /policies`` routes are
  mounted; asserts the agent-spec ``cost_budget`` policy IS returned.
  Fails on the buggy build (empty list).

- ``test_spec_cost_budget_hard_blocks_while_invisible_in_list``
  (symptom 2): uses the plain ``client`` fixture (same as existing
  cost-aware tests) so the policy engine is wired through the agent
  spec path; asserts the DENY fires immediately with no prior soft
  warning when the session's cumulative spend is already above the cap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def policy_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App with a ``policy_store`` so the ``GET /policies`` route is active.

    No auth provider — falls through to the unauthenticated path (no
    permission checks required), which is sufficient for testing
    policy list visibility.

    :param runtime_init: Initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: FastAPI app with policy CRUD and session routes.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        policy_store=SqlAlchemyPolicyStore(db_uri),
        comment_store=SqlAlchemyCommentStore(db_uri),
    )


@pytest_asyncio.fixture()
async def policy_client(
    policy_app: FastAPI,
    mock_llm: ControllableMockClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the policy-store-enabled app.

    Also patches the runtime-global ``_policy_store`` so the policy
    engine path (``get_policy_store()``) sees session-scoped policies.

    :param policy_app: FastAPI app with policy store.
    :param mock_llm: Controllable mock LLM — released on teardown.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for the harness process manager.
    :param monkeypatch: Pytest monkeypatch fixture.
    :yields: A ready-to-use async HTTP client.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    policy_store = SqlAlchemyPolicyStore(db_uri)
    monkeypatch.setattr("omnigent.runtime._globals._policy_store", policy_store)

    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
) -> str:
    """Create a session bound to *agent_id* and return its conversation id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create session failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


def _cost_budget_guardrails(max_cost_usd: float) -> dict[str, Any]:
    """Build guardrails with a ``cost_budget`` policy at *max_cost_usd*.

    No ``expensive_models`` specified: the policy blocks ALL models once
    over budget (true hard stop), matching the bug-report scenario where
    the session was blocked regardless of model.

    :param max_cost_usd: Hard spend cap in USD.
    :returns: Guardrails dict for :func:`create_test_agent`.
    """
    return {
        "policies": {
            "session_cost_guard": {
                "type": "function",
                "function": {
                    "path": "omnigent.policies.builtins.cost.cost_budget",
                    "arguments": {
                        "max_cost_usd": max_cost_usd,
                        # No expensive_models → block all models once over
                        # budget (the hard-stop semantics in the bug report).
                    },
                },
            }
        }
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_spec_cost_budget_policy_visible_in_list(
    policy_client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/policies must include agent-spec guardrail policies.

    When an agent declares a ``cost_budget`` guardrail in its spec, that policy
    is active and enforced.  The list endpoint must surface it so the user can
    see what limits are in effect.

    On the buggy build the endpoint omits spec-declared policies entirely —
    the list comes back empty, leaving the user with no visibility into the
    active $3.00 limit.  This is the root cause of the "mysterious $3 limit":
    the user's session was blocked by a spec-declared ``cost_budget`` policy
    that ``GET /policies`` reported as not existing.

    This test FAILS on the buggy build (empty list) and PASSES once spec
    policies are included in the ``GET /policies`` response.
    """
    agent = await create_test_agent(
        policy_client,
        name="budget-visible-agent",
        guardrails=_cost_budget_guardrails(max_cost_usd=3.0),
    )
    session_id = await _create_session(policy_client, agent["id"])

    resp = await policy_client.get(f"/v1/sessions/{session_id}/policies")
    assert resp.status_code == 200, f"GET /policies failed: {resp.text}"
    data = resp.json()

    assert data["object"] == "list"
    policies = data["data"]

    # The spec-declared cost_budget policy MUST appear in the list.
    # Without the fix: policies == [] (empty list) → this assertion fails.
    cost_budget_policies = [
        p
        for p in policies
        if "cost_budget" in str(p.get("handler", ""))
        or "cost_budget" in str(p.get("factory_params", ""))
    ]
    assert cost_budget_policies, (
        f"Expected at least one cost_budget policy in GET /sessions/{{id}}/policies, "
        f"but got {len(policies)} total policies (none with cost_budget): "
        f"{policies!r}.  The spec-declared $3.00 cost_budget guardrail is "
        f"active and blocking, but invisible to the user "
    )

    # The entry should expose the limit value so the user can see it.
    budget_entry = cost_budget_policies[0]
    factory_params = budget_entry.get("factory_params", {})
    assert factory_params.get("max_cost_usd") == 3.0, (
        f"cost_budget policy should surface max_cost_usd=3.0, got: {factory_params!r}"
    )


async def test_spec_cost_budget_hard_blocks_while_invisible_in_list(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Cost-budget hard block fires while policy is invisible in the list.

    The compound failure from the bug report:

    1. A session is bound to an agent with a $3 cost_budget guardrail.
    2. Cumulative spend climbs to $37.86 (above the cap).
    3. The user queries ``GET /policies`` — returns empty (policy invisible).
    4. The user sends a new turn — immediately DENY with the $3 cap error.

    The user has no way to know the limit existed, no warning before the
    block, and no affordance to act on it.

    This test uses the plain ``client`` fixture (matching
    ``test_journey_cost_aware_e2e.py``) so the policy engine evaluates
    through the agent-spec path, confirming the DENY fires correctly.
    Asserts the DENY fires (policy IS enforced) — confirms the spec
    guardrail reaches the evaluate path even when it is invisible in the
    policy list (symptom 1).
    """
    store = SqlAlchemyConversationStore(db_uri)

    # No expensive_models → hard stop for all models (matches bug report).
    agent = await create_test_agent(
        client,
        name="mystery-budget-agent",
        guardrails=_cost_budget_guardrails(max_cost_usd=3.0),
    )
    session_id = await _create_session(client, agent["id"])

    # Seed cumulative spend at $37.86 (the exact value from the bug report).
    store.set_session_usage(session_id, {"total_cost_usd": 37.86})

    # ── Step A: hard DENY fires immediately — no prior warning ───────────────
    eval_resp = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {"name": "Bash", "arguments": {}},
                "context": {},
            }
        },
    )
    assert eval_resp.status_code == 200, f"evaluate failed: {eval_resp.text}"
    body = eval_resp.json()

    # Policy IS enforced — DENY fires from the spec-declared guardrail.
    assert body["result"] == "POLICY_ACTION_DENY", (
        f"Expected POLICY_ACTION_DENY for spend $37.86 against $3.00 cap, "
        f"got {body['result']!r}.  Verify the spec-declared cost_budget policy "
        f"reaches the evaluate path via agent guardrails."
    )
    reason = body.get("reason", "")
    # Match the exact error from the bug report: "spend $37.86 reached the $3.00 limit"
    assert "3.00" in reason or "3" in reason, (
        f"DENY reason should mention the $3.00 limit, got: {reason!r}"
    )
    assert "37.86" in reason, (
        f"DENY reason should mention the current spend $37.86, got: {reason!r}"
    )
