"""Cross-user tests for runner binding ownership.

Exercises the security invariant that a user can only bind sessions
to runners they own, and that runner listing is scoped to the
caller's own runners.

Uses the ``auth_app`` / ``auth_client`` fixture pattern from
``test_sessions_permissions.py``: a :class:`SqlAlchemyPermissionStore`
is wired into the app so :class:`UnifiedAuthProvider` and permission
checks are active, and ``X-Forwarded-Email`` headers impersonate
different users.
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
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
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
from tests.unit.server.conftest import ControllableMockClient
from tests.unit.server.helpers import create_test_agent, register_test_runner

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
ALICE_RUNNER = "runner_alice_001"
BOB_RUNNER = "runner_bob_001"


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
    all session and runner routes.

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
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the auth-enabled FastAPI app.

    Same lifecycle pattern as the shared ``client`` fixture from
    ``conftest.py``.
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


# ── Helpers ──────────────────────────────────────────────────


async def _create_session_as(
    client: httpx.AsyncClient,
    agent_id: str,
    user: str,
) -> dict[str, Any]:
    """Create a session as a specific user via multipart bundled create.

    Each call creates a fresh session-scoped agent so there are no
    cross-user agent-ownership issues. ``agent_id`` is accepted for
    call-site compatibility but ignored.

    :param client: The test HTTP client.
    :param agent_id: Ignored — kept for call-site compatibility.
    :param user: User identity for ``X-Forwarded-Email``.
    :returns: The session snapshot dict.
    """
    import json as _json

    from tests.unit.server.helpers import build_agent_bundle

    bundle = build_agent_bundle(name="test-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"X-Forwarded-Email": user},
    )
    assert resp.status_code == 201, (
        f"Session creation failed for {user}: {resp.status_code} {resp.text}"
    )
    session_id = resp.json()["session_id"]
    snap = await client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-Forwarded-Email": user},
    )
    assert snap.status_code == 200
    return snap.json()


async def _patch_runner(
    client: httpx.AsyncClient,
    session_id: str,
    runner_id: str,
    user: str,
) -> httpx.Response:
    """PATCH a session with a runner_id as a specific user.

    :param client: The test HTTP client.
    :param session_id: Session to patch.
    :param runner_id: Runner to bind.
    :param user: User identity for ``X-Forwarded-Email``.
    :returns: The raw HTTP response.
    """
    return await client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        headers={"X-Forwarded-Email": user},
    )


# ── Tests: Runner listing is scoped to owner ─────────


async def test_list_runners_scoped_to_owner(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
) -> None:
    """GET /v1/runners returns only runners owned by the caller.

    Alice registers runner_alice; Bob registers runner_bob. When
    Alice lists runners she should see only hers. Bob should see
    only his.
    """
    register_test_runner(auth_app, ALICE_RUNNER, owner=ALICE)
    register_test_runner(auth_app, BOB_RUNNER, owner=BOB)

    alice_resp = await auth_client.get(
        "/v1/runners",
        headers={"X-Forwarded-Email": ALICE},
    )
    bob_resp = await auth_client.get(
        "/v1/runners",
        headers={"X-Forwarded-Email": BOB},
    )

    alice_ids = {r["runner_id"] for r in alice_resp.json()["data"]}
    bob_ids = {r["runner_id"] for r in bob_resp.json()["data"]}

    # Alice sees only her runner; Bob's runner is hidden.
    assert alice_ids == {ALICE_RUNNER}, (
        f"Alice should see only her runner, but saw {alice_ids}. "
        "If Bob's runner appears, the ownership filter is broken."
    )
    # Bob sees only his runner; Alice's runner is hidden.
    assert bob_ids == {BOB_RUNNER}, (
        f"Bob should see only his runner, but saw {bob_ids}. "
        "If Alice's runner appears, the ownership filter is broken."
    )


async def test_runner_status_hides_other_users_runner(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
) -> None:
    """GET /v1/runners/{id}/status reports offline for another user's runner.

    Alice's runner is online, but Bob querying its status should
    see ``online: false`` to prevent runner id enumeration.
    """
    register_test_runner(auth_app, ALICE_RUNNER, owner=ALICE)

    bob_resp = await auth_client.get(
        f"/v1/runners/{ALICE_RUNNER}/status",
        headers={"X-Forwarded-Email": BOB},
    )
    alice_resp = await auth_client.get(
        f"/v1/runners/{ALICE_RUNNER}/status",
        headers={"X-Forwarded-Email": ALICE},
    )

    # Bob sees Alice's runner as offline — prevents enumeration.
    assert bob_resp.json()["online"] is False, (
        "Bob should see Alice's runner as offline, but it reported online. "
        "The status endpoint is leaking runner existence to other users."
    )
    # Alice sees her own runner as online.
    assert alice_resp.json()["online"] is True, "Alice should see her own runner as online."


# ── Tests: Runner binding requires ownership ─────────


async def test_bind_own_runner_succeeds(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alice can bind her session to her own runner.

    Baseline happy-path: the ownership check must not reject
    legitimate same-user bindings.
    """
    # Stub out the runner-notification helper so the PATCH handler
    # doesn't try to POST into the fake tunnel (which would hang).
    from omnigent.server.routes import sessions as sessions_mod

    async def _stub_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub_get_runner_client)

    register_test_runner(auth_app, ALICE_RUNNER, owner=ALICE)
    agent = await create_test_agent(auth_client, user=ALICE)
    session = await _create_session_as(auth_client, agent["id"], ALICE)

    resp = await _patch_runner(auth_client, session["id"], ALICE_RUNNER, ALICE)

    # Owner binding succeeds with 200.
    assert resp.status_code == 200, (
        f"Alice binding her own runner should succeed, but got {resp.status_code}: "
        f"{resp.text}. The ownership check is rejecting legitimate bindings."
    )


async def test_bind_other_users_runner_is_forbidden(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
) -> None:
    """Bob cannot bind his session to Alice's runner.

    This is the core fix: a caller who owns a session must not
    be able to attach it to a runner owned by a different user.
    """
    register_test_runner(auth_app, ALICE_RUNNER, owner=ALICE)
    agent = await create_test_agent(auth_client, user=ALICE)
    session = await _create_session_as(auth_client, agent["id"], BOB)

    resp = await _patch_runner(auth_client, session["id"], ALICE_RUNNER, BOB)

    # Cross-user binding must be rejected.
    assert resp.status_code == 403, (
        f"Bob binding Alice's runner should return 403, but got {resp.status_code}: "
        f"{resp.text}. If 200, the runner ownership check is missing."
    )


async def test_parent_session_runner_inheritance_blocked_cross_user(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner inheritance via parent_session_id is blocked cross-user.

    When Bob creates a session with parent_session_id pointing to
    Alice's session (which is bound to Alice's runner), the new
    session must NOT inherit Alice's runner binding
    (defense-in-depth).
    """
    # Stub out runner notification to avoid hanging on the fake tunnel.
    from omnigent.server.routes import sessions as sessions_mod

    async def _stub_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub_get_runner_client)

    register_test_runner(auth_app, ALICE_RUNNER, owner=ALICE)
    agent = await create_test_agent(auth_client, user=ALICE)

    # Alice creates a session and binds it to her runner.
    alice_session = await _create_session_as(auth_client, agent["id"], ALICE)
    bind_resp = await _patch_runner(auth_client, alice_session["id"], ALICE_RUNNER, ALICE)
    assert bind_resp.status_code == 200

    # Grant Bob read access to Alice's session so the parent lookup
    # succeeds (session access is handled by a separate fix; this test
    # focuses on runner inheritance, not parent session access).
    await auth_client.post(
        f"/v1/sessions/{alice_session['id']}/permissions",
        json={"user_id": BOB, "level": "edit"},
        headers={"X-Forwarded-Email": ALICE},
    )

    # Bob creates a child session referencing Alice's session.
    resp = await auth_client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": alice_session["id"],
        },
        headers={"X-Forwarded-Email": BOB},
    )

    # The session should be created, but without Alice's runner.
    if resp.status_code == 201:
        child = resp.json()
        # The child session must NOT have inherited Alice's runner.
        child_runner = child.get("runner_id")
        assert child_runner != ALICE_RUNNER, (
            f"Bob's child session inherited Alice's runner {ALICE_RUNNER!r} "
            "via parent_session_id. The runner ownership check on inheritance "
            "is missing (defense-in-depth)."
        )


# ── Tests: Fork runner binding (clone-and-resume) ────


async def test_fork_is_unbound_and_only_forker_can_bind_runner(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clone-and-resume runner-binding contract for a forked session.

    A fork of a shared session is a fresh, unbound session owned by the
    forker. The forker can bind their own runner to resume it; the
    source owner retains no binding control over the fork.

    Pins the Web UI clone/fork "live runner binding" path: Bob clones
    Alice's shared session and resumes it on his own runner, without
    inheriting Alice's runner and without Alice being able to
    rebind Bob's clone.
    """
    # Stub the runner-notification helper so PATCH doesn't POST into the
    # fake tunnel (which would hang) — same pattern as
    # ``test_bind_own_runner_succeeds``.
    from omnigent.server.routes import sessions as sessions_mod

    async def _stub_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> None:
        return None

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub_get_runner_client)

    register_test_runner(auth_app, ALICE_RUNNER, owner=ALICE)
    register_test_runner(auth_app, BOB_RUNNER, owner=BOB)

    # Alice owns a session bound to her runner, shared read-only with Bob.
    agent = await create_test_agent(auth_client, user=ALICE)
    alice_session = await _create_session_as(auth_client, agent["id"], ALICE)
    bind = await _patch_runner(auth_client, alice_session["id"], ALICE_RUNNER, ALICE)
    assert bind.status_code == 200, bind.text
    grant = await auth_client.put(
        f"/v1/sessions/{alice_session['id']}/permissions",
        json={"user_id": BOB, "level": LEVEL_READ},
        headers={"X-Forwarded-Email": ALICE},
    )
    assert grant.status_code == 200, grant.text

    # Bob (read-only) forks Alice's session.
    fork_resp = await auth_client.post(
        f"/v1/sessions/{alice_session['id']}/fork",
        json={},
        headers={"X-Forwarded-Email": BOB},
    )
    assert fork_resp.status_code == 201, fork_resp.text
    fork = fork_resp.json()
    fork_id = fork["id"]

    # The fork is a fresh, unbound, idle session owned by Bob.
    assert fork["status"] == "idle"
    # Owner level is required for the owner-only runner bind below; if
    # the fork didn't grant Bob ownership, the resume PATCH would 403.
    assert fork["permission_level"] == LEVEL_OWNER, (
        f"Forker should own the fork, got permission_level={fork['permission_level']}."
    )
    # A non-None runner_id (especially ALICE_RUNNER) would mean the fork
    # inherited the source's runner binding — a cross-user leak.
    assert fork["runner_id"] is None, (
        f"Fork should start unbound, but runner_id={fork['runner_id']!r}. "
        f"If it equals {ALICE_RUNNER!r}, fork_conversation leaked the "
        "source's runner binding to the forker."
    )

    # Bob resumes the clone on his OWN runner — the live-runner-binding
    # step a Web UI clone flow performs after fork.
    resume = await _patch_runner(auth_client, fork_id, BOB_RUNNER, BOB)
    assert resume.status_code == 200, (
        f"Bob binding his own runner to his fork should succeed, got "
        f"{resume.status_code}: {resume.text}."
    )
    # _registered_runner_id returns the trimmed id verbatim, so the
    # bound runner must be Bob's, proving the rebind persisted.
    assert resume.json()["runner_id"] == BOB_RUNNER, (
        f"Resumed fork should report Bob's runner as bound, got {resume.json()['runner_id']!r}."
    )

    # Alice has no grant on Bob's fork, so she cannot bind a runner to
    # it — no access is hidden as 404 (not 403) to avoid leaking
    # existence. A 200 here would mean the fork's access controls
    # didn't isolate it from the source owner.
    alice_intrudes = await _patch_runner(auth_client, fork_id, ALICE_RUNNER, ALICE)
    assert alice_intrudes.status_code == 404, (
        f"Source owner should have no binding control over the forker's "
        f"clone (no access → 404), got {alice_intrudes.status_code}: "
        f"{alice_intrudes.text}."
    )


# ── Tests: No-auth mode backward compatibility ──────────────


async def test_no_auth_runner_listing_shows_all(
    app: FastAPI,
) -> None:
    """Without auth, GET /v1/runners lists all runners.

    Single-user dev mode should not break runner discovery. Runners
    registered without an owner are visible to all callers.
    """
    register_test_runner(app, "runner_dev_a")
    register_test_runner(app, "runner_dev_b")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/v1/runners")

    runner_ids = {r["runner_id"] for r in resp.json()["data"]}
    # Both runners visible without auth.
    assert "runner_dev_a" in runner_ids, "runner_dev_a should be visible in no-auth mode."
    assert "runner_dev_b" in runner_ids, "runner_dev_b should be visible in no-auth mode."
