"""Integration tests for the admin routes (user list + per-user sessions)
and the ``is_admin`` field on ``GET /v1/me``.

Uses a real ``SqlAlchemyPermissionStore`` + ``SqlAlchemyConversationStore``
so the full request -> store -> response pipeline is exercised, in header
auth mode (the multi-user surface admins use).
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
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with header auth + permission store (multi-user admin surface).

    :param runtime_init: Initializes the runtime with a mock LLM.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts.
    :returns: A :class:`FastAPI` instance with the admin routes mounted.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app.

    :param auth_app: The admin-routes app.
    :param mock_llm: Controllable mock LLM — released on teardown.
    :param tmp_path: Pytest temp dir for the harness process manager.
    :yields: A ready-to-use :class:`httpx.AsyncClient`.
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


def _headers(email: str) -> dict[str, str]:
    """Headers simulating an authenticated user.

    :param email: The user email to present.
    :returns: Dict with the ``X-Forwarded-Email`` header.
    """
    return {"X-Forwarded-Email": email}


def _make_user(db_uri: str, email: str, *, is_admin: bool = False) -> None:
    """Seed a user row.

    :param db_uri: Per-test SQLite URI.
    :param email: User email to create.
    :param is_admin: Whether the user is an admin.
    """
    SqlAlchemyPermissionStore(db_uri).ensure_user(email, is_admin=is_admin)


def _make_session_for(
    db_uri: str,
    owner: str,
    *,
    cost_usd: float | None = None,
    total_tokens: int | None = None,
) -> str:
    """Create a conversation, grant ``owner`` owner access, optionally set usage.

    :param db_uri: Per-test SQLite URI.
    :param owner: The user to make owner, e.g. ``"alice@example.com"``.
    :param cost_usd: If set, written into the session's usage rollup.
    :param total_tokens: If set, written into the session's usage rollup.
    :returns: The new conversation id.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    perm = SqlAlchemyPermissionStore(db_uri)
    perm.ensure_user(owner)
    perm.grant(owner, conv.id, level=LEVEL_OWNER)
    if cost_usd is not None or total_tokens is not None:
        conv_store.set_session_usage(
            conv.id,
            {"total_cost_usd": cost_usd or 0.0, "total_tokens": total_tokens or 0},
        )
    return conv.id


# ── /v1/me is_admin ───────────────────────────────────────────────────────────


async def test_me_reports_admin_true(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """``GET /v1/me`` reports ``is_admin: true`` for an admin caller."""
    _make_user(db_uri, "boss@example.com", is_admin=True)
    resp = await auth_client.get("/v1/me", headers=_headers("boss@example.com"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "boss@example.com"
    assert body["is_admin"] is True


async def test_me_reports_admin_false(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """``GET /v1/me`` reports ``is_admin: false`` for a non-admin caller."""
    _make_user(db_uri, "peon@example.com", is_admin=False)
    resp = await auth_client.get("/v1/me", headers=_headers("peon@example.com"))
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False


# ── GET /v1/admin/users ───────────────────────────────────────────────────────


async def test_list_users_as_admin(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """An admin sees every real user with the correct admin flag."""
    _make_user(db_uri, "boss@example.com", is_admin=True)
    _make_user(db_uri, "alice@example.com", is_admin=False)

    resp = await auth_client.get("/v1/admin/users", headers=_headers("boss@example.com"))

    assert resp.status_code == 200
    users = {u["user_id"]: u["is_admin"] for u in resp.json()["users"]}
    assert users["boss@example.com"] is True
    assert users["alice@example.com"] is False
    # Reserved sentinels are not real users.
    assert "local" not in users
    assert "__public__" not in users


async def test_list_users_includes_cost_rollup(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Each user row carries a cost/token rollup summed across their sessions."""
    _make_user(db_uri, "boss@example.com", is_admin=True)
    _make_session_for(db_uri, "alice@example.com", cost_usd=1.25, total_tokens=1000)
    _make_session_for(db_uri, "alice@example.com", cost_usd=0.75, total_tokens=500)

    resp = await auth_client.get("/v1/admin/users", headers=_headers("boss@example.com"))

    assert resp.status_code == 200
    by_id = {u["user_id"]: u for u in resp.json()["users"]}
    alice = by_id["alice@example.com"]
    assert alice["cost_usd"] == pytest.approx(2.0)
    assert alice["total_tokens"] == 1500
    assert alice["session_count"] == 2


async def test_list_users_forbidden_for_non_admin(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A non-admin gets 403 from the admin user list."""
    _make_user(db_uri, "peon@example.com", is_admin=False)
    resp = await auth_client.get("/v1/admin/users", headers=_headers("peon@example.com"))
    assert resp.status_code == 403


# (No unauthenticated 401 test here: in *header* auth mode a missing identity
# header falls back to the ``local`` admin identity by design. The 401 path is
# OIDC-mode-specific and is covered by the OIDC route tests.)


# ── GET /v1/admin/users/{user_id}/sessions ────────────────────────────────────


async def test_list_user_sessions_as_admin(auth_client: httpx.AsyncClient, db_uri: str) -> None:
    """An admin sees a target user's sessions."""
    _make_user(db_uri, "boss@example.com", is_admin=True)
    conv_id = _make_session_for(db_uri, "alice@example.com")

    resp = await auth_client.get(
        "/v1/admin/users/alice@example.com/sessions",
        headers=_headers("boss@example.com"),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "alice@example.com"
    ids = [s["id"] for s in body["sessions"]]
    assert conv_id in ids


async def test_list_user_sessions_includes_cost(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Each session row carries its own cost/tokens, and totals roll them up."""
    _make_user(db_uri, "boss@example.com", is_admin=True)
    _make_session_for(db_uri, "alice@example.com", cost_usd=2.5, total_tokens=4200)

    resp = await auth_client.get(
        "/v1/admin/users/alice@example.com/sessions",
        headers=_headers("boss@example.com"),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sessions"][0]["cost_usd"] == pytest.approx(2.5)
    assert body["sessions"][0]["total_tokens"] == 4200
    assert body["totals"]["cost_usd"] == pytest.approx(2.5)
    assert body["totals"]["total_tokens"] == 4200


async def test_list_user_sessions_forbidden_for_non_admin(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A non-admin cannot browse another user's sessions."""
    _make_user(db_uri, "peon@example.com", is_admin=False)
    _make_session_for(db_uri, "alice@example.com")
    resp = await auth_client.get(
        "/v1/admin/users/alice@example.com/sessions",
        headers=_headers("peon@example.com"),
    )
    assert resp.status_code == 403
