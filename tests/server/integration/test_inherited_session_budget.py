"""Native descendants evaluate the root's budget at their own policy gates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runtime import _globals
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.routes import sessions as sessions_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """Enable session policy storage for the shared server client fixture."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    policy_store = SqlAlchemyPolicyStore(db_uri)
    monkeypatch.setattr(_globals, "_policy_store", policy_store)
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        policy_store=policy_store,
    )


@pytest.fixture()
def interrupted_sessions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record interruption broadcasts without requiring connected runners."""
    interrupted: list[str] = []

    async def forward(session_id: str, router: Any, change: Any, *a: Any, **k: Any) -> None:
        if isinstance(change, dict) and change.get("type") == "interrupt":
            interrupted.append(session_id)

    monkeypatch.setattr(sessions_routes, "_forward_session_change_to_runner", forward)
    return interrupted


async def _create_budget_tree(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    *,
    expensive_models: list[str] | None = None,
) -> tuple[str, str, str, str]:
    """Create a root-only $50 budget and seed $40 spread across four sessions."""
    agent = await create_test_agent(client)
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert response.status_code == 201, response.text
    root_id = response.json()["id"]
    child = store.create_conversation(agent_id=agent["id"], parent_conversation_id=root_id)
    grandchild = store.create_conversation(agent_id=agent["id"], parent_conversation_id=child.id)
    sibling = store.create_conversation(agent_id=agent["id"], parent_conversation_id=root_id)
    params: dict[str, Any] = {"max_cost_usd": 50.0}
    if expensive_models is not None:
        params["expensive_models"] = expensive_models
    attached = await client.post(
        f"/v1/sessions/{root_id}/policies",
        json={
            "name": "session_cost_budget",
            "type": "python",
            "handler": "omnigent.policies.builtins.cost.cost_budget",
            "factory_params": params,
        },
    )
    assert attached.status_code < 300, attached.text
    for session_id, cost in ((root_id, 10), (child.id, 5), (grandchild.id, 5), (sibling.id, 20)):
        store.set_session_usage(session_id, {"total_cost_usd": cost})
    return root_id, child.id, grandchild.id, sibling.id


async def _evaluate(client: httpx.AsyncClient, session_id: str, phase: str) -> dict[str, Any]:
    """Post the same request/tool boundary checks used by native policy hooks."""
    response = await client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={
            "event": {
                "type": phase,
                "target": "",
                "data": {"name": "Bash", "arguments": {"command": "ls"}}
                if phase == "PHASE_TOOL_CALL"
                else {},
                "context": {},
            }
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("phase", ["PHASE_REQUEST", "PHASE_TOOL_CALL"])
@pytest.mark.parametrize("descendant", ["child", "grandchild"])
async def test_descendant_checks_inherited_budget_independently(
    client: httpx.AsyncClient,
    db_uri: str,
    interrupted_sessions: list[str],
    phase: str,
    descendant: str,
) -> None:
    """A child's own gate detects shared spend without evaluating the parent."""
    store = SqlAlchemyConversationStore(db_uri)
    _root_id, child_id, grandchild_id, sibling_id = await _create_budget_tree(client, store)
    session_id = child_id if descendant == "child" else grandchild_id

    allowed = await _evaluate(client, session_id, phase)
    assert allowed["result"] == "POLICY_ACTION_ALLOW", allowed

    # Only the sibling spends more; the evaluated descendant remains at $5.
    store.set_session_usage(sibling_id, {"total_cost_usd": 35})
    denied = await _evaluate(client, session_id, phase)
    assert denied["result"] == "POLICY_ACTION_DENY", denied
    assert "$55.00" in denied["reason"]
    assert "$50.00" in denied["reason"]
    assert interrupted_sessions == []


@pytest.mark.parametrize("phase", ["PHASE_REQUEST", "PHASE_TOOL_CALL"])
async def test_inherited_downgrade_budget_allows_cheaper_child_model(
    client: httpx.AsyncClient,
    db_uri: str,
    interrupted_sessions: list[str],
    phase: str,
) -> None:
    """An inherited model-specific cap preserves the existing downgrade escape."""
    store = SqlAlchemyConversationStore(db_uri)
    root_id, child_id, _grandchild_id, sibling_id = await _create_budget_tree(
        client, store, expensive_models=["opus"]
    )
    store.set_session_usage(sibling_id, {"total_cost_usd": 35})
    store.update_conversation(root_id, model_override="claude-opus-4-1")
    store.update_conversation(child_id, model_override="claude-opus-4-1")
    denied = await _evaluate(client, child_id, phase)
    assert denied["result"] == "POLICY_ACTION_DENY", denied

    store.update_conversation(child_id, model_override="gpt-4o-mini")
    allowed = await _evaluate(client, child_id, phase)
    assert allowed["result"] == "POLICY_ACTION_ALLOW", allowed
    assert interrupted_sessions == []
