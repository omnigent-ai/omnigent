"""
End-to-end integration test for the ASK policy approve/refuse lifecycle.

Exercises the full user journey:

1. Create a session.
2. Attach an ASK policy via ``POST /v1/sessions/{session_id}/policies``
   using the registered ``ask_on_os_tools`` handler.
3. Trigger policy evaluation via ``POST /v1/sessions/{id}/policies/evaluate``
   with a Bash tool call.
4. Observe the parked elicitation in the session snapshot.
5. Resolve with accept (approve) or decline (refuse) via
   ``POST /v1/sessions/{id}/elicitations/{eid}/resolve``.
6. Assert the evaluate endpoint returns ``POLICY_ACTION_ALLOW`` (approve)
   or ``POLICY_ACTION_DENY`` (refuse).

Uses the shared ``runtime_init`` and ``mock_llm`` fixtures from
``tests/server/conftest.py``, plus a per-module policy-enabled app
fixture that wires in a :class:`SqlAlchemyPolicyStore` so the session
policy CRUD routes are mounted and the evaluate endpoint picks up
session-attached policies.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime import pending_elicitations, session_stream
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


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture()
def policy_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """
    FastAPI app with a policy store wired in.

    The standard ``app`` fixture from ``conftest.py`` omits the policy
    store, so the session policy CRUD routes are not mounted. This
    fixture adds one so ``POST /v1/sessions/{id}/policies`` and the
    evaluate endpoint's ``get_policy_store()`` both see session-attached
    policies.

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

    Mirrors the shared ``client`` fixture but targets the
    ``policy_app`` so session policy CRUD routes are available.
    Also patches the runtime's ``_policy_store`` global so the
    evaluate endpoint picks up session-attached policies.

    :param policy_app: The policy-enabled FastAPI app.
    :param mock_llm: Controllable mock LLM (released on teardown).
    :param tmp_path: Pytest temporary directory fixture.
    :param db_uri: Test database URI.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    # Wire the policy store into the runtime global so
    # ``get_policy_store()`` returns it during evaluate.
    from omnigent.runtime import _globals

    monkeypatch.setattr(_globals, "_policy_store", SqlAlchemyPolicyStore(db_uri))

    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


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


async def _attach_ask_policy(
    client: httpx.AsyncClient,
    session_id: str,
) -> str:
    """
    Attach the registered ``ask_on_os_tools`` ASK policy to a session.

    This builtin policy ASKs for approval on any file or shell tool
    call (Bash, Read, Write, Edit, Glob, Grep). Using a registered
    handler avoids the policy registry allowlist rejection.

    :param client: Test HTTP client.
    :param session_id: Session to attach the policy to.
    :returns: The created policy id.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "test_ask_policy",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
    )
    assert resp.status_code == 200, f"attach policy failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["name"] == "test_ask_policy"
    return body["id"]


def _tool_call_request(
    tool_name: str = "Bash",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a ``PHASE_TOOL_CALL`` policy-evaluate request.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :param arguments: Tool arguments dict.
    :returns: JSON body for ``POST /v1/sessions/{id}/policies/evaluate``.
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
    timeout_s: float = 5.0,
) -> str:
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


# ── Tests ───────────────────────────────────────────────────


async def test_ask_policy_approve_flow(
    client: httpx.AsyncClient,
) -> None:
    """
    Attach ASK policy, evaluate, approve → ALLOW.

    Full journey: create session → attach ``ask_on_os_tools`` policy
    → trigger evaluate with a Bash tool call → observe pending
    elicitation in the session snapshot → resolve with accept →
    evaluate returns ``POLICY_ACTION_ALLOW``. Proves the session-
    attached policy fires, parks a real server-side Future, and the
    URL-based resolve wakes it with the correct verdict.
    """
    agent = await create_test_agent(client, "test-ask-approve")
    session_id = await _create_session(client, agent["id"])
    await _attach_ask_policy(client, session_id)

    try:
        # Subscribe to the stream before triggering evaluation so the
        # elicitation event is not missed.
        drain = asyncio.create_task(_drain_elicitation_id(session_id))
        await asyncio.sleep(0.05)

        # The evaluate POST parks until the verdict arrives.
        evaluate = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json=_tool_call_request("Bash"),
            )
        )

        # Learn the elicitation id from the stream.
        elicitation_id = await drain

        # Verify the session snapshot shows a pending elicitation.
        snapshot = await client.get(f"/v1/sessions/{session_id}")
        assert snapshot.status_code == 200, snapshot.text
        pending = snapshot.json().get("pending_elicitations", [])
        pending_ids = [p["elicitation_id"] for p in pending]
        assert elicitation_id in pending_ids, (
            f"elicitation {elicitation_id} not in snapshot pending list: {pending_ids}"
        )

        # Approve.
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text

        # The parked evaluate call should now return ALLOW.
        resp = await evaluate
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "POLICY_ACTION_ALLOW"
    finally:
        pending_elicitations.reset_for_tests()


async def test_ask_policy_refuse_flow(
    client: httpx.AsyncClient,
) -> None:
    """
    Attach ASK policy, evaluate, refuse → DENY.

    Same setup as the approve flow but resolves with ``decline``.
    The evaluate endpoint must collapse the ASK to
    ``POLICY_ACTION_DENY`` — fail-closed. Proves the session-attached
    ASK policy's refuse path terminates correctly and the DENY
    sentinel propagates.
    """
    agent = await create_test_agent(client, "test-ask-refuse")
    session_id = await _create_session(client, agent["id"])
    await _attach_ask_policy(client, session_id)

    try:
        drain = asyncio.create_task(_drain_elicitation_id(session_id))
        await asyncio.sleep(0.05)

        evaluate = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json=_tool_call_request("Bash"),
            )
        )

        elicitation_id = await drain

        # Refuse.
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "decline"},
        )
        assert verdict.status_code == 202, verdict.text

        # The parked evaluate call should now return DENY.
        resp = await evaluate
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["result"] == "POLICY_ACTION_DENY"
    finally:
        pending_elicitations.reset_for_tests()
