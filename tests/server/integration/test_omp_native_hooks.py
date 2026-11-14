"""Native OMP child isolation, policy inheritance and explicit session transitions."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_policy_evaluate import (
    _drain_elicitation_id,
    _patch_default_policies,
    _tool_result_request,
)

pytestmark = pytest.mark.asyncio


def _ask_result(event: dict[str, Any]) -> dict[str, Any]:
    return (
        {"result": "ASK", "reason": "Review native output"}
        if event.get("type") == "tool_result"
        else {"result": "ALLOW"}
    )


async def _omp(client: httpx.AsyncClient) -> str:
    agent = await create_test_agent(
        client,
        name="omp-hooks",
        executor={"type": "omnigent", "config": {"harness": "omp-native"}},
    )
    return agent["_session_id"]


@pytest.mark.parametrize(
    "action,expected", [("accept", "ALLOW"), ("decline", "DENY"), ("cancel", "DENY")]
)
async def test_native_tool_result_approval_parks_and_collapses(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, action: str, expected: str
) -> None:
    _patch_default_policies(monkeypatch, f"{__name__}._ask_result")
    parent = await _omp(client)
    drain = asyncio.create_task(_drain_elicitation_id(parent))
    await asyncio.sleep(0)
    evaluate = asyncio.create_task(
        client.post(
            f"/v1/sessions/{parent}/policies/evaluate",
            json=_tool_result_request("review me", "read"),
        )
    )
    try:
        elicitation = await asyncio.wait_for(drain, 5)
        assert not evaluate.done(), "result must stay withheld until an explicit verdict"
        resolved = await client.post(
            f"/v1/sessions/{parent}/elicitations/{elicitation}/resolve", json={"action": action}
        )
        assert resolved.status_code == 202, resolved.text
        result = await asyncio.wait_for(evaluate, 5)
        assert result.status_code == 200, result.text
        assert result.json()["result"] == f"POLICY_ACTION_{expected}"
    finally:
        for task in (drain, evaluate):
            if not task.done():
                task.cancel()
        await asyncio.gather(drain, evaluate, return_exceptions=True)


@pytest.mark.parametrize("phase", ["PHASE_TOOL_CALL", "PHASE_TOOL_RESULT"])
async def test_omp_child_inherits_dynamically_attached_root_deny(
    policy_client: httpx.AsyncClient, db_uri: str, phase: str
) -> None:
    client = policy_client
    parent = await _omp(client)
    body = {"native_session_id": "native-child", "agent_id": "advisor", "name": "advisor"}
    first = await client.post(f"/v1/sessions/{parent}/hooks/omp-child", json=body)
    assert first.status_code == 200, first.text
    child = first.json()["session_id"]
    repeated = await client.post(f"/v1/sessions/{parent}/hooks/omp-child", json=body)
    assert repeated.json()["session_id"] == child
    store = SqlAlchemyConversationStore(db_uri)
    row = store.get_conversation(child)
    assert row is not None and row.parent_conversation_id == parent
    assert row.root_conversation_id == parent
    assert row.external_session_id == "native-child"
    attached = await client.post(
        f"/v1/sessions/{parent}/policies",
        json={
            "name": "deny_native",
            "type": "python",
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "factory_params": {
                "expression": (
                    'event.type in ["tool_call", "tool_result"] ? '
                    '{"result": "DENY", "reason": "Root policy applies to OMP children"} '
                    ': {"result": "ALLOW"}'
                )
            },
        },
    )
    assert attached.status_code < 300, attached.text
    event = {
        "type": phase,
        "target": "read",
        "data": {"name": "read", "arguments": {}}
        if phase == "PHASE_TOOL_CALL"
        else {"result": "private"},
        "request_data": {"name": "read", "arguments": {}},
    }
    evaluated = await client.post(f"/v1/sessions/{child}/policies/evaluate", json={"event": event})
    assert evaluated.status_code == 200, evaluated.text
    assert evaluated.json()["result"] == "POLICY_ACTION_DENY"
    assert evaluated.json()["reason"] == "Root policy applies to OMP children"
    # Resource discovery belongs on the parent; child conversational content does not.
    assert all(item.type == "resource_event" for item in store.list_items(parent).data)


@pytest.mark.parametrize("previous", ["old", None])
async def test_omp_transition_cas_replaces_active_context_and_retains_audit(
    client: httpx.AsyncClient, db_uri: str, previous: str | None
) -> None:
    parent = await _omp(client)
    store = SqlAlchemyConversationStore(db_uri)
    if previous is not None:
        store.set_external_session_id(parent, previous)
    body = {
        "previous_session_id": previous,
        "native_session_id": "new",
        "transition_id": "a" * 32,
        "compacted_messages": [],
        "model": None,
    }
    transition = await client.post(f"/v1/sessions/{parent}/hooks/omp-session", json=body)
    assert transition.status_code == 200, transition.text
    assert store.get_conversation(parent).external_session_id == "new"
    repeated = await client.post(f"/v1/sessions/{parent}/hooks/omp-session", json=body)
    assert repeated.status_code == 200, repeated.text
    checkpoints = store.list_items(parent, type="compaction").data
    assert len(checkpoints) == 1
    assert checkpoints[0].data.compacted_messages == []
    stale = await client.post(
        f"/v1/sessions/{parent}/hooks/omp-session",
        json={**body, "native_session_id": "stale", "transition_id": "b" * 32},
    )
    assert stale.status_code == 409, stale.text
    assert store.get_conversation(parent).external_session_id == "new"


async def test_other_harness_cannot_register_omp_child_or_transition(
    client: httpx.AsyncClient,
) -> None:
    agent = await create_test_agent(client)
    parent = agent["_session_id"]
    child = await client.post(
        f"/v1/sessions/{parent}/hooks/omp-child", json={"native_session_id": "n", "agent_id": "a"}
    )
    assert child.status_code == 400
    transition = await client.post(
        f"/v1/sessions/{parent}/hooks/omp-session",
        json={
            "previous_session_id": "n",
            "native_session_id": "m",
            "transition_id": "t",
            "compacted_messages": [],
        },
    )
    assert transition.status_code == 400
