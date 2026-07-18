"""Integration tests for runner capability route authorization.

Verifies that runner callbacks authenticated via binding tokens are
limited to an explicit action allow-list. Tests both allowed routes
(read session, read agent spec, append events, evaluate policy) and
forbidden routes (fork, patch, labels, delete, permission management)
under strict header/proxy auth mode — the deployment posture for
Databricks App hosts.

See specs/003-shared-external-host-access/contracts/runner-capability.md
for the action allow-list contract.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio

_BINDING_TOKEN = "route-cap-test-token"
_OTHER_TOKEN = "route-cap-other-token"
_USER = "alice@example.com"


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """Auth-enabled app in strict header mode (Databricks deployment posture)."""
    from omnigent.server.auth import UnifiedAuthProvider

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
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the auth-enabled app."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


async def _create_session(
    client: httpx.AsyncClient,
    user: str,
    db_uri: str,
    *,
    binding_token: str = _BINDING_TOKEN,
) -> tuple[str, str]:
    """Create a session as *user* and bind a runner via the binding token.

    Returns ``(session_id, binding_token)``.
    """
    bundle = build_agent_bundle(name="test-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": user},
    )
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    session_id = resp.json()["session_id"]

    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(user)
    perm_store.grant(user, session_id, LEVEL_OWNER)
    conv_store.replace_runner_id(session_id, token_bound_runner_id(binding_token))
    return session_id, binding_token


def _runner_headers(token: str | None) -> dict[str, str]:
    """Headers for a runner callback request (no X-Forwarded-Email)."""
    headers: dict[str, str] = {}
    if token is not None:
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
    return headers


# ── Allowed runner callbacks ─────────────────────────────────


async def test_runner_can_read_session_snapshot(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner with a matching binding token can read its session snapshot."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers=_runner_headers(token),
    )
    assert resp.status_code == 200, f"runner GET session failed: {resp.status_code} {resp.text}"
    assert resp.json()["id"] == session_id


async def test_runner_can_read_session_agent(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner can fetch its bound agent definition."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/agent",
        headers=_runner_headers(token),
    )
    assert resp.status_code == 200, f"runner GET agent failed: {resp.status_code} {resp.text}"


async def test_runner_can_read_session_agent_contents(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner can download its bound agent bundle."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/agent/contents",
        headers=_runner_headers(token),
    )
    assert resp.status_code == 200, (
        f"runner GET agent contents failed: {resp.status_code} {resp.text}"
    )


async def test_runner_can_post_runner_produced_events(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner can post runner-produced status events on its own session."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": "running"}},
        headers=_runner_headers(token),
    )
    assert resp.status_code == 202, f"runner POST event failed: {resp.status_code} {resp.text}"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "message", "data": {"role": "user", "content": "injected"}},
        {"type": "interrupt", "data": {}},
        {"type": "slash_command", "data": {}},
    ],
)
async def test_runner_cannot_submit_human_events(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    body: dict[str, object],
) -> None:
    """A runner token cannot impersonate a human through the shared event route."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=body,
        headers=_runner_headers(token),
    )
    assert resp.status_code == 403, (
        f"runner human event should be denied; got {resp.status_code}: {resp.text}"
    )


async def test_runner_can_evaluate_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner can call the policy evaluation endpoint."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    body = {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "data": {"tool_name": "test_tool", "tool_input": "{}"},
        },
    }
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=body,
        headers={
            **_runner_headers(token),
            "Content-Type": "application/json",
        },
    )
    # 200 = authorized and processed; 400 = authorized but body rejected.
    # 401/403/404 = authorization failed (the regression we guard against).
    assert resp.status_code in (200, 400), (
        f"runner evaluate-policy should be authorized; got {resp.status_code}: {resp.text}"
    )


# ── Forbidden routes: runner token must not confer general access ──


async def test_runner_cannot_fork_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner cannot fork a session — fork is not a runner action."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/fork",
        json={},
        headers=_runner_headers(token),
    )
    assert resp.status_code in (401, 403, 404), (
        f"runner fork should be denied; got {resp.status_code}: {resp.text}"
    )


async def test_runner_cannot_access_session_labels(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner cannot list session labels — not a runner action."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/labels",
        headers=_runner_headers(token),
    )
    assert resp.status_code in (401, 403, 404), (
        f"runner GET labels should be denied; got {resp.status_code}: {resp.text}"
    )


async def test_runner_cannot_delete_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner cannot delete a session."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.delete(
        f"/v1/sessions/{session_id}",
        headers=_runner_headers(token),
    )
    assert resp.status_code in (401, 403, 404), (
        f"runner DELETE session should be denied; got {resp.status_code}: {resp.text}"
    )


async def test_runner_cannot_grant_permissions(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner cannot manage session permissions."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": "bob@example.com", "level": 1},
        headers=_runner_headers(token),
    )
    assert resp.status_code in (401, 403, 404), (
        f"runner PUT permissions should be denied; got {resp.status_code}: {resp.text}"
    )


async def test_runner_cannot_revoke_permissions(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner cannot revoke session permissions."""
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.delete(
        f"/v1/sessions/{session_id}/permissions/bob@example.com",
        headers=_runner_headers(token),
    )
    assert resp.status_code in (401, 403, 404), (
        f"runner DELETE permissions should be denied; got {resp.status_code}: {resp.text}"
    )


# ── Cross-session isolation ──────────────────────────────────


async def test_runner_denied_on_other_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A runner token bound to session A grants nothing on session B."""
    _session_a, token_a = await _create_session(auth_client, _USER, db_uri)
    session_b, token_b = await _create_session(
        auth_client, _USER, db_uri, binding_token=_OTHER_TOKEN
    )

    # Token A cannot read session B.
    resp = await auth_client.get(
        f"/v1/sessions/{session_b}",
        headers=_runner_headers(token_a),
    )
    assert resp.status_code in (401, 403, 404), (
        f"runner token A on session B should be denied; got {resp.status_code}"
    )

    # Token B can read session B (control).
    resp = await auth_client.get(
        f"/v1/sessions/{session_b}",
        headers=_runner_headers(token_b),
    )
    assert resp.status_code == 200, (
        f"runner token B on session B should succeed; got {resp.status_code}"
    )


# ── No-token and invalid-token regression ────────────────────


async def test_no_token_no_user_returns_401(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Without a binding token or user identity, the request is rejected."""
    session_id, _ = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 401, f"no token + no user should be 401; got {resp.status_code}"


async def test_invalid_token_no_user_returns_401(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An invalid binding token with no user identity falls through to 401."""
    session_id, _ = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers=_runner_headers("bogus-token"),
    )
    assert resp.status_code == 401, (
        f"invalid token + no user should be 401; got {resp.status_code}"
    )


async def test_valid_user_with_bogus_token_unaffected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A real user's access is unaffected by a bogus runner token."""
    session_id, _ = await _create_session(auth_client, _USER, db_uri)

    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={
            "X-Forwarded-Email": _USER,
            RUNNER_TUNNEL_TOKEN_HEADER: "bogus-token",
        },
    )
    assert resp.status_code == 200, (
        f"valid user with bogus token should succeed; got {resp.status_code}: {resp.text}"
    )


# ── Attribution ──────────────────────────────────────────────


async def test_runner_event_attribution_not_human(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Events posted via a runner token are not attributed to a human user.

    The runner has no user identity, so ``created_by`` on persisted items
    must be ``None`` (system/runner attribution), never the session owner.
    """
    session_id, token = await _create_session(auth_client, _USER, db_uri)

    # Post an external assistant message via the runner token.
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "test-agent",
                "text": "runner says hi",
            },
        },
        headers=_runner_headers(token),
    )
    assert resp.status_code == 202, f"runner POST event failed: {resp.status_code} {resp.text}"

    # Fetch the session items and verify attribution.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/items",
        headers={"X-Forwarded-Email": _USER},
    )
    assert resp.status_code == 200, f"GET items failed: {resp.status_code} {resp.text}"
    body = resp.json()
    items = body.get("data", []) if isinstance(body, dict) else body
    for item in items:
        if not isinstance(item, dict):
            continue
        created_by = item.get("created_by")
        assert created_by != _USER, (
            f"runner-posted item attributed to human user {_USER!r}; "
            f"runner events must use system/runner attribution"
        )


# ── Route inventory: each runner callback declares a RunnerAction ──


async def test_sessions_route_module_imports_runner_action() -> None:
    """The sessions route module must import and use RunnerAction.

    Ensures runner callbacks go through the centralized capability
    system instead of ad-hoc binding-token permission shortcuts.
    """
    module_name = "omnigent.server.routes.sessions"
    assert importlib.util.find_spec(module_name) is not None

    module = importlib.import_module(module_name)
    # The module must import RunnerAction from the capability module.
    assert hasattr(module, "RunnerAction"), (
        "sessions route module must import RunnerAction from omnigent.server.runner_capabilities"
    )
