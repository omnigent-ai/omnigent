"""
Reproduction for a surprise hard block by a spawn-attached cost budget
the user never set, at many times the cap, with no ASK/warning first.

Journey: a session acquires a small cost budget the user never set — the
sub-agent spawn path (``sys_session_send``'s ``cost_budget`` argument)
attaches one chosen by the orchestrating model, with only
``max_cost_usd`` and no ``ask_thresholds_usd``. The user keeps working
normally; once cumulative spend crosses the cap, the very next request /
tool call is hard-DENYed ("spend $37.86 reached the $3.00 limit" — 12.6x
over the cap) with no prior warning checkpoint, and the budget was never
surfaced to the user beforehand.

These tests drive the real server app (real stores + policy engine, the
same ``POST /v1/sessions/{id}/policies`` payload the spawn path posts)
through that journey:

- ``test_spawn_attached_max_only_budget_warns_before_hard_block`` — the
  exact spawn-time attach (handler ``subagent_cost_budget``, name
  ``__subagent_cost_budget``, max-only params), spend seeded far over
  the cap, then the next tool call is gated. Requires the FIRST
  over-budget gate to produce a user-visible warning (an ASK
  elicitation) rather than an un-warned hard DENY. FAILS on the buggy
  build (immediate DENY at 12.6x the cap, no elicitation ever
  published); passes once a warning precedes — or replaces — the
  un-warned hard block.
- ``test_spawn_attached_budget_is_visible_in_policy_list`` — the same
  spawn-shaped attach must appear in
  ``GET /v1/sessions/{id}/policies`` so the active budget is not
  invisible to the user ("the session's policy list came back empty").
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime import session_stream
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


# ── Fixtures ────────────────────────────────────────────────────────────

# The exact policy payload the sys_session_send spawn path posts to the
# child session (omnigent/runner/tool_dispatch.py) when the orchestrating
# model passes a max-only cost_budget — the shape the reporter was hit by.
_SPAWN_BUDGET_PAYLOAD: dict[str, Any] = {
    "name": "__subagent_cost_budget",
    "type": "python",
    "handler": "omnigent.policies.builtins.cost.subagent_cost_budget",
    "factory_params": {"max_cost_usd": 3.0},
    "enabled": True,
}


@pytest.fixture()
def policy_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """
    FastAPI app with a policy store wired in.

    The standard ``app`` fixture from ``conftest.py`` omits the policy
    store, so the session policy CRUD routes are not mounted. This
    fixture adds one so ``POST /v1/sessions/{id}/policies`` (the exact
    call the spawn path makes) and the evaluate endpoint's
    ``get_policy_store()`` both see session-attached policies.

    :param runtime_init: Fixture that initializes the runtime with a
        mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
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
        comment_store=SqlAlchemyCommentStore(db_uri),
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )


@pytest_asyncio.fixture()
async def client(
    policy_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """
    Async HTTP client wired to the policy-enabled app.

    Mirrors the shared ``client`` fixture but targets ``policy_app`` so
    session policy CRUD routes are available, and patches the runtime's
    ``_policy_store`` global so the evaluate endpoint picks up
    session-attached policies.

    :param policy_app: The policy-enabled FastAPI app.
    :param mock_llm: Controllable mock LLM (released on teardown).
    :param tmp_path: Pytest temporary directory fixture.
    :param db_uri: Test database URI.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runtime import _globals, set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    monkeypatch.setattr(_globals, "_policy_store", SqlAlchemyPolicyStore(db_uri))

    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── Helpers ─────────────────────────────────────────────────────────────


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a session bound to an agent and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
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


async def _drain_elicitation_id(
    session_id: str,
    *,
    subscribed: asyncio.Event | None = None,
    timeout_s: float = 15.0,
) -> str:
    """
    Block on the session SSE stream until a
    ``response.elicitation_request`` arrives; return its id.

    :param session_id: Session to subscribe to.
    :param subscribed: When provided, set as soon as the SSE subscriber
        slot is registered, so callers can sequence the triggering
        action without a sleep.
    :param timeout_s: Max seconds to wait before failing.
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


# ── Tests ───────────────────────────────────────────────────────────────


async def test_spawn_attached_max_only_budget_warns_before_hard_block(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The first over-budget gate must warn, not silently hard-DENY.

    Reconstructs the reported journey: the session accrues spend with
    no budget in sight, then a max-only $3 budget lands without the
    user setting it — attached with the exact payload the
    ``sys_session_send`` spawn path posts (no ``ask_thresholds_usd``,
    no ``expensive_models``, so the cap is a block-all hard stop). On
    the buggy build the very next gate is a hard DENY at $37.86
    against $3.00 (12.6x over) with no ASK/warning ever shown. The
    fixed behavior must surface a user-visible warning (ASK
    elicitation) before — or instead of — the un-warned hard block.
    """
    store = SqlAlchemyConversationStore(db_uri)

    # Agent with NO budget anywhere: not in config.yaml guardrails, not
    # account-level. The user never set a budget.
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    # The user works normally; cumulative spend accrues to $37.86
    # before any budget exists.
    store.set_session_usage(session_id, {"total_cost_usd": 37.86})

    # A $3 max-only budget lands without user action — the exact
    # payload the spawn path posts to the child session.
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_SPAWN_BUDGET_PAYLOAD,
    )
    assert resp.status_code < 400, f"policy attach failed: {resp.status_code} {resp.text}"

    # Next user action: a tool call. Watch the session stream for a
    # warning elicitation while the gate evaluates.
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

    done, _pending = await asyncio.wait(
        {drain, evaluate_task},
        return_when=asyncio.FIRST_COMPLETED,
        timeout=20.0,
    )
    assert done, "neither the policy gate nor a warning elicitation completed in 20s"

    if drain in done and not drain.cancelled() and drain.exception() is None:
        # Fixed behavior: a warning ASK was published before any hard
        # block. Approve it and let the evaluation settle.
        elicitation_id = drain.result()
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text
        eval_resp = await evaluate_task
        assert eval_resp.status_code == 200, eval_resp.text
        return

    # The gate answered without ever publishing a warning elicitation.
    drain.cancel()
    eval_resp = evaluate_task.result()
    assert eval_resp.status_code == 200, eval_resp.text
    body = eval_resp.json()
    assert body["result"] != "POLICY_ACTION_DENY", (
        "surprise block reproduced: the first over-budget gate of a "
        "max-only cost budget the user never set hard-DENYed with no "
        f"prior ASK/warning. DENY reason: {body.get('reason')!r}"
    )


async def test_spawn_attached_budget_is_visible_in_policy_list(
    client: httpx.AsyncClient,
) -> None:
    """A spawn-attached budget must show up in the session policy list.

    The reporter's session was blocked by a budget while "the session's
    policy list came back empty". Attach the budget with the exact
    payload the ``sys_session_send`` spawn path posts and require
    ``GET /v1/sessions/{id}/policies`` to surface it.
    """
    agent = await create_test_agent(client)
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=_SPAWN_BUDGET_PAYLOAD,
    )
    assert resp.status_code < 400, f"policy attach failed: {resp.status_code} {resp.text}"

    listing = await client.get(f"/v1/sessions/{session_id}/policies")
    assert listing.status_code == 200, listing.text
    names = [p.get("name") for p in listing.json()["data"]]
    assert "__subagent_cost_budget" in names, (
        "invisible-budget facet reproduced: the spawn-attached cost budget "
        f"is missing — session policy list returned {names!r}"
    )
