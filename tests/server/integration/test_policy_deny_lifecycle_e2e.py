"""Integration tests for the DENY policy attach/remove lifecycle.

Exercises the full user journey:

1. Create a session with an agent.
2. Attach a DENY policy via ``POST /v1/sessions/{session_id}/policies``.
3. Send a user message — verify the DENY fires (synchronous inline verdict).
4. Remove the policy via ``DELETE``.
5. Send another message — verify the mock LLM responds normally.
6. Verify the policy is gone from the list endpoint.

Also covers phase-scoping: a DENY policy attached on ``tool_call`` phase
only must not block ``input`` (REQUEST) phase messages.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

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
    """App with a ``policy_store`` so session-policy routes are active.

    Uses no auth provider — the session policy routes fall through to
    the unauthenticated path (no permission checks), which is sufficient
    for testing the policy engine wiring.

    :param runtime_init: Initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A FastAPI app with policy CRUD and session routes.
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
    """Async HTTP client wired to the policy-enabled app.

    Also patches the runtime-global ``_policy_store`` so the
    ``_build_policy_engine_from_spec`` path (which reads the store via
    ``get_policy_store()``) sees session-scoped policies created
    through the CRUD routes.

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

    # Patch the runtime global so the policy engine picks up session policies.
    policy_store = SqlAlchemyPolicyStore(db_uri)
    monkeypatch.setattr("omnigent.runtime._globals._policy_store", policy_store)

    # Allow the make_fixed_action_callable factory through the registry
    # allowlist. In production this would be added via policy_modules config.
    # Patch at the use site (the route module imports the function directly).
    from omnigent.server.routes import session_policies as _sp_mod

    _original_is_registered = _sp_mod.is_registered_handler
    monkeypatch.setattr(
        _sp_mod,
        "is_registered_handler",
        lambda handler: (
            handler == "omnigent.policies.function.make_fixed_action_callable"
            or _original_is_registered(handler)
        ),
    )

    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """Create a session bound to an agent and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _attach_deny_policy(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    name: str = "test_deny_policy",
    reason: str = "Blocked by test policy",
    factory_params: dict | None = None,
) -> str:
    """Attach a DENY policy to a session and return its policy id.

    :param client: Test HTTP client.
    :param session_id: Session to attach the policy to.
    :param name: Policy name.
    :param reason: Deny reason.
    :param factory_params: Override factory params if needed.
    :returns: The created policy id.
    """
    params = {"action": "deny", "reason": reason} if factory_params is None else factory_params
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": name,
            "type": "python",
            "handler": "omnigent.policies.function.make_fixed_action_callable",
            "factory_params": params,
        },
    )
    assert resp.status_code == 200, f"policy create failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["id"].startswith("pol_")
    return body["id"]


async def _send_user_message(
    client: httpx.AsyncClient,
    session_id: str,
    text: str,
) -> httpx.Response:
    """Post a user message event and return the raw response.

    :param client: Test HTTP client.
    :param session_id: Target session.
    :param text: Message text.
    :returns: The raw HTTP response.
    """
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_deny_policy_lifecycle(
    policy_client: httpx.AsyncClient,
    mock_llm: ControllableMockClient,
) -> None:
    """Full DENY lifecycle: attach -> get rejected -> remove -> get through.

    1. Create a session with an agent.
    2. Attach a DENY policy via the session policies endpoint.
    3. Send a user message — verify the DENY fires synchronously.
    4. Remove the policy via DELETE.
    5. Send another message — verify the mock LLM responds normally.
    6. Verify the policy is gone from the list endpoint.
    """
    agent = await create_test_agent(policy_client)
    session_id = await _create_session(policy_client, agent["id"])

    # ── Step 2: attach DENY policy ──
    policy_id = await _attach_deny_policy(policy_client, session_id)

    # ── Step 3: send message, expect DENY ──
    resp_denied = await _send_user_message(
        policy_client, session_id, "Hello, this should be blocked."
    )
    assert resp_denied.status_code == 202, (
        f"expected 202 from events endpoint; got {resp_denied.status_code} {resp_denied.text}"
    )
    verdict = resp_denied.json()
    assert verdict.get("denied") is True, f"expected synchronous DENY verdict; got {verdict}"
    assert "Blocked by test policy" in verdict.get("reason", ""), (
        f"expected deny reason to contain 'Blocked by test policy'; got {verdict}"
    )

    # ── Step 4: remove the policy ──
    del_resp = await policy_client.delete(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
    )
    assert del_resp.status_code == 200, (
        f"policy delete failed: {del_resp.status_code} {del_resp.text}"
    )
    assert del_resp.json()["deleted"] is True

    # ── Step 5: send another message, expect it passes policy ──
    resp_allowed = await _send_user_message(
        policy_client, session_id, "Hello, this should go through."
    )
    # After policy removal the message must NOT be denied by policy.
    # It may return 202 (queued) or 503 (no runner bound) — both prove
    # the policy layer allowed it through.
    assert resp_allowed.status_code in {202, 503}, (
        f"expected 202 or 503 after policy removal; "
        f"got {resp_allowed.status_code} {resp_allowed.text}"
    )
    body = resp_allowed.json()
    # A synchronous DENY verdict ({"denied": true}) would mean the policy is still active.
    assert body.get("denied") is not True, f"message was denied after policy removal; got {body}"

    # ── Step 6: verify the policy list is empty ──
    list_resp = await policy_client.get(f"/v1/sessions/{session_id}/policies")
    assert list_resp.status_code == 200
    policies = list_resp.json()["data"]
    session_policies = [p for p in policies if p.get("source") == "session"]
    assert len(session_policies) == 0, (
        f"expected no session policies after deletion; got {session_policies}"
    )


async def test_deny_policy_only_blocks_matching_phase(
    policy_client: httpx.AsyncClient,
    mock_llm: ControllableMockClient,
) -> None:
    """A DENY policy scoped to ``tool_call`` phase does not block input messages.

    1. Attach a DENY policy that fires only on ``tool_call`` events.
    2. Send a user message (INPUT/REQUEST phase) — verify it goes through.

    This proves that phase-scoping in ``make_fixed_action_callable``
    correctly causes the callable to abstain (return ``None``) on
    non-matching phases, which the engine coerces to ALLOW.
    """
    agent = await create_test_agent(policy_client)
    session_id = await _create_session(policy_client, agent["id"])

    # Attach DENY on tool_call only.
    await _attach_deny_policy(
        policy_client,
        session_id,
        name="deny_tool_call_only",
        factory_params={
            "action": "deny",
            "reason": "Tool calls are blocked",
            "on_phases": ["tool_call"],
        },
    )

    # Send user message (REQUEST phase) — should NOT be denied.
    # May return 202 (queued) or 503 (no runner) — both prove the
    # policy layer allowed it through; only {"denied": true} is a failure.
    resp = await _send_user_message(policy_client, session_id, "Hello, this should go through.")
    assert resp.status_code in {202, 503}, (
        f"expected 202 or 503; got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body.get("denied") is not True, (
        f"tool_call-only DENY policy incorrectly blocked an input message; got {body}"
    )
