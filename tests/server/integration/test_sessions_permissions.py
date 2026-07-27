"""Integration tests for session permission endpoints.

Exercises the full middleware -> route -> store pipeline for the
permission management endpoints on ``/v1/sessions/{id}/permissions``
and the access-control enforcement on session CRUD endpoints when
a :class:`PermissionStore` is active.

Uses a custom ``auth_app`` / ``auth_client`` fixture pair that wires
a :class:`SqlAlchemyPermissionStore` into the FastAPI app so the
``UnifiedAuthProvider`` and permission checks are active. Requests
include ``X-Forwarded-Email`` headers to impersonate different users.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.host.frames import HostHelloFrame
from omnigent.runtime import session_stream
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import presence
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import (
    build_agent_bundle,
    create_test_agent,
    start_session_stream_collector,
)

pytestmark = pytest.mark.asyncio


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App fixture with permission store enabled.

    Mirrors the shared ``app`` fixture from ``conftest.py`` but adds
    a :class:`SqlAlchemyPermissionStore` so
    :class:`UnifiedAuthProvider` and permission checks are active on
    all session routes.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    """
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
        # Explicit strict header mode (the deployed multi-user
        # posture): requests without X-Forwarded-Email are rejected
        # with 401. Constructed directly rather than via
        # create_auth_provider() so ambient OMNIGENT_* env vars in
        # the test runner can't flip the mode.
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the auth-enabled FastAPI app.

    Same lifecycle pattern as the shared ``client`` fixture from
    ``conftest.py``: starts the harness process manager, yields the
    client, then tears down DBOS on exit.
    """
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


@pytest.fixture()
def local_auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """App fixture modeling the explicit single-user local runtime.

    Same wiring as :func:`auth_app` but with
    ``local_single_user=True`` (the posture of a server spawned with
    ``OMNIGENT_LOCAL_SINGLE_USER=1``): requests without
    ``X-Forwarded-Email`` resolve to the reserved ``"local"``
    identity instead of being rejected.

    :param runtime_init: Fixture that initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    """
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
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
    )


@pytest_asyncio.fixture()
async def local_auth_client(
    local_auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the single-user-mode FastAPI app.

    Same lifecycle pattern as :func:`auth_client`.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=local_auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


@pytest.fixture()
def host_perm_app(
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """Auth-enabled app that also wires a host store.

    Same shape as :func:`auth_app` but passes ``host_store`` so the
    host-launch authorization path in ``POST /v1/sessions`` is live.
    """
    from omnigent.server.auth import create_auth_provider

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
        auth_provider=create_auth_provider(),
        host_store=HostStore(db_uri),
    )


@pytest_asyncio.fixture()
async def host_perm_client(
    host_perm_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client for the host-enabled auth app (mirrors ``auth_client``)."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=host_perm_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── Helpers ──────────────────────────────────────────────────


def _register_online_host(app: FastAPI, host_id: str, owner: str) -> None:
    """Register an online host owned by ``owner`` on the app.

    Persists the host (so the owner check can resolve it) and registers
    a no-op WebSocket in the app's live registry (so the session-create
    flow sees the host as online and would send it a ``host.stat`` if
    the ownership check were missing). The registry only needs an object
    exposing ``send_text``/``receive_text``.

    :param app: The app whose ``host_store``/``host_registry`` to use.
    :param host_id: Host id to register, e.g. ``"f54bb9272002938a3a934bfcb6bb228a"``.
    :param owner: Owning user, e.g. ``"alice@example.com"``.
    """
    app.state.host_store.upsert_on_connect(host_id, f"{owner}-laptop", owner)
    app.state.host_registry.register(
        host_id,
        type(
            "FakeWS",
            (),
            {"send_text": lambda self, d: None, "receive_text": lambda self: ""},
        )(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name=host_id),
        owner=owner,
    )


async def _create_session_as(
    client: httpx.AsyncClient,
    agent_id: str,
    user: str | None,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Create a session as a specific user.

    Uses multipart bundled create so each session gets its own
    session-scoped agent. The ``agent_id`` parameter is accepted
    for backward compatibility but ignored — sessions always
    create a fresh agent from the test bundle.

    :param client: The test HTTP client.
    :param agent_id: Ignored — kept for call-site compatibility.
    :param user: User identity for ``X-Forwarded-Email``, or
        ``None`` to omit the header (falls back to ``"local"``
        in header mode).
    :param title: Optional session title.
    :returns: A dict with ``id`` (session_id), ``agent_id``, and
        other session fields.
    """
    import json as _json

    bundle = build_agent_bundle(name="test-agent")
    metadata: dict[str, Any] = {}
    if title is not None:
        metadata["title"] = title
    headers = {"X-Forwarded-Email": user} if user is not None else {}
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=headers,
    )
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    # Bundled create returns {session_id: "..."} — fetch the full
    # snapshot so callers have the same shape as before.
    session_id = resp.json()["session_id"]
    snap = await client.get(
        f"/v1/sessions/{session_id}",
        headers=headers,
    )
    assert snap.status_code == 200, f"session snapshot failed: {snap.text}"
    return snap.json()


async def _grant_permission(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    granter: str,
    target_user: str,
    level: int,
) -> httpx.Response:
    """Grant a permission on a session.

    :param client: The test HTTP client.
    :param session_id: Session to grant access to.
    :param granter: User identity of the granter.
    :param target_user: User to receive the grant.
    :param level: Numeric permission level (1/2/3).
    :returns: The raw httpx response.
    """
    return await client.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": target_user, "level": level},
        headers={"X-Forwarded-Email": granter},
    )


async def _revoke_permission(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    revoker: str,
    target_user: str,
) -> httpx.Response:
    """Revoke a permission on a session.

    :param client: The test HTTP client.
    :param session_id: Session to revoke access from.
    :param revoker: User identity of the revoker.
    :param target_user: User whose grant to revoke.
    :returns: The raw httpx response.
    """
    return await client.delete(
        f"/v1/sessions/{session_id}/permissions/{target_user}",
        headers={"X-Forwarded-Email": revoker},
    )


async def _list_sessions_as(
    client: httpx.AsyncClient,
    user: str,
) -> list[dict[str, Any]]:
    """List sessions visible to a specific user.

    :param client: The test HTTP client.
    :param user: User identity for ``X-Forwarded-Email``.
    :returns: List of session dicts from the ``data`` field.
    """
    resp = await client.get(
        "/v1/sessions",
        headers={"X-Forwarded-Email": user},
    )
    assert resp.status_code == 200, f"list sessions failed: {resp.status_code} {resp.text}"
    return resp.json()["data"]


async def _list_permissions(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    user: str | None,
) -> httpx.Response:
    """List permissions on a session.

    :param client: The test HTTP client.
    :param session_id: Session to list grants for.
    :param user: User identity for ``X-Forwarded-Email``, or
        ``None`` to omit the header (falls back to ``"local"``).
    :returns: The raw httpx response.
    """
    headers = {"X-Forwarded-Email": user} if user is not None else {}
    return await client.get(
        f"/v1/sessions/{session_id}/permissions",
        headers=headers,
    )


# ── Critical CUJ: full grant/revoke/list lifecycle ──────────


async def test_full_permission_lifecycle(
    auth_client: httpx.AsyncClient,
) -> None:
    """Full permission lifecycle: grant, downgrade, revoke, self-revoke
    block, and visibility in session list.

    Steps:
    1. bryan creates session S1 -> gets manage grant automatically
    2. bryan grants corey edit (level 2) on S1
    3. bryan reassigns corey to read (level 1) via upsert
    4. bryan grants rice edit (level 2) on S1
    5. bryan revokes corey entirely
    6. bryan tries to revoke own manage -> 403 (self-revoke blocked)
    7. Verify DB state: only bryan (manage) and rice (edit)
    8. rice lists sessions -> sees S1
    9. corey lists sessions -> sees nothing
    10. bryan lists sessions -> sees S1
    """
    agent = await create_test_agent(auth_client, user="bryan")

    # Step 1: bryan creates session -> auto-gets manage
    s1 = await _create_session_as(
        auth_client,
        agent["id"],
        "bryan",
        title="lifecycle-test",
    )
    session_id = s1["id"]

    # Step 2: bryan grants corey edit (level 2)
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_EDIT,
    )
    # Grant endpoint returns 200 with the permission object.
    assert resp.status_code == 200, f"grant failed: {resp.status_code} {resp.text}"
    grant_body = resp.json()
    assert grant_body["user_id"] == "corey", "Grant response should echo the target user_id."
    assert grant_body["level"] == LEVEL_EDIT, "Grant response should echo the requested level."

    # Step 3: bryan downgrades corey to read (level 1) via upsert
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200
    # Upsert should return the new level, not the old one.
    assert resp.json()["level"] == LEVEL_READ, (
        "Upsert downgrade should reflect the new (lower) level. "
        "If still 2, the upsert did not overwrite."
    )

    # Step 4: bryan grants rice edit (level 2)
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="rice",
        level=LEVEL_EDIT,
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "rice"
    assert resp.json()["level"] == LEVEL_EDIT

    # Step 5: bryan revokes corey entirely
    resp = await _revoke_permission(
        auth_client,
        session_id,
        revoker="bryan",
        target_user="corey",
    )
    # Revoke returns 204 No Content.
    assert resp.status_code == 204, f"revoke failed: {resp.status_code} {resp.text}"

    # Step 6: bryan tries to revoke own manage -> 403
    resp = await _revoke_permission(
        auth_client,
        session_id,
        revoker="bryan",
        target_user="bryan",
    )
    # Self-revoke of a manage grant is blocked to prevent orphaned
    # sessions. The route returns 403 (FORBIDDEN).
    assert resp.status_code == 403, (
        f"Expected 403 for self-revoke of manage grant, "
        f"got {resp.status_code}. If 204, the self-revoke guard "
        f"is broken and the session could be orphaned."
    )

    # Step 7: verify DB state via list_permissions
    resp = await _list_permissions(
        auth_client,
        session_id,
        user="bryan",
    )
    assert resp.status_code == 200
    grants = resp.json()["permissions"]
    grant_map = {g["user_id"]: g["level"] for g in grants}
    # Only bryan (owner) and rice (edit) should remain.
    # corey was revoked in step 5.
    assert grant_map == {"bryan": LEVEL_OWNER, "rice": LEVEL_EDIT}, (
        f"Expected exactly bryan=owner(4) and rice=edit(2), "
        f"got {grant_map}. If corey is present, the revoke in "
        f"step 5 failed silently."
    )

    # Step 8: rice lists sessions -> sees S1
    rice_sessions = await _list_sessions_as(auth_client, "rice")
    rice_ids = {s["id"] for s in rice_sessions}
    assert session_id in rice_ids, (
        "rice has an edit grant and should see S1 in the session list. "
        "If absent, the list_conversations accessible_by filter is "
        "not including rice's grant."
    )

    # Step 9: corey lists sessions -> sees nothing
    corey_sessions = await _list_sessions_as(auth_client, "corey")
    corey_ids = {s["id"] for s in corey_sessions}
    assert session_id not in corey_ids, (
        "corey's grant was revoked and should NOT see S1. If present, "
        "the revoke did not propagate to the list filter."
    )

    # Step 10: bryan lists sessions -> sees S1
    bryan_sessions = await _list_sessions_as(auth_client, "bryan")
    bryan_ids = {s["id"] for s in bryan_sessions}
    assert session_id in bryan_ids, (
        "bryan has a manage grant and should see S1 in the session list."
    )


# ── Visibility: no grants -> no sessions ─────────────────────


async def test_user_without_grants_sees_no_sessions(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user with no grants sees an empty session list."""
    agent = await create_test_agent(auth_client, user="bryan")
    await _create_session_as(auth_client, agent["id"], "user-a", title="private")

    # user-b has never been granted anything.
    sessions = await _list_sessions_as(auth_client, "user-b")
    assert sessions == [], (
        "user-b has no grants and should see an empty session list. "
        "If non-empty, the accessible_by filter is not enforced or "
        "sessions without grants are visible by default."
    )


# ── Grant read: can GET but not POST events ──────────────────


async def test_read_grant_allows_get_but_blocks_post_events(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user with read-only access can GET a session but cannot POST events."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # user-a grants user-b read access.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # user-b can GET the session snapshot.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 200, (
        f"user-b with read grant should be able to GET the session, got {resp.status_code}."
    )
    # Verify the response contains the session id to confirm the
    # full pipeline returned real data (not just a status code).
    assert resp.json()["id"] == session_id

    # user-b cannot POST events (requires edit level).
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "blocked"}],
            },
        },
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 403, (
        f"user-b with read-only grant should be blocked from POST events "
        f"(requires edit). Got {resp.status_code}. If 202, the "
        f"permission check on POST /events is missing or not enforcing "
        f"LEVEL_EDIT."
    )


async def test_edit_grant_blocked_from_stop_session_requires_owner(
    auth_client: httpx.AsyncClient,
) -> None:
    """An editor can post ordinary events but cannot stop the session.

    ``stop_session`` terminates the whole running session for every
    participant — a lifecycle action on par with delete — so the
    route requires owner level on top of the LEVEL_EDIT gate that
    covers ordinary events. This pins that a shared collaborator with
    edit access (who CAN post messages / interrupt) is still blocked
    from killing the owner's session.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # user-a grants user-b edit access.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_EDIT,
    )
    assert resp.status_code == 200

    # Sanity: the edit grant DOES let user-b post an ordinary event
    # (interrupt requires only LEVEL_EDIT). If this 403s, the setup is
    # wrong and the stop_session 403 below wouldn't prove the extra
    # owner gate — it'd just be the edit gate failing.
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "interrupt", "data": {}},
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 202, (
        f"user-b with edit grant should be able to post interrupt "
        f"(LEVEL_EDIT), got {resp.status_code}: {resp.text}"
    )

    # The actual claim: user-b is blocked from stop_session (owner-only).
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 403, (
        f"user-b with edit grant should be blocked from stop_session "
        f"(requires owner). Got {resp.status_code}. If 202, the owner "
        f"gate on stop_session is missing — an editor could kill the "
        f"owner's session."
    )

    # The owner (user-a) is NOT blocked by the gate. No runner is bound
    # in this app, so the forward is a best-effort no-op and the route
    # returns 202 — what matters is that it's not a 403.
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
        headers={"X-Forwarded-Email": "user-a"},
    )
    assert resp.status_code == 202, (
        f"The session owner should pass the stop_session owner gate "
        f"and get 202, got {resp.status_code}: {resp.text}"
    )


# ── Grant edit: can POST events but not manage permissions ───


async def test_edit_grant_allows_post_but_blocks_permission_management(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user with edit access can POST events but cannot manage permissions."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # user-a grants user-b edit access.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_EDIT,
    )
    assert resp.status_code == 200

    # user-b can PATCH the session title (requires edit).
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "updated by user-b"},
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 200, (
        "user-b with edit grant should be able to PATCH the session title, "
        f"got {resp.status_code}."
    )
    assert resp.json()["title"] == "updated by user-b", (
        "PATCH should reflect the new title in the response."
    )

    # user-b cannot grant permissions (requires manage).
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-b",
        target_user="user-c",
        level=LEVEL_READ,
    )
    assert resp.status_code == 403, (
        f"user-b with edit grant should be blocked from granting "
        f"permissions (requires manage). Got {resp.status_code}. If 200, "
        f"the LEVEL_MANAGE check on PUT /permissions is broken."
    )

    # user-b cannot list permissions (requires manage).
    resp = await _list_permissions(
        auth_client,
        session_id,
        user="user-b",
    )
    assert resp.status_code == 403, (
        f"user-b with edit grant should be blocked from listing "
        f"permissions. Got {resp.status_code}."
    )


async def test_archive_requires_owner_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Archiving a session is gated owner-only: a read-only viewer and
    an editor are both blocked; only the owner succeeds.

    Archiving stops the session (an owner-gated lifecycle action), so a
    shared editor must not be able to kill the owner's running agent by
    archiving — and a viewer must not be able to hide a shared session
    from everyone else. Both non-owner arms must 403; the owner arm
    confirms the gate isn't accidentally raised above owner.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # user-a grants user-b READ only — read-only cannot archive.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 403, (
        f"read-only user-b must not archive a shared session. Got {resp.status_code}."
    )

    # Upgrade user-b to EDIT — still blocked, archiving is owner-only.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_EDIT,
    )
    assert resp.status_code == 200
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 403, (
        f"editor user-b must not archive a shared session (archive is "
        f"owner-only). Got {resp.status_code}. If 200, the gate is "
        f"wrongly left at edit and an editor can stop the owner's runner."
    )

    # The session is still not archived — the denied PATCHes had no effect.
    snap = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "user-a"},
    )
    assert snap.json()["archived"] is False, "denied archive must not mutate the session"

    # The owner (user-a) can archive.
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
        headers={"X-Forwarded-Email": "user-a"},
    )
    assert resp.status_code == 200, (
        f"the owner should be able to archive; got {resp.status_code}. "
        f"If 403, the gate is wrongly raised above owner."
    )
    assert resp.json()["archived"] is True


async def test_cost_control_override_patch_requires_edit_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """The cost-control switch rides the PATCH route's edit gate.

    A read-only collaborator must not be able to flip another user's
    session out of (or into) cost-optimized mode — that changes how
    the owner's turns execute and what they cost. Edit access (the
    same level as title / model_override updates) suffices.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # user-a grants user-b READ only — blocked from the PATCH.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"cost_control_mode_override": "off"},
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 403, (
        f"read-only user-b must not set cost_control_mode_override on a "
        f"shared session. Got {resp.status_code}. If 200, the PATCH "
        f"edit gate is not covering the new field."
    )

    # The denied PATCH had no effect — the owner still sees unset.
    snap = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "user-a"},
    )
    assert snap.json()["cost_control_mode_override"] is None, (
        "denied PATCH must not mutate the session row"
    )

    # Upgrade user-b to EDIT — the switch is an edit-level field
    # (same gate as title / model_override), so the PATCH now lands.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_EDIT,
    )
    assert resp.status_code == 200
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"cost_control_mode_override": "off"},
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 200, (
        f"editor user-b should be able to set the switch; got "
        f"{resp.status_code}. If 403, the gate is wrongly raised "
        f"above edit."
    )
    assert resp.json()["cost_control_mode_override"] == "off"


# ── Public access via __public__ sentinel ────────────────────


async def test_public_grant_hides_from_list_but_allows_direct_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """A __public__ read grant does NOT list the session, but direct GET works."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(
        auth_client,
        agent["id"],
        "user-a",
        title="public-session",
    )
    session_id = s1["id"]

    # user-a grants __public__ read.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="__public__",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # user-b (no direct grant) should NOT see the session in the list —
    # public-only sessions are excluded from the sidebar.
    sessions = await _list_sessions_as(auth_client, "user-b")
    session_ids = {s["id"] for s in sessions}
    assert session_id not in session_ids, (
        "A __public__-only session should not appear in another user's "
        "session list. Public sessions are accessible by direct URL only."
    )

    # user-b CAN still GET the session directly.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 200, (
        f"user-b should be able to GET a session with __public__ read "
        f"grant, got {resp.status_code}."
    )
    assert resp.json()["id"] == session_id


async def test_get_snapshot_reports_resolved_permission_level(
    auth_client: httpx.AsyncClient,
) -> None:
    """The GET-snapshot ``permission_level`` reflects the resolved level
    for the caller — owner, direct grant, and public fallback.

    Exercises the ``require_access_and_level`` → ``_get_session_snapshot``
    level threading end-to-end through the route (the other snapshot
    permission tests only assert access, never the displayed level):

    - owner sees ``LEVEL_OWNER`` (own grant from session creation),
    - a direct READ grantee sees ``LEVEL_READ``,
    - a user with no own grant accessing via ``__public__`` sees the
      public grant level (``resolved_level`` falls back to it).
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # Owner: own grant from creation → LEVEL_OWNER.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "user-a"},
    )
    assert resp.status_code == 200, f"owner GET failed: {resp.status_code}."
    assert resp.json()["permission_level"] == LEVEL_OWNER, (
        f"owner snapshot should report LEVEL_OWNER, got {resp.json()['permission_level']}."
    )

    # Direct READ grantee: own grant → LEVEL_READ.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 200, f"grantee GET failed: {resp.status_code}."
    assert resp.json()["permission_level"] == LEVEL_READ, (
        f"READ-grantee snapshot should report LEVEL_READ, got {resp.json()['permission_level']}."
    )

    # Public fallback: user-c has no own grant; access + level come from
    # the __public__ READ grant (resolved_level falls back to it).
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="__public__",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "user-c"},
    )
    assert resp.status_code == 200, f"public-access GET failed: {resp.status_code}."
    assert resp.json()["permission_level"] == LEVEL_READ, (
        "public-only caller's snapshot should fall back to the public READ "
        f"grant level, got {resp.json()['permission_level']}."
    )


# ── List grants (GET /permissions) ───────────────────────────


async def test_list_permissions_shows_all_grants(
    auth_client: httpx.AsyncClient,
) -> None:
    """GET /sessions/{id}/permissions returns all grants for the session."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # Grant two additional users.
    await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )
    await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-c",
        level=LEVEL_EDIT,
    )

    resp = await _list_permissions(
        auth_client,
        session_id,
        user="user-a",
    )
    assert resp.status_code == 200
    grants = resp.json()["permissions"]
    grant_map = {g["user_id"]: g["level"] for g in grants}
    # user-a auto-got owner on creation; user-b and user-c were
    # granted explicitly above.
    assert grant_map == {
        "user-a": LEVEL_OWNER,
        "user-b": LEVEL_READ,
        "user-c": LEVEL_EDIT,
    }, (
        f"Expected 3 grants (user-a=owner, user-b=read, user-c=edit), "
        f"got {grant_map}. If user-a is missing, the auto-grant on "
        f"session creation is broken."
    )
    # Every grant should reference this session.
    for g in grants:
        assert g["conversation_id"] == session_id, (
            "Each grant's conversation_id must match the session."
        )


# ── Single-user local runtime ─────────────────────────────────


async def test_single_user_local_can_access_own_session(
    local_auth_client: httpx.AsyncClient,
) -> None:
    """On a single-user local runtime, headerless requests work as 'local'.

    The single-user app (``local_single_user=True``) resolves a
    missing ``X-Forwarded-Email`` to the reserved ``"local"``
    identity, which gets an owner auto-grant on sessions it creates
    — the bare `omnigent run` / local web UI flow.
    """
    agent = await create_test_agent(local_auth_client, user="bryan")

    # Omit X-Forwarded-Email to trigger the "local" fallback —
    # explicitly sending "local" as a header is rejected (reserved).
    s_local = await _create_session_as(
        local_auth_client,
        agent["id"],
        None,
        title="local-session",
    )
    resp = await local_auth_client.get(
        f"/v1/sessions/{s_local['id']}",
        # No X-Forwarded-Email header -> UnifiedAuthProvider returns "local"
    )
    assert resp.status_code == 200, (
        f"local user should be able to GET its own session, got {resp.status_code}."
    )
    assert resp.json()["id"] == s_local["id"]


# ── Admin bypass ─────────────────────────────────────────────


async def test_admin_user_bypasses_permission_checks(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An admin user can access sessions they have no explicit grant for.

    Sets the admin flag directly on the permission store to simulate
    server startup behavior (the CLI calls
    ``ensure_user("local", is_admin=True)``).
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # Set "admin-user" as admin directly in the store.
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user("admin-user", is_admin=True)

    # admin-user can GET user-a's session without any grant.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "admin-user"},
    )
    assert resp.status_code == 200, (
        f"admin user should bypass permission checks and GET any session, "
        f"got {resp.status_code}. If 404, the is_admin check in "
        f"check_session_access is not working."
    )
    assert resp.json()["id"] == session_id

    # admin-user can also list permissions on user-a's session.
    resp = await _list_permissions(
        auth_client,
        session_id,
        user="admin-user",
    )
    assert resp.status_code == 200, (
        f"admin user should be able to list permissions, got {resp.status_code}."
    )


# ── Revoke nonexistent: idempotent 204 ──────────────────────


async def test_revoke_nonexistent_grant_returns_204(
    auth_client: httpx.AsyncClient,
) -> None:
    """Revoking a user who has no grant returns 204 (no error)."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # "stranger" has never been granted anything on this session.
    resp = await _revoke_permission(
        auth_client,
        session_id,
        revoker="user-a",
        target_user="stranger",
    )
    # Idempotent revoke: 204 whether or not the grant existed.
    assert resp.status_code == 204, (
        f"Revoking a nonexistent grant should return 204, "
        f"got {resp.status_code}. If 404 or 400, the revoke "
        f"endpoint is not idempotent."
    )


# ── Cross-session isolation ──────────────────────────────────


async def test_grant_on_one_session_does_not_leak_to_another(
    auth_client: httpx.AsyncClient,
) -> None:
    """A grant on session A does not grant access to session B."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(
        auth_client,
        agent["id"],
        "user-a",
        title="session-A",
    )
    s2 = await _create_session_as(
        auth_client,
        agent["id"],
        "user-a",
        title="session-B",
    )

    # Grant user-b read on session A only.
    resp = await _grant_permission(
        auth_client,
        s1["id"],
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # user-b can GET session A.
    resp = await auth_client.get(
        f"/v1/sessions/{s1['id']}",
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 200

    # user-b cannot GET session B (no grant).
    resp = await auth_client.get(
        f"/v1/sessions/{s2['id']}",
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 404, (
        f"user-b has a grant on session A but NOT session B. "
        f"GET on session B should return 404, got {resp.status_code}. "
        f"If 200, grants are leaking across sessions."
    )


# ── Non-manager cannot grant or revoke ───────────────────────


async def test_non_manager_cannot_grant_permissions(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user with only read access cannot grant permissions."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # Grant user-b read only.
    await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )

    # user-b tries to grant user-c -> blocked (requires manage).
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-b",
        target_user="user-c",
        level=LEVEL_READ,
    )
    assert resp.status_code == 403, (
        f"user-b with read grant should be blocked from granting "
        f"permissions. Got {resp.status_code}."
    )


async def test_non_manager_cannot_revoke_permissions(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user without manage access cannot revoke permissions."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # Grant user-b edit (not manage).
    await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_EDIT,
    )

    # user-b tries to revoke user-a -> blocked (requires manage).
    resp = await _revoke_permission(
        auth_client,
        session_id,
        revoker="user-b",
        target_user="user-a",
    )
    assert resp.status_code == 403, (
        f"user-b with edit grant should be blocked from revoking "
        f"permissions. Got {resp.status_code}."
    )


# ── Session creator auto-grant ───────────────────────────────


async def test_session_creator_gets_manage_grant(
    auth_client: httpx.AsyncClient,
) -> None:
    """Creating a session auto-grants the creator manage access."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "owner")
    session_id = s1["id"]

    # The creator should have a manage grant.
    resp = await _list_permissions(
        auth_client,
        session_id,
        user="owner",
    )
    assert resp.status_code == 200
    grants = resp.json()["permissions"]
    # Exactly one grant: the creator with owner level.
    assert len(grants) == 1, (
        f"Expected exactly 1 auto-grant on a fresh session, "
        f"got {len(grants)}. If 0, the auto-grant on create is broken."
    )
    assert grants[0]["user_id"] == "owner"
    assert grants[0]["level"] == LEVEL_OWNER, (
        f"Auto-grant should be owner (level {LEVEL_OWNER}), got {grants[0]['level']}."
    )


# ── Upsert: grant upgrades existing level ───────────────────


async def test_grant_upgrade_via_upsert(
    auth_client: httpx.AsyncClient,
) -> None:
    """Granting a higher level to an existing user upgrades the grant."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    session_id = s1["id"]

    # Grant user-b read first.
    await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_READ,
    )

    # Upgrade user-b to manage.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="user-a",
        target_user="user-b",
        level=LEVEL_MANAGE,
    )
    assert resp.status_code == 200
    assert resp.json()["level"] == LEVEL_MANAGE, (
        "Upsert should upgrade the level from read to manage."
    )

    # Verify via list.
    resp = await _list_permissions(
        auth_client,
        session_id,
        user="user-a",
    )
    grant_map = {g["user_id"]: g["level"] for g in resp.json()["permissions"]}
    assert grant_map["user-b"] == LEVEL_MANAGE, (
        "After upgrade, user-b should have manage level in the store."
    )


# ── Unauthenticated request: no header ────────────────────


async def test_no_header_rejected_in_header_mode(
    auth_client: httpx.AsyncClient,
) -> None:
    """Requests without X-Forwarded-Email are rejected (401) in header mode.

    Regression test at the route level: on a deployed
    header-mode server (no single-user marker), a missing or
    proxy-dropped identity header must fail closed. Before the fix
    it resolved to the shared "local" identity, giving every
    unauthenticated request OWNER access to every other
    unauthenticated user's sessions.
    """
    agent = await create_test_agent(auth_client, user="bryan")

    # Create without header -> 401, nothing created.
    resp = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        # No X-Forwarded-Email header
    )
    assert resp.status_code == 401, (
        f"Creating a session without X-Forwarded-Email must be rejected "
        f"with 401 in header mode, got {resp.status_code}. A 201 means "
        f"unauthenticated requests share the 'local' identity."
    )

    # Reads fail closed too: a headerless request can't list sessions.
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    resp = await auth_client.get("/v1/sessions")
    assert resp.status_code == 401, (
        f"Listing sessions without X-Forwarded-Email must be rejected "
        f"with 401 in header mode, got {resp.status_code}."
    )
    resp = await auth_client.get(f"/v1/sessions/{s1['id']}")
    assert resp.status_code == 401, (
        f"Reading a session without X-Forwarded-Email must be rejected "
        f"with 401 in header mode, got {resp.status_code}."
    )


async def test_no_header_defaults_to_local_user_in_single_user_mode(
    local_auth_client: httpx.AsyncClient,
) -> None:
    """Headerless requests default to 'local' on a single-user runtime."""
    agent = await create_test_agent(local_auth_client, user="bryan")

    # Create session without header -> should succeed as "local".
    resp = await local_auth_client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        # No X-Forwarded-Email header
    )
    assert resp.status_code == 201, (
        f"Creating a session without X-Forwarded-Email should default "
        f"to 'local' user on a single-user runtime, got {resp.status_code}."
    )
    session_id = resp.json()["id"]

    # "local" should have an owner grant. Use user=None to omit
    # the header and trigger the "local" fallback — sending
    # "local" as a header would be rejected (reserved name).
    resp = await _list_permissions(
        local_auth_client,
        session_id,
        user=None,
    )
    assert resp.status_code == 200
    grants = resp.json()["permissions"]
    assert any(g["user_id"] == "local" and g["level"] == LEVEL_OWNER for g in grants), (
        "The 'local' user (default from missing header) should have "
        "an owner auto-grant on the created session."
    )


# ── Self-downgrade blocked ─────────────────────────────────


async def test_self_grant_blocked_at_any_level(
    auth_client: httpx.AsyncClient,
) -> None:
    """The session owner cannot grant themselves ANY level — self-modification is fully blocked.

    The route checks ``body.user_id == user_id`` before touching
    the store, so every self-grant level (1, 2, 3) must return 403
    with "Cannot modify your own permissions". This is not limited
    to downgrades — even re-granting the same level is blocked.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = s1["id"]

    for level in (LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE):
        resp = await _grant_permission(
            auth_client,
            session_id,
            granter="bryan",
            target_user="bryan",
            level=level,
        )
        # Self-grant at ANY level must be blocked. If 200, the
        # ``body.user_id == user_id`` guard in grant_permission is
        # missing or not covering this level.
        assert resp.status_code == 403, (
            f"Self-grant at level={level} should return 403, "
            f"got {resp.status_code}. If 200, the self-modification "
            f"guard is broken or has a level exception."
        )
        body = resp.json()
        assert "Cannot modify your own permissions" in body["error"]["message"], (
            f"Expected 'Cannot modify your own permissions' in error "
            f"message, got {body['error']['message']!r}."
        )


# ── Self-revoke blocked (general) ──────────────────────────


async def test_self_revoke_blocked(
    auth_client: httpx.AsyncClient,
) -> None:
    """The session owner cannot revoke themselves — self-modification is fully blocked.

    The existing lifecycle test (step 6) checks self-revoke for the
    owner. This test verifies the same guard independently and
    confirms the error message explicitly mentions self-modification.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = s1["id"]

    resp = await _revoke_permission(
        auth_client,
        session_id,
        revoker="bryan",
        target_user="bryan",
    )
    # Self-revoke must always be blocked. If 204, the
    # ``target_user_id == user_id`` guard in revoke_permission
    # is missing or was removed.
    assert resp.status_code == 403, (
        f"Self-revoke should return 403, got {resp.status_code}. "
        f"If 204, the self-modification guard is broken and the "
        f"session could be orphaned."
    )
    body = resp.json()
    assert "Cannot modify your own permissions" in body["error"]["message"], (
        f"Expected 'Cannot modify your own permissions' in error "
        f"message, got {body['error']['message']!r}."
    )


# ── Multi-session list with mixed access ───────────────────


async def test_multi_session_list_mixed_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Multiple sessions with mixed grants produce correct per-user visibility.

    Bryan creates S1-S5. Grants:
      - corey: read on S1, edit on S3
      - __public__: read on S5
      - rice: manage on S2

    Expected visibility (public-only sessions are excluded from the list):
      - bryan: all 5 (owner)
      - corey: S1 + S3 (direct grants only) = 2
      - rice: S2 (direct grant only) = 1
      - nobody: nothing (no direct grants)
    """
    agent = await create_test_agent(auth_client, user="bryan")

    sessions = []
    for i in range(1, 6):
        s = await _create_session_as(
            auth_client,
            agent["id"],
            "bryan",
            title=f"session-{i}",
        )
        sessions.append(s)
    s1, s2, s3, _, s5 = sessions

    # Grant corey read on S1, edit on S3.
    resp = await _grant_permission(
        auth_client,
        s1["id"],
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200
    resp = await _grant_permission(
        auth_client,
        s3["id"],
        granter="bryan",
        target_user="corey",
        level=LEVEL_EDIT,
    )
    assert resp.status_code == 200

    # Grant __public__ read on S5.
    resp = await _grant_permission(
        auth_client,
        s5["id"],
        granter="bryan",
        target_user="__public__",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # Grant rice manage on S2.
    resp = await _grant_permission(
        auth_client,
        s2["id"],
        granter="bryan",
        target_user="rice",
        level=LEVEL_MANAGE,
    )
    assert resp.status_code == 200

    all_ids = {s["id"] for s in sessions}

    # Bryan (owner of all 5) sees all 5.
    bryan_sessions = await _list_sessions_as(auth_client, "bryan")
    bryan_ids = {s["id"] for s in bryan_sessions}
    # Bryan owns all five sessions and must see every one.
    # If any are missing, the accessible_by filter is too restrictive
    # for owners.
    assert all_ids.issubset(bryan_ids), (
        f"Bryan should see all 5 sessions, but is missing {all_ids - bryan_ids}."
    )

    # Corey sees S1 (read), S3 (edit) = 2 of bryan's sessions.
    # S5 is public-only (no direct grant for corey) so it's excluded.
    corey_sessions = await _list_sessions_as(auth_client, "corey")
    corey_ids = {s["id"] for s in corey_sessions}
    expected_corey = {s1["id"], s3["id"]}
    assert expected_corey == corey_ids & all_ids, (
        f"Corey should see exactly S1, S3 from bryan's sessions, "
        f"but sees {corey_ids & all_ids}. Expected {expected_corey}."
    )

    # Rice sees S2 (manage) = 1 of bryan's sessions.
    # S5 is public-only (no direct grant for rice) so it's excluded.
    rice_sessions = await _list_sessions_as(auth_client, "rice")
    rice_ids = {s["id"] for s in rice_sessions}
    expected_rice = {s2["id"]}
    assert expected_rice == rice_ids & all_ids, (
        f"Rice should see exactly S2 from bryan's sessions, "
        f"but sees {rice_ids & all_ids}. Expected {expected_rice}."
    )

    # Nobody (zero direct grants) sees nothing — public-only sessions
    # are excluded from the list (accessible by direct URL only).
    nobody_sessions = await _list_sessions_as(auth_client, "nobody")
    nobody_ids = {s["id"] for s in nobody_sessions}
    assert not (nobody_ids & all_ids), (
        f"Nobody should see none of bryan's sessions, but sees {nobody_ids & all_ids}."
    )


# ── Invalid grant levels at HTTP boundary ──────────────────


@pytest.mark.parametrize(
    "invalid_level",
    [0, 4, -1],
    ids=["zero", "four", "negative"],
)
async def test_grant_invalid_level_returns_422(
    auth_client: httpx.AsyncClient,
    invalid_level: int,
) -> None:
    """Out-of-range grant levels (0, 4, -1) are rejected with 422 by Pydantic.

    The ``GrantPermissionRequest.level`` field has ``Field(ge=1, le=3)``,
    so any value outside [1, 3] triggers a Pydantic validation error
    before the route handler runs. 422 means the request body was
    syntactically valid JSON but semantically invalid.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = s1["id"]

    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="someone",
        level=invalid_level,
    )
    # Pydantic's Field(ge=1, le=3) rejects out-of-range levels with
    # a 422 Unprocessable Entity before the handler runs. If 200,
    # either the Field constraint was removed or the level is not
    # validated.
    assert resp.status_code == 422, (
        f"Grant with level={invalid_level} should return 422, "
        f"got {resp.status_code}. If 200, the Field(ge=1, le=3) "
        f"constraint on GrantPermissionRequest.level is missing."
    )


async def test_grant_valid_level_succeeds(
    auth_client: httpx.AsyncClient,
) -> None:
    """A valid grant level (2) succeeds with 200, confirming the validation boundary.

    This is the positive counterpart to the invalid-level tests: proves
    that the validation logic does not over-reject.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = s1["id"]

    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="someone",
        level=LEVEL_EDIT,
    )
    # Valid level=2 must succeed. If 422, the validation is too strict.
    assert resp.status_code == 200, (
        f"Grant with valid level=2 should return 200, got {resp.status_code}."
    )
    assert resp.json()["level"] == LEVEL_EDIT, "Returned level should match the granted level."


# ── Pagination with permission filter ──────────────────────


async def test_pagination_with_permission_filter(
    auth_client: httpx.AsyncClient,
) -> None:
    """Session list respects pagination cursors when filtered by permission.

    Bryan creates S1, S2, S3 and grants corey read on all three.
    Corey lists with limit=1 and paginates through all three sessions
    using the ``after`` cursor from each page.
    """
    agent = await create_test_agent(auth_client, user="bryan")

    session_ids = []
    for i in range(1, 4):
        s = await _create_session_as(
            auth_client,
            agent["id"],
            "bryan",
            title=f"page-{i}",
        )
        session_ids.append(s["id"])
        # Grant corey read on each session.
        resp = await _grant_permission(
            auth_client,
            s["id"],
            granter="bryan",
            target_user="corey",
            level=LEVEL_READ,
        )
        assert resp.status_code == 200

    # Corey paginates with limit=1.
    collected_ids: list[str] = []
    after_cursor: str | None = None
    pages_seen = 0

    while True:
        params: dict[str, Any] = {"limit": 1}
        if after_cursor is not None:
            params["after"] = after_cursor
        resp = await auth_client.get(
            "/v1/sessions",
            params=params,
            headers={"X-Forwarded-Email": "corey"},
        )
        assert resp.status_code == 200, f"List sessions failed: {resp.status_code} {resp.text}"
        body = resp.json()
        page_data = body["data"]
        # Each page must contain exactly 1 session (limit=1) except
        # possibly the last page if we've exhausted the list.
        for item in page_data:
            collected_ids.append(item["id"])
        pages_seen += 1
        if not body["has_more"]:
            break
        # Use last_id as the cursor for the next page.
        after_cursor = body["last_id"]
        # Safety: prevent infinite loops in case of a bug.
        assert pages_seen <= 5, (
            f"Pagination did not terminate after 5 pages — possible "
            f"infinite loop. Collected: {collected_ids}"
        )

    # All 3 sessions must appear exactly once across all pages.
    # If fewer, the permission filter or pagination cursor is broken.
    # If duplicates, the cursor is not advancing correctly.
    assert set(collected_ids) == set(session_ids), (
        f"Expected all 3 sessions {session_ids} across paginated pages, "
        f"got {collected_ids}. If fewer, pagination or permission "
        f"filter dropped a session. If duplicates, the cursor "
        f"is not advancing."
    )
    assert len(collected_ids) == len(session_ids), (
        f"Expected exactly {len(session_ids)} session ids (no duplicates), "
        f"got {len(collected_ids)}: {collected_ids}."
    )


# ── Transfer ownership ─────────────────────────────────────


async def test_owner_grant_is_immutable(
    auth_client: httpx.AsyncClient,
) -> None:
    """Owner (level 4) grants cannot be revoked or overwritten.

    Bryan creates S1 (gets owner), grants corey manage. Corey
    attempts to revoke bryan — blocked because bryan has LEVEL_OWNER.
    Bryan's owner grant also cannot be downgraded via a new grant.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = s1["id"]

    # Bryan grants corey manage access.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_MANAGE,
    )
    assert resp.status_code == 200

    # Corey attempts to revoke bryan — blocked (owner is immutable).
    resp = await _revoke_permission(
        auth_client,
        session_id,
        revoker="corey",
        target_user="bryan",
    )
    assert resp.status_code == 403, (
        f"Revoking an owner grant should return 403. "
        f"Got {resp.status_code}. If 204, the owner immutability "
        f"guard is broken."
    )

    # Bryan still has full access.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "bryan"},
    )
    assert resp.status_code == 200

    # Verify both grants remain.
    resp = await _list_permissions(
        auth_client,
        session_id,
        user="bryan",
    )
    assert resp.status_code == 200
    grants = resp.json()["permissions"]
    grant_map = {g["user_id"]: g["level"] for g in grants}
    assert grant_map == {"bryan": LEVEL_OWNER, "corey": LEVEL_MANAGE}, (
        f"Expected bryan=owner(4) and corey=manage(3), got {grant_map}."
    )


# ── PATCH session: title requires edit, runner_id requires owner ──


async def test_patch_session_requires_edit_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Read-only cannot PATCH title; edit can. Runner_id requires owner.

    Bryan creates S1, grants corey read. Corey tries PATCH title -> 403.
    Bryan upgrades corey to edit. Corey PATCHes title -> 200.
    Corey tries PATCH runner_id -> 403 (owner required), with fork hint.
    Bryan (owner) PATCHes runner_id -> 200.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(
        auth_client,
        agent["id"],
        "bryan",
        title="original-title",
    )
    session_id = s1["id"]

    # Grant corey read only.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # Corey tries PATCH title with read-only -> blocked.
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "corey-was-here"},
        headers={"X-Forwarded-Email": "corey"},
    )
    assert resp.status_code == 403, (
        f"Corey with read-only should get 403 on PATCH, got {resp.status_code}."
    )

    # Bryan upgrades corey to edit.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_EDIT,
    )
    assert resp.status_code == 200

    # Corey PATCHes title -> succeeds with edit.
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "corey-was-here"},
        headers={"X-Forwarded-Email": "corey"},
    )
    assert resp.status_code == 200, (
        f"Corey with edit access should be able to PATCH title, got {resp.status_code}."
    )
    assert resp.json()["title"] == "corey-was-here", (
        "PATCH response should reflect the updated title."
    )

    # Corey tries PATCH runner_id -> blocked (requires owner).
    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": "runner_fake"},
        headers={"X-Forwarded-Email": "corey"},
    )
    assert resp.status_code == 403, (
        f"Corey with edit access should get 403 when setting runner_id "
        f"(requires owner), got {resp.status_code}."
    )
    error_msg = resp.json()["error"]["message"]
    assert "owner" in error_msg.lower(), "403 message should mention owner requirement."
    assert f"--fork {session_id}" in error_msg, "403 message should suggest the fork CLI command."


# ── GET /sessions/{id}/items with read access ──────────────


async def test_get_session_items_with_read_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user with read access can GET session items; a user with no access gets 404."""
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = s1["id"]

    # Grant corey read.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # Corey can GET items.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/items",
        headers={"X-Forwarded-Email": "corey"},
    )
    # Read access is sufficient for GET /items. If 403, the items
    # endpoint requires a higher level than LEVEL_READ.
    assert resp.status_code == 200, (
        f"Corey with read grant should GET /items -> 200, got {resp.status_code}."
    )

    # Stranger has no access -> 404.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/items",
        headers={"X-Forwarded-Email": "stranger"},
    )
    # No grant at all -> 404 (session existence hidden).
    assert resp.status_code == 404, (
        f"Stranger with no grant should get 404 on /items, "
        f"got {resp.status_code}. If 200, the permission check "
        f"on GET /items is missing."
    )


# ── SSE stream without permissions → 404 ───────────────────


async def test_stream_session_denied_without_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user with no grant gets 404 when requesting the SSE stream.

    The permission check fires before the streaming generator starts,
    so the response completes immediately with a JSON error body.
    We do NOT test the success case (200) here because the SSE stream
    never terminates and the ASGI in-process transport blocks. The
    permission guard is shared code (``_require_access``) tested on
    other endpoints; the 404 case confirms the stream endpoint wires
    it in.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = s1["id"]

    # Nobody has no grant -> 404 (session existence hidden).
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/stream",
        headers={"X-Forwarded-Email": "nobody"},
    )
    # No grant at all -> 404. If 200, the permission check on
    # GET /stream is missing. The error response body is a normal
    # JSON payload (not SSE) because the error fires before the
    # streaming generator starts.
    assert resp.status_code == 404, (
        f"Nobody with no grant should get 404 on /stream, "
        f"got {resp.status_code}. If 200, the permission check "
        f"on GET /stream is missing."
    )

    # Also verify a user with insufficient (read-only) access can
    # at least reach the endpoint (the stream requires LEVEL_READ,
    # which they have). We test this indirectly: grant corey read,
    # then confirm corey does NOT get 404 on the stream URL by
    # checking that the GET /session (same permission level) works.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200
    # Corey can GET the session (same LEVEL_READ check as /stream).
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": "corey"},
    )
    # If corey can GET the session, the read-level permission is
    # sufficient. The stream endpoint uses the same guard.
    assert resp.status_code == 200, (
        f"Corey with read grant should GET session -> 200, got {resp.status_code}."
    )


# ── Fork session requires read access ───────────────────────


async def test_fork_session_requires_read_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """A user with no access cannot fork; a user with read access can.

    Bryan creates S1. Nobody tries to fork -> 404 (no access).
    Bryan grants corey read. Corey forks -> 201.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    s1 = await _create_session_as(
        auth_client,
        agent["id"],
        "bryan",
        title="original",
    )
    session_id = s1["id"]

    # Nobody (no grant) tries to fork -> 404 (hides existence).
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/fork",
        json={},
        headers={"X-Forwarded-Email": "nobody"},
    )
    assert resp.status_code == 404, (
        f"User with no access should get 404 on fork, got {resp.status_code}."
    )

    # Grant corey read.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # Corey forks -> succeeds.
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/fork",
        json={},
        headers={"X-Forwarded-Email": "corey"},
    )
    assert resp.status_code == 201, (
        f"Corey with read access should be able to fork, got {resp.status_code}."
    )
    fork = resp.json()
    assert fork["id"] != session_id, "Fork should have a new session id."
    assert fork["permission_level"] == LEVEL_OWNER, (
        f"Forking user should be the owner of the new session, "
        f"got permission_level={fork['permission_level']}."
    )

    # Bryan should NOT have access to Corey's fork.
    resp = await auth_client.get(
        f"/v1/sessions/{fork['id']}",
        headers={"X-Forwarded-Email": "bryan"},
    )
    assert resp.status_code == 404, (
        f"Bryan should not have access to Corey's fork, got {resp.status_code}."
    )


# ── GET /sessions/{id}/owner ─────────────────────────────────


async def test_get_owner_returns_creator(
    auth_client: httpx.AsyncClient,
) -> None:
    """GET /sessions/{id}/owner returns the session creator for any user with read access."""
    agent = await create_test_agent(auth_client, user="bryan")
    session = await _create_session_as(auth_client, agent["id"], "bryan")
    session_id = session["id"]

    # Grant corey read access.
    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # Bryan (owner) sees himself as owner.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/owner",
        headers={"X-Forwarded-Email": "bryan"},
    )
    assert resp.status_code == 200
    assert resp.json()["owner"] == "bryan"

    # Corey (read-only) also sees bryan as owner.
    resp = await auth_client.get(
        f"/v1/sessions/{session_id}/owner",
        headers={"X-Forwarded-Email": "corey"},
    )
    assert resp.status_code == 200
    assert resp.json()["owner"] == "bryan"


async def test_get_owner_forbidden_without_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """GET /sessions/{id}/owner returns 404 for users with no access."""
    agent = await create_test_agent(auth_client, user="bryan")
    session = await _create_session_as(auth_client, agent["id"], "bryan")

    resp = await auth_client.get(
        f"/v1/sessions/{session['id']}/owner",
        headers={"X-Forwarded-Email": "nobody"},
    )
    assert resp.status_code == 404


# ── owner field in GET /v1/sessions list ────────────────────


async def test_list_sessions_includes_owner_for_shared_session(
    auth_client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions includes the owner field so the sidebar
    can display it without a separate per-session API call.

    Bryan creates a session, grants Corey read access. When Corey
    lists sessions, the item must carry ``owner: "bryan"``.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    session = await _create_session_as(auth_client, agent["id"], "bryan", title="shared-test")
    session_id = session["id"]

    resp = await _grant_permission(
        auth_client,
        session_id,
        granter="bryan",
        target_user="corey",
        level=LEVEL_READ,
    )
    assert resp.status_code == 200

    # Corey lists sessions and should see the owner field.
    corey_sessions = await _list_sessions_as(auth_client, "corey")
    matched = [s for s in corey_sessions if s["id"] == session_id]
    assert len(matched) == 1, (
        f"Corey should see exactly one session ({session_id}), "
        f"got {len(matched)}. If 0, the grant didn't propagate to the list filter."
    )
    # owner must be the creator, not the requesting user.
    assert matched[0]["owner"] == "bryan", (
        f"Expected owner='bryan' (the session creator), "
        f"got owner={matched[0].get('owner')!r}. If None, the list "
        f"endpoint is not populating the owner field."
    )


async def test_list_sessions_includes_owner_for_own_session(
    auth_client: httpx.AsyncClient,
) -> None:
    """The owner field is present even when the requesting user
    is the session owner — the sidebar needs it to know whether
    to render the owner subtitle.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    session = await _create_session_as(auth_client, agent["id"], "bryan", title="own-test")
    session_id = session["id"]

    bryan_sessions = await _list_sessions_as(auth_client, "bryan")
    matched = [s for s in bryan_sessions if s["id"] == session_id]
    assert len(matched) == 1, f"Bryan should see the session they created ({session_id})."
    assert matched[0]["owner"] == "bryan", (
        f"Expected owner='bryan' for the creator's own session, "
        f"got owner={matched[0].get('owner')!r}."
    )


# ── session-create authorization ─────────────


async def _create_bundled_session_as(
    client: httpx.AsyncClient,
    user: str,
    *,
    name: str = "test-agent",
    title: str | None = None,
) -> dict[str, Any]:
    """Create a session via multipart upload as a specific user.

    Returns the full session snapshot (including ``agent_id``).

    :param client: The test HTTP client.
    :param user: User identity for ``X-Forwarded-Email``.
    :param name: Agent name to write into the bundle.
    :param title: Optional session title.
    :returns: Parsed ``GET /v1/sessions/{id}`` snapshot.
    """
    metadata: dict[str, Any] = {}
    if title is not None:
        metadata["title"] = title
    bundle = build_agent_bundle(name=name)
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": user},
    )
    assert resp.status_code == 201, (
        f"bundled session create failed: {resp.status_code} {resp.text}"
    )
    session_id = resp.json()["session_id"]
    snapshot = await client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": user},
    )
    assert snapshot.status_code == 200
    return snapshot.json()


async def test_w5_01_parent_session_id_requires_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """A caller cannot supply another user's session as
    ``parent_session_id`` to inherit runner bindings and establish
    a parent-child link.

    Alice creates a session. Bob tries to create a child session
    referencing Alice's session as parent — the server must reject
    with 404 (session not found from Bob's perspective) since Bob
    has no access to Alice's session.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    alice_session = await _create_session_as(
        auth_client, agent["id"], "alice", title="alice-parent"
    )
    alice_session_id = alice_session["id"]

    # Bob tries to create a child session referencing Alice's session.
    resp = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": alice_session_id,
        },
        headers={"X-Forwarded-Email": "bob"},
    )
    assert resp.status_code in (403, 404), (
        f"Expected 403/404 when Bob references Alice's session as parent, "
        f"got {resp.status_code}: {resp.text}"
    )


async def test_w5_01_parent_session_id_allowed_with_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Positive path: when Alice grants Bob read access to
    her session, Bob can reference it as ``parent_session_id``.
    """
    agent = await create_test_agent(auth_client, user="bryan")
    alice_session = await _create_session_as(
        auth_client, agent["id"], "alice", title="alice-parent"
    )
    alice_session_id = alice_session["id"]

    # Alice grants Bob read access.
    grant_resp = await _grant_permission(
        auth_client,
        alice_session_id,
        granter="alice",
        target_user="bob",
        level=LEVEL_READ,
    )
    assert grant_resp.status_code == 200

    # Bob can now create a child session referencing Alice's session.
    # Use Alice's session-scoped agent (Bob has read access to Alice's
    # owning session, so the agent access check passes).
    resp = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": alice_session["agent_id"],
            "parent_session_id": alice_session_id,
        },
        headers={"X-Forwarded-Email": "bob"},
    )
    assert resp.status_code == 201, (
        f"Expected 201 when Bob has read access to Alice's parent session, "
        f"got {resp.status_code}: {resp.text}"
    )


async def test_w5_01_multipart_parent_session_id_requires_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Multipart path: a caller cannot supply another user's
    session as ``metadata.parent_session_id`` on the bundled create.

    The bundle-mode ``sys_session_create`` rides this endpoint, so a
    missing check would let any user parent a session into someone
    else's tree (and inherit their runner binding) by uploading a
    bundle. Must reject with 404 (no access = existence hidden).
    """
    alice_session = await _create_session_as(
        auth_client, "ignored", "alice", title="alice-multipart-parent"
    )

    bundle = build_agent_bundle(name="bob-bundle-agent")
    resp = await auth_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": alice_session["id"]})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": "bob"},
    )
    assert resp.status_code in (403, 404), (
        f"Expected 403/404 when Bob references Alice's session as multipart "
        f"parent, got {resp.status_code}: {resp.text}"
    )


async def test_w5_01_multipart_parent_session_id_allowed_with_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Positive path, multipart: with READ access to Alice's
    session, Bob's bundled create may parent into it.

    The created child must be linked to Alice's session — proving the
    metadata field traversed authorization → store → conversation row,
    not just that the request was accepted.
    """
    alice_session = await _create_session_as(
        auth_client, "ignored", "alice", title="alice-multipart-parent-ok"
    )
    grant_resp = await _grant_permission(
        auth_client,
        alice_session["id"],
        granter="alice",
        target_user="bob",
        level=LEVEL_READ,
    )
    assert grant_resp.status_code == 200

    bundle = build_agent_bundle(name="bob-granted-bundle-agent")
    resp = await auth_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": alice_session["id"]})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": "bob"},
    )
    assert resp.status_code == 201, (
        f"Expected 201 when Bob has read access to Alice's parent session, "
        f"got {resp.status_code}: {resp.text}"
    )
    child_id = resp.json()["session_id"]
    snap = await auth_client.get(
        f"/v1/sessions/{child_id}",
        headers={"X-Forwarded-Email": "bob"},
    )
    assert snap.status_code == 200, snap.text
    assert snap.json()["parent_session_id"] == alice_session["id"]


async def test_w7_2_session_scoped_agent_requires_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """A caller cannot execute another user's session-scoped
    agent by raw ``agent_id``.

    Alice creates a bundled session (which produces a session-scoped
    agent). Bob extracts the agent id and tries to create a new
    session with it — the server must reject because Bob has no
    access to Alice's owning session.
    """
    alice_session = await _create_bundled_session_as(
        auth_client, "alice", name="alice-private-agent", title="alice-bundled"
    )
    alice_agent_id = alice_session["agent_id"]

    # Bob tries to create a session using Alice's session-scoped agent.
    resp = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": alice_agent_id},
        headers={"X-Forwarded-Email": "bob"},
    )
    assert resp.status_code in (403, 404), (
        f"Expected 403/404 when Bob uses Alice's session-scoped agent, "
        f"got {resp.status_code}: {resp.text}"
    )


async def test_w7_2_session_scoped_agent_allowed_with_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Positive path: when Alice grants Bob read access to
    the owning session, Bob can use the session-scoped agent.
    """
    alice_session = await _create_bundled_session_as(
        auth_client, "alice", name="alice-shared-agent", title="alice-shared"
    )
    alice_agent_id = alice_session["agent_id"]
    alice_session_id = alice_session["id"]

    # Alice grants Bob read access to the owning session.
    grant_resp = await _grant_permission(
        auth_client,
        alice_session_id,
        granter="alice",
        target_user="bob",
        level=LEVEL_READ,
    )
    assert grant_resp.status_code == 200

    # Bob can now create a session with Alice's session-scoped agent.
    resp = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": alice_agent_id},
        headers={"X-Forwarded-Email": "bob"},
    )
    assert resp.status_code == 201, (
        f"Expected 201 when Bob has read access to Alice's owning session, "
        f"got {resp.status_code}: {resp.text}"
    )


async def test_w7_2_session_scoped_agent_requires_owning_session_access(
    auth_client: httpx.AsyncClient,
) -> None:
    """Session-scoped agents require the caller to have READ
    access to the owning session before binding the agent to a new
    session. Without a grant, Bob cannot use Bryan's agent.
    """
    agent = await create_test_agent(auth_client, user="bryan")

    # Bob tries to create a session with Bryan's agent — should be
    # denied because Bob has no access to the owning session.
    resp = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        headers={"X-Forwarded-Email": "bob"},
    )
    assert resp.status_code in (403, 404), (
        f"Expected 403/404 for session-scoped agent without access, "
        f"got {resp.status_code}: {resp.text}"
    )


async def test_create_session_rejects_other_users_host(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Creating a session that targets another user's host is rejected
    with 403 BEFORE the host is ever contacted.

    The create-session workspace validation does a ``host.stat``
    round-trip to the target host. Without an ownership check on that
    path, a user could probe another user's host filesystem (path
    existence / cwd-boundary) just by naming their ``host_id`` — a
    cross-user disclosure the launch-time ``resolve_host_launch`` check
    would not prevent, since the stat already happened. The shared
    ``resolve_host_owner`` check must fire first.

    The ``detail == "not your host"`` assertion pins the 403 to the
    host-owner check (an agent-access rejection would 403 differently),
    and the empty ``outbound_queue`` proves the order: a missing check
    would have enqueued the stat frame to Alice's connection.
    """
    # Alice owns an online host.
    _register_online_host(host_perm_app, "f54bb9272002938a3a934bfcb6bb228a", "alice@example.com")
    alice_conn = host_perm_app.state.host_registry.get("f54bb9272002938a3a934bfcb6bb228a")
    assert alice_conn is not None

    # A bindable BUILT-IN (template) agent: session_id IS NULL, so any
    # user can bind it. Session-scoped agents are owned by one session
    # and would block Bob at the agent-access check before the host
    # check (see designs/BUILTIN_AGENTS.md); creating a template here
    # isolates the host-owner check.
    SqlAlchemyAgentStore(db_uri).create(
        "f2b40a7cc3eaec4ee8cbb151e1021c75",
        "builtin-xuser-agent",
        "f2b40a7cc3eaec4ee8cbb151e1021c75/bundle",
    )

    # Bob targets Alice's host.
    resp = await host_perm_client.post(
        "/v1/sessions",
        json={
            "agent_id": "f2b40a7cc3eaec4ee8cbb151e1021c75",
            "host_id": "f54bb9272002938a3a934bfcb6bb228a",
            "workspace": "/tmp",
        },
        headers={"X-Forwarded-Email": "bob@example.com"},
    )

    assert resp.status_code == 403, (
        f"Bob targeting Alice's host should be 403, got {resp.status_code}: {resp.text}"
    )
    assert resp.json().get("detail") == "not your host", (
        f"403 should come from the host-owner check, got detail={resp.json().get('detail')!r}"
    )
    # The ownership check fired before the host.stat round-trip: nothing
    # was ever enqueued to Alice's host connection.
    assert alice_conn.outbound_queue.empty(), (
        "Bob's create reached Alice's host (a frame was enqueued) before "
        "the ownership check — cross-user host probe."
    )


# ── Fork permission isolation ─────────────


async def test_read_only_collaborator_can_fork_and_owns_the_fork(
    auth_client: httpx.AsyncClient,
) -> None:
    """A read-only collaborator can fork a shared session; the fork is
    owned by the forking user and its permissions are isolated from the
    source (source grants not copied, source not mutated, no sidebar
    leak)."""
    agent = await create_test_agent(auth_client, user="bryan")
    # Bryan owns the source (creator auto-gets LEVEL_OWNER); shares read
    # with corey.
    source_id = (await _create_session_as(auth_client, agent["id"], "bryan"))["id"]
    grant = await _grant_permission(
        auth_client, source_id, granter="bryan", target_user="corey", level=LEVEL_READ
    )
    assert grant.status_code == 200

    # A read-only collaborator can fork.
    fork_resp = await auth_client.post(
        f"/v1/sessions/{source_id}/fork",
        json={},
        headers={"X-Forwarded-Email": "corey"},
    )
    assert fork_resp.status_code == 201, fork_resp.text
    fork_id = fork_resp.json()["id"]

    # Fork: corey owns it, bryan has no grant on it.
    fork_perms = {
        p["user_id"]: p["level"]
        for p in (await _list_permissions(auth_client, fork_id, user="corey")).json()[
            "permissions"
        ]
    }
    assert fork_perms == {"corey": LEVEL_OWNER}

    # Source grants unchanged by the fork.
    src_perms = {
        p["user_id"]: p["level"]
        for p in (await _list_permissions(auth_client, source_id, user="bryan")).json()[
            "permissions"
        ]
    }
    assert src_perms == {"bryan": LEVEL_OWNER, "corey": LEVEL_READ}

    # Visibility: corey sees the fork; bryan does not.
    assert fork_id in {s["id"] for s in await _list_sessions_as(auth_client, "corey")}
    assert fork_id not in {s["id"] for s in await _list_sessions_as(auth_client, "bryan")}


async def test_bob_cannot_create_worktree_session_on_alice_host(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
) -> None:
    """
    Bob cannot create a git-worktree session on Alice's host.

    Distinct from the workspace-only host-owner test above: this one
    carries a ``git`` block, so it exercises the worktree-creation
    path. Worktree creation writes to the host (``git worktree add``),
    and host ownership is checked (in ``_validate_session_workspace``)
    BEFORE that path runs. If someone reordered worktree creation
    ahead of the ownership gate, a ``host.create_worktree`` frame would
    reach Alice's host and the empty-queue assertion below would fail —
    a regression the workspace-only test can't catch.
    """
    _register_online_host(host_perm_app, "f54bb9272002938a3a934bfcb6bb228a", "alice@example.com")
    alice_conn = host_perm_app.state.host_registry.get("f54bb9272002938a3a934bfcb6bb228a")
    assert alice_conn is not None

    # Bob owns an agent he is allowed to bind (passes the agent
    # access check), isolating the host-owner check as the rejection.
    bob_agent = await create_test_agent(
        host_perm_client, name="bob-worktree-agent", user="bob@example.com"
    )

    resp = await host_perm_client.post(
        "/v1/sessions",
        json={
            "agent_id": bob_agent["id"],
            "host_id": "f54bb9272002938a3a934bfcb6bb228a",
            "workspace": "/Users/alice/repo",
            "git": {"branch_name": "feature/x"},
        },
        headers={"X-Forwarded-Email": "bob@example.com"},
    )

    # 403 from the host-owner check (404 also acceptable if the host
    # were hidden); either way, rejected before any worktree write.
    assert resp.status_code in (403, 404), (
        f"Bob creating a worktree on Alice's host should be 403/404, "
        f"got {resp.status_code}: {resp.text}"
    )
    # Nothing reached Alice's host — no stat, and crucially no
    # host.create_worktree (the write path stayed behind the gate).
    assert alice_conn.outbound_queue.empty(), (
        "Bob's worktree create reached Alice's host before the ownership "
        "check — the worktree-write path is not gated."
    )


async def test_bob_cannot_clean_up_alice_worktree_via_delete(
    host_perm_app: FastAPI,
    host_perm_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Bob cannot trigger worktree cleanup on Alice's session.

    The delete endpoint's owner check fires before the cleanup branch,
    so Bob's ``?delete_branch=true`` is rejected and no
    ``host.remove_worktree`` frame reaches Alice's host. Alice's
    session (and its worktree) survive.
    """
    _register_online_host(host_perm_app, "f54bb9272002938a3a934bfcb6bb228a", "alice@example.com")
    alice_conn = host_perm_app.state.host_registry.get("f54bb9272002938a3a934bfcb6bb228a")
    assert alice_conn is not None

    # Alice owns a worktree session. Built via the store + an explicit
    # owner grant because the public API has no way to set git_branch.
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id="f54bb9272002938a3a934bfcb6bb228a",
        workspace="/Users/alice/repo-worktrees/feature-x",
        git_branch="feature/x",
    )
    perm = SqlAlchemyPermissionStore(db_uri)
    perm.ensure_user("alice@example.com")
    perm.grant("alice@example.com", conv.id, LEVEL_OWNER)

    resp = await host_perm_client.delete(
        f"/v1/sessions/{conv.id}?delete_branch=true",
        headers={"X-Forwarded-Email": "bob@example.com"},
    )

    # 404 (not enumerable) — Bob has no grant on Alice's session.
    assert resp.status_code in (403, 404), (
        f"Bob deleting Alice's session should be 403/404, got {resp.status_code}: {resp.text}"
    )
    # No remove_worktree frame reached Alice's host.
    assert alice_conn.outbound_queue.empty(), (
        "Bob's delete reached Alice's host before the owner check — cross-user worktree cleanup."
    )
    # Alice's session still exists (Bob's delete didn't go through).
    assert conv_store.get_conversation(conv.id) is not None, (
        "Bob's rejected delete must not have removed Alice's session."
    )


# ── child_sessions: enumeration is gated on parent READ ──────


async def test_list_child_sessions_blocks_cross_user(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A user without READ on the parent cannot enumerate its child sub-agents.

    Regression guard for the missing access check on
    ``GET /v1/sessions/{id}/child_sessions``: before the fix the route ran
    no ``_require_access`` at all, so any authenticated user could read
    another user's sub-agent titles, message previews, and
    pending-elicitation counts (cross-user data exposure + an existence
    oracle). ``user-a`` owns the parent; ``user-b`` (no grant) must get
    the existence-hiding 404; the owner still sees the seeded child.

    :param auth_client: Permission-enabled test client.
    :param db_uri: Per-test SQLite database URI, used to seed the child.
    """
    agent = await create_test_agent(auth_client, user="user-a")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    parent_id = s1["id"]

    # Seed one sub-agent child under user-a's parent — direct store write,
    # mirroring spawn._spawn_one minus the workflow start / SSE publish.
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent_id,
    )

    # user-b holds no grant on the parent → 404, not 403, so existence
    # isn't leaked. The pre-fix bug returned 200 with the full child listing.
    denied = await auth_client.get(
        f"/v1/sessions/{parent_id}/child_sessions",
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert denied.status_code == 404, (
        f"user-b without a grant must get the existence-hiding 404; got "
        f"{denied.status_code}: {denied.text}. A 403 leaks that the parent "
        f"exists, and a 200 means the enumeration hole is open."
    )
    # The denial must leak nothing — neither the child id nor its title.
    assert child.id not in denied.text and "researcher:auth" not in denied.text, (
        f"a denied response must not leak child data; got {denied.text}"
    )

    # The owner still lists the child end-to-end — the fix didn't break the
    # happy path, and this is exactly the data user-b was denied.
    owned = await auth_client.get(
        f"/v1/sessions/{parent_id}/child_sessions",
        headers={"X-Forwarded-Email": "user-a"},
    )
    assert owned.status_code == 200, f"owner list failed: {owned.text}"
    child_ids = [row["id"] for row in owned.json()["data"]]
    assert child.id in child_ids, (
        f"owner should see the seeded child {child.id!r}, got {child_ids}"
    )


async def test_list_child_sessions_allows_read_grant(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A user granted READ on the parent may enumerate its children.

    Confirms the gate is ``LEVEL_READ`` and not over-restricted to
    EDIT/OWNER: enumerating children is a read, so a read grant suffices.

    :param auth_client: Permission-enabled test client.
    :param db_uri: Per-test SQLite database URI, used to seed the child.
    """
    agent = await create_test_agent(auth_client, user="user-a")
    s1 = await _create_session_as(auth_client, agent["id"], "user-a")
    parent_id = s1["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent_id,
    )

    grant = await _grant_permission(
        auth_client, parent_id, granter="user-a", target_user="user-b", level=LEVEL_READ
    )
    assert grant.status_code == 200, f"grant failed: {grant.text}"

    resp = await auth_client.get(
        f"/v1/sessions/{parent_id}/child_sessions",
        headers={"X-Forwarded-Email": "user-b"},
    )
    assert resp.status_code == 200, (
        f"a READ grant must allow child enumeration; got {resp.status_code}: "
        f"{resp.text}. A 403 means the gate is over-restricted (EDIT/OWNER)."
    )
    child_ids = [row["id"] for row in resp.json()["data"]]
    assert child.id in child_ids, (
        f"read-granted user-b should see the seeded child {child.id!r}, got {child_ids}"
    )


# ── SSE stream presence (who-is-viewing circles) ─────────────
#
# The ASGI in-process transport buffers streaming responses, so these
# tests never iterate a live SSE body. Instead: the stream request
# runs as a background task (the route generator executes eagerly
# inside it), presence effects are observed through a real
# ``session_stream`` collector — the same pub/sub the route publishes
# to — and the stream is terminated via ``session_stream.close`` /
# task cancellation, after which the buffered body (including the
# snapshot-on-connect frames) becomes readable.


async def _end_stream_via_close(session_id: str, task: asyncio.Task[Any]) -> httpx.Response:
    """
    Terminate a buffered SSE stream request and return its response.

    Repeatedly broadcasts end-of-stream — ``close`` only reaches
    subscribers whose slot is already registered, and the stream task
    may not have subscribed yet — until the request task completes.

    :param session_id: The streamed session, e.g. ``"d1f9214d74c38b9f9a9db17ed8352dc4"``.
    :param task: The background ``client.get(...)`` request task.
    :returns: The completed (fully buffered) SSE response.
    """
    for _ in range(200):
        session_stream.close(session_id)
        if task.done():
            break
        await asyncio.sleep(0.01)
    return await asyncio.wait_for(task, 2.0)


def _sse_presence_events(body: str) -> list[dict[str, Any]]:
    """
    Parse ``session.presence`` frames out of a raw SSE body.

    :param body: The buffered ``text/event-stream`` payload.
    :returns: Decoded presence event dicts, in wire order.
    """
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        payload = json.loads(line[len("data: ") :])
        if payload.get("type") == "session.presence":
            events.append(payload)
    return events


async def test_stream_presence_join_broadcast_and_snapshot(
    auth_client: httpx.AsyncClient,
) -> None:
    """Opening the stream registers the viewer, broadcasts the join to
    co-subscribers, and the stream's own snapshot-on-connect carries the
    full viewer list — with the ``idle`` query param applied."""
    agent = await create_test_agent(auth_client, user="alice@example.com")
    session_id = (await _create_session_as(auth_client, agent["id"], "alice@example.com"))["id"]

    collector = await start_session_stream_collector(session_id)
    task = asyncio.create_task(
        auth_client.get(
            f"/v1/sessions/{session_id}/stream?idle=true",
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
    )
    try:
        # The join broadcast reaches an already-subscribed co-viewer
        # (the collector). Wrong/missing viewers here means the route
        # never registered the stream with the presence registry, or
        # dropped the idle query param on the floor.
        join = await collector.next_event()
        assert join["type"] == "session.presence"
        assert [v["user_id"] for v in join["viewers"]] == ["alice@example.com"]
        assert join["viewers"][0]["idle"] is True

        resp = await _end_stream_via_close(session_id, task)
        assert resp.status_code == 200
        # The buffered body holds the snapshot-on-connect frames: the
        # presence snapshot must list the connecting viewer themself.
        # An empty list here means the ``_resource_snapshot`` append is
        # missing — joiners would see nobody until the next edge.
        snapshots = _sse_presence_events(resp.text)
        assert snapshots, f"no session.presence frame in stream body: {resp.text[:500]}"
        assert [v["user_id"] for v in snapshots[0]["viewers"]] == ["alice@example.com"]
        assert snapshots[0]["viewers"][0]["idle"] is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await collector.stop()


async def test_stream_disconnect_broadcasts_leave_after_grace(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the stream (client disconnect) drives the generator's
    ``finally`` → presence deregistration → grace → leave broadcast."""
    monkeypatch.setattr(presence, "_LEAVE_GRACE_S", 0.05)
    agent = await create_test_agent(auth_client, user="alice@example.com")
    session_id = (await _create_session_as(auth_client, agent["id"], "alice@example.com"))["id"]

    collector = await start_session_stream_collector(session_id)
    task = asyncio.create_task(
        auth_client.get(
            f"/v1/sessions/{session_id}/stream",
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
    )
    try:
        join = await collector.next_event()
        assert [v["user_id"] for v in join["viewers"]] == ["alice@example.com"]
        # Default idle (no query param) is active — a True here means
        # the route invented an idle state for a plain connect.
        assert join["viewers"][0]["idle"] is False

        # Cancel the request: with the in-process transport the app
        # coroutine runs inside this task, so once the gather returns
        # the generator's ``finally`` (and presence.disconnect) has run.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # The leave lands only after the grace timer fires. No event =
        # the finally never deregistered (ghost viewers forever).
        leave = await collector.next_event()
        assert leave["type"] == "session.presence"
        assert leave["viewers"] == []
        # Top-level session: the presence scope (root) is the session itself.
        assert presence.snapshot(session_id, session_id)["viewers"] == []
    finally:
        await collector.stop()


async def test_stream_local_single_user_not_tracked(
    local_auth_client: httpx.AsyncClient,
) -> None:
    """A single-user request with no identity falls back to the reserved
    ``local`` user, which presence must NOT track (same attribution rule
    as ``created_by`` on messages).

    Uses ``local_auth_client`` (``local_single_user=True``) — the only
    posture in which a missing ``X-Forwarded-Email`` resolves to
    ``local`` instead of being rejected. Default header mode fails closed
    on missing identity, so ``auth_client``
    would 401 here and never exercise the presence attribution filter.
    """
    agent = await create_test_agent(local_auth_client, user=None)
    session_id = (await _create_session_as(local_auth_client, agent["id"], None))["id"]

    task = asyncio.create_task(local_auth_client.get(f"/v1/sessions/{session_id}/stream"))
    try:
        resp = await _end_stream_via_close(session_id, task)
        assert resp.status_code == 200
        # The stream's own snapshot-on-connect ran AFTER any (buggy)
        # registration would have happened, so a "local" viewer in it
        # proves the attribution filter was dropped from the route.
        snapshots = _sse_presence_events(resp.text)
        assert snapshots, f"no session.presence frame in stream body: {resp.text[:500]}"
        assert snapshots[0]["viewers"] == []
        # Top-level session: the presence scope (root) is the session itself.
        assert presence.snapshot(session_id, session_id)["viewers"] == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stream_presence_spans_subagent_conversations(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Viewers of a sub-agent page appear in the root page's presence
    (and vice versa).

    Regression test for tree-scoped presence: the web's sub-agent page
    opens the CHILD conversation's stream, and pre-fix presence was
    keyed by the streamed conversation id — so two users on the same
    session but different agents never saw each other's circles.

    :param auth_client: Permission-enabled test client.
    :param db_uri: Per-test SQLite database URI, used to seed the child.
    """
    agent = await create_test_agent(auth_client, user="alice@example.com")
    parent_id = (await _create_session_as(auth_client, agent["id"], "alice@example.com"))["id"]
    # Seed one sub-agent child under Alice's session — direct store
    # write, mirroring spawn._spawn_one minus workflow start.
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent_id,
    )
    grant = await _grant_permission(
        auth_client,
        parent_id,
        granter="alice@example.com",
        target_user="bob@example.com",
        level=LEVEL_READ,
    )
    assert grant.status_code == 200, f"grant failed: {grant.text}"

    root_collector = await start_session_stream_collector(parent_id)
    child_collector = await start_session_stream_collector(child.id)
    alice_task = asyncio.create_task(
        auth_client.get(
            f"/v1/sessions/{parent_id}/stream",
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
    )
    try:
        join = await root_collector.next_event()
        assert join["type"] == "session.presence"
        assert [v["user_id"] for v in join["viewers"]] == ["alice@example.com"]

        # Bob opens the SUB-AGENT conversation's stream only after
        # Alice's join landed, so the next root-stream event is
        # deterministically Bob's join.
        bob_task = asyncio.create_task(
            auth_client.get(
                f"/v1/sessions/{child.id}/stream",
                headers={"X-Forwarded-Email": "bob@example.com"},
            )
        )
        try:
            # Alice's root stream learns of Bob. Pre-fix this never
            # fired: Bob's registration lived under the child id and
            # the root stream got no presence event at all.
            both = await root_collector.next_event()
            assert both["type"] == "session.presence"
            assert both["conversation_id"] == parent_id
            assert [v["user_id"] for v in both["viewers"]] == [
                "alice@example.com",
                "bob@example.com",
            ]
            # Bob's child stream gets the SAME tree-wide list, stamped
            # with the child id his client guards incoming events by.
            child_event = await child_collector.next_event()
            assert child_event["type"] == "session.presence"
            assert child_event["conversation_id"] == child.id
            assert [v["user_id"] for v in child_event["viewers"]] == [
                "alice@example.com",
                "bob@example.com",
            ]
        finally:
            bob_task.cancel()
            await asyncio.gather(bob_task, return_exceptions=True)
    finally:
        alice_task.cancel()
        await asyncio.gather(alice_task, return_exceptions=True)
        await root_collector.stop()
        await child_collector.stop()
