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
from omnigent.stores.host_store import HostStore
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
        host_store=HostStore(db_uri),
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


def _invite(db_uri: str, session_id: str, user: str, *, level: int = 1) -> None:
    """Grant ``user`` a non-owner role on an existing session (an invite).

    :param db_uri: Per-test SQLite URI.
    :param session_id: The session to share.
    :param user: The invitee, e.g. ``"btallman@example.com"``.
    :param level: Grant level (1=read, 2=edit, 3=manage).
    """
    perm = SqlAlchemyPermissionStore(db_uri)
    perm.ensure_user(user)
    perm.grant(user, session_id, level=level)


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


async def test_list_users_includes_host_counts(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Each user row carries owned host counts (total + the live subset)."""
    _make_user(db_uri, "boss@example.com", is_admin=True)
    # A real (logged-in) user row; with no grants she stays visible, and her
    # owned hosts are counted even though she owns no session.
    _make_user(db_uri, "alice@example.com")
    hosts = HostStore(db_uri)
    # upsert_on_connect registers a fresh, online host.
    hosts.upsert_on_connect("host_a1", "alice-laptop", "alice@example.com")
    hosts.upsert_on_connect("host_a2", "alice-desktop", "alice@example.com")
    # Mark one offline so online_host_count < host_count.
    hosts.set_offline("host_a2")

    resp = await auth_client.get("/v1/admin/users", headers=_headers("boss@example.com"))

    assert resp.status_code == 200
    by_id = {u["user_id"]: u for u in resp.json()["users"]}
    alice = by_id["alice@example.com"]
    assert alice["host_count"] == 2
    assert alice["online_host_count"] == 1
    # A user with no hosts reports zero, not a missing field.
    assert by_id["boss@example.com"]["host_count"] == 0
    assert by_id["boss@example.com"]["online_host_count"] == 0


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


async def test_list_user_sessions_includes_host(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Each session row reports its bound host: friendly name + liveness."""
    _make_user(db_uri, "boss@example.com", is_admin=True)
    bound = _make_session_for(db_uri, "alice@example.com")
    unbound = _make_session_for(db_uri, "alice@example.com")
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect("host_a1", "alice-laptop", "alice@example.com")
    SqlAlchemyConversationStore(db_uri).set_host_id(bound, "host_a1", workspace="/w")

    resp = await auth_client.get(
        "/v1/admin/users/alice@example.com/sessions",
        headers=_headers("boss@example.com"),
    )

    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()["sessions"]}
    assert by_id[bound]["host"] == "alice-laptop"
    assert by_id[bound]["host_online"] is True
    # An unbound session reports no host.
    assert by_id[unbound]["host"] is None
    assert by_id[unbound]["host_online"] is False


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
    sess = body["sessions"][0]
    assert sess["cost_usd"] == pytest.approx(2.5)
    assert sess["total_tokens"] == 4200
    # alice owns this session.
    assert sess["role"] == "owner"
    assert sess["owner"] == "alice@example.com"
    assert sess["is_owner"] is True
    assert body["totals"]["cost_usd"] == pytest.approx(2.5)
    assert body["totals"]["total_tokens"] == 4200


async def test_cost_is_attributed_to_owner_not_invitee(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A user merely invited to a session isn't charged; the session shows role/owner.

    Reproduces the btallman case: invited to a session owned by alice, btallman
    must show a $0 rollup, and the session row must mark them a read-role
    non-owner whose owner is alice.
    """
    _make_user(db_uri, "boss@example.com", is_admin=True)
    conv_id = _make_session_for(db_uri, "alice@example.com", cost_usd=5.0, total_tokens=8000)
    _invite(db_uri, conv_id, "btallman@example.com", level=1)  # read-only invite

    # User-list rollup: cost goes to alice; btallman (invite-only phantom) is
    # hidden entirely, and counted under "hidden".
    users_resp = await auth_client.get("/v1/admin/users", headers=_headers("boss@example.com"))
    payload = users_resp.json()
    by_id = {u["user_id"]: u for u in payload["users"]}
    assert by_id["alice@example.com"]["cost_usd"] == pytest.approx(5.0)
    assert by_id["alice@example.com"]["session_count"] == 1
    assert "btallman@example.com" not in by_id
    assert payload["hidden"] == 1

    # btallman's session view still works directly: the session is labeled as a
    # read-role invite owned by alice, and the rollup total stays $0.
    sess_resp = await auth_client.get(
        "/v1/admin/users/btallman@example.com/sessions",
        headers=_headers("boss@example.com"),
    )
    body = sess_resp.json()
    sess = next(s for s in body["sessions"] if s["id"] == conv_id)
    assert sess["role"] == "read"
    assert sess["owner"] == "alice@example.com"
    assert sess["is_owner"] is False
    assert body["totals"]["cost_usd"] == pytest.approx(0.0)
    assert body["totals"]["session_count"] == 0


async def test_logged_in_idle_user_and_admin_not_hidden(
    auth_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Hiding targets ONLY invite-only phantoms — not idle real users or admins.

    A user who logged in but owns/joined nothing (no grants) and an admin who
    owns nothing both stay visible; only the invited-but-owns-nothing phantom
    is filtered.
    """
    _make_user(db_uri, "boss@example.com", is_admin=True)
    _make_user(db_uri, "idle@example.com", is_admin=False)  # logged in, no grants
    _make_user(db_uri, "ghostadmin@example.com", is_admin=True)  # admin, no sessions
    conv_id = _make_session_for(db_uri, "alice@example.com", cost_usd=1.0)
    _invite(db_uri, conv_id, "phantom@example.com", level=2)  # invited, owns nothing

    resp = await auth_client.get("/v1/admin/users", headers=_headers("boss@example.com"))
    payload = resp.json()
    ids = {u["user_id"] for u in payload["users"]}

    assert "idle@example.com" in ids  # real login, kept
    assert "ghostadmin@example.com" in ids  # admin, kept
    assert "alice@example.com" in ids  # owns a session, kept
    assert "phantom@example.com" not in ids  # invite-only phantom, hidden
    assert payload["hidden"] == 1


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
