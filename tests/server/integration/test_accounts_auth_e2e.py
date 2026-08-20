"""End-to-end integration tests for accounts-mode authentication flows.

Exercises the full HTTP flow against a real FastAPI app with accounts
auth enabled, using ``httpx.AsyncClient`` with ``ASGITransport``.

Unlike the unit-level tests in ``tests/server/test_accounts.py`` (which
use ``fastapi.testclient.TestClient``), these tests exercise the async
ASGI pipeline end-to-end, matching how the server runs in production.

Covers: first-run setup, login, login failure, /auth/me, invite +
register, self-serve password change, admin user listing, logout,
and magic-link mint + redeem.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.server.auth import create_auth_provider
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)

pytestmark = pytest.mark.asyncio

# ── Constants ─────────────────────────────────────────────

_ADMIN_USERNAME = "admin"
_ADMIN_PASSWORD = "admin-pw-12345"
_COOKIE_SECRET_HEX = secrets.token_hex(32)


# ── Fixtures ──────────────────────────────────────────────


def _build_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    init_admin_password: str | None,
) -> FastAPI:
    """Build a production-shaped accounts-mode FastAPI app.

    Mirrors ``_build_accounts_app`` from ``test_accounts.py`` but
    returns the raw ``FastAPI`` instance (no ``TestClient`` wrapper)
    so it can be used with ``httpx.ASGITransport``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", _COOKIE_SECRET_HEX)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")
    if init_admin_password is not None:
        monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", init_admin_password)
    else:
        monkeypatch.delenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", _ADMIN_USERNAME)
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-creds"))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_AUTO_OPEN", "0")
    # Strip ambient OIDC issuer so the enable switch resolves to accounts.
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)

    db_url = f"sqlite:///{tmp_path}/test.db"

    from omnigent.db.utils import get_or_create_engine
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    get_or_create_engine(db_url)
    telemetry.init()
    permission_store = SqlAlchemyPermissionStore(db_url)
    agent_store = SqlAlchemyAgentStore(db_url)
    conversation_store = SqlAlchemyConversationStore(db_url)
    file_store = SqlAlchemyFileStore(db_url)
    comment_store = SqlAlchemyCommentStore(db_url)
    host_store = HostStore(db_url)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        agent_cache=agent_cache,
        caps=RuntimeCaps(),
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
    )

    auth_provider = create_auth_provider()
    account_store = SqlAlchemyAccountStore(db_url)
    return create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=comment_store,
        permission_store=permission_store,
        host_store=host_store,
        auth_provider=auth_provider,
        account_store=account_store,
    )


@pytest.fixture()
def accounts_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Accounts-mode app with admin pre-seeded."""
    return _build_app(tmp_path, monkeypatch, init_admin_password=_ADMIN_PASSWORD)


@pytest.fixture()
def setup_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Accounts-mode app with NO admin — first-run setup pending."""
    return _build_app(tmp_path, monkeypatch, init_admin_password=None)


@pytest_asyncio.fixture()
async def client(accounts_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the pre-seeded accounts app."""
    from omnigent.db.utils import clear_engine_cache

    transport = httpx.ASGITransport(app=accounts_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    clear_engine_cache()


@pytest_asyncio.fixture()
async def setup_client(setup_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the needs-setup accounts app."""
    from omnigent.db.utils import clear_engine_cache

    transport = httpx.ASGITransport(app=setup_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    clear_engine_cache()


# ── Helpers ───────────────────────────────────────────────


async def _login(
    client: httpx.AsyncClient,
    username: str,
    password: str,
) -> dict[str, str]:
    """Log in and return the session cookies as a dict."""
    resp = await client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    # Extract cookies from the Set-Cookie header(s).
    cookies = {}
    for cookie_header in resp.headers.get_list("set-cookie"):
        name, _, rest = cookie_header.partition("=")
        value = rest.split(";")[0]
        cookies[name.strip()] = value.strip()
    return cookies


def _cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    """Build a Cookie header dict from a cookies dict."""
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {"cookie": cookie_str}


# ── 1. First-run setup ───────────────────────────────────


async def test_setup_creates_first_admin(
    setup_client: httpx.AsyncClient,
) -> None:
    """POST /auth/setup creates the first admin and returns a session."""
    resp = await setup_client.post(
        "/auth/setup",
        json={"username": "alice", "password": "alice-pw-12345"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["id"] == "alice"
    assert body["user"]["is_admin"] is True
    assert "token" in body
    assert "expires_in" in body
    # A session cookie was set.
    assert any("session" in h.lower() for h in resp.headers.get_list("set-cookie"))


async def test_setup_409_after_first_admin(
    setup_client: httpx.AsyncClient,
) -> None:
    """POST /auth/setup returns 409 once an admin already exists."""
    first = await setup_client.post(
        "/auth/setup",
        json={"username": "alice", "password": "alice-pw-12345"},
    )
    assert first.status_code == 200

    second = await setup_client.post(
        "/auth/setup",
        json={"username": "bob", "password": "bob-pw-123456"},
    )
    assert second.status_code == 409


# ── 2. Login (success) ───────────────────────────────────


async def test_login_returns_session_cookie(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/login with valid creds returns 200 and sets a cookie."""
    resp = await client.post(
        "/auth/login",
        json={"username": _ADMIN_USERNAME, "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["id"] == _ADMIN_USERNAME
    assert body["user"]["is_admin"] is True
    assert "token" in body
    # Session cookie present.
    set_cookie_headers = resp.headers.get_list("set-cookie")
    assert any("session" in h.lower() for h in set_cookie_headers)


# ── 3. Login failure ─────────────────────────────────────


async def test_login_wrong_password_returns_401(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/login with wrong password returns 401."""
    resp = await client.post(
        "/auth/login",
        json={"username": _ADMIN_USERNAME, "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert "invalid" in resp.json()["error"].lower()


async def test_login_unknown_user_returns_401(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/login with unknown user returns 401."""
    resp = await client.post(
        "/auth/login",
        json={"username": "nonexistent", "password": "whatever-12345"},
    )
    assert resp.status_code == 401


# ── 4. Me endpoint ───────────────────────────────────────


async def test_me_with_valid_cookie(
    client: httpx.AsyncClient,
) -> None:
    """GET /auth/me with a valid session cookie returns user info."""
    cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    resp = await client.get("/auth/me", headers=_cookie_header(cookies))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == _ADMIN_USERNAME
    assert body["is_admin"] is True
    assert "created_at" in body


async def test_me_without_cookie_returns_401(
    client: httpx.AsyncClient,
) -> None:
    """GET /auth/me without a session cookie returns 401."""
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


# ── 5. Invite + register flow ────────────────────────────


async def test_invite_and_register_flow(
    client: httpx.AsyncClient,
) -> None:
    """Admin creates an invite, then a new user registers with it."""
    # Login as admin.
    admin_cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    admin_headers = _cookie_header(admin_cookies)

    # Mint an invite.
    invite_resp = await client.post(
        "/auth/invite",
        json={"is_admin": False},
        headers=admin_headers,
    )
    assert invite_resp.status_code == 200, invite_resp.text
    invite_body = invite_resp.json()
    token = invite_body["token"]
    assert "register_url" in invite_body

    # Register a new user with the invite token.
    reg_resp = await client.post(
        "/auth/register",
        json={
            "invite": token,
            "username": "bob",
            "password": "bob-pw-123456",
        },
    )
    assert reg_resp.status_code == 200, reg_resp.text
    reg_body = reg_resp.json()
    assert reg_body["user"]["id"] == "bob"
    assert reg_body["user"]["is_admin"] is False
    assert "token" in reg_body


async def test_invite_requires_admin(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/invite without admin rights returns 403."""
    # First create a non-admin user via invite.
    admin_cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    admin_headers = _cookie_header(admin_cookies)

    invite_resp = await client.post(
        "/auth/invite", json={"is_admin": False}, headers=admin_headers
    )
    assert invite_resp.status_code == 200, invite_resp.text
    token = invite_resp.json()["token"]

    reg_resp = await client.post(
        "/auth/register",
        json={"invite": token, "username": "bob", "password": "bob-pw-123456"},
    )
    assert reg_resp.status_code == 200, reg_resp.text

    # Login as non-admin bob.
    bob_cookies = await _login(client, "bob", "bob-pw-123456")
    bob_headers = _cookie_header(bob_cookies)

    # Bob cannot mint invites.
    resp = await client.post("/auth/invite", json={"is_admin": False}, headers=bob_headers)
    assert resp.status_code == 403


# ── 6. Password change ───────────────────────────────────


async def test_password_change(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/users/me/password updates the password."""
    cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    headers = _cookie_header(cookies)

    new_password = "new-admin-pw-12345"
    resp = await client.post(
        "/auth/users/me/password",
        json={"old_password": _ADMIN_PASSWORD, "new_password": new_password},
        headers=headers,
    )
    assert resp.status_code == 204

    # Old password no longer works.
    old_resp = await client.post(
        "/auth/login",
        json={"username": _ADMIN_USERNAME, "password": _ADMIN_PASSWORD},
    )
    assert old_resp.status_code == 401

    # New password works.
    new_resp = await client.post(
        "/auth/login",
        json={"username": _ADMIN_USERNAME, "password": new_password},
    )
    assert new_resp.status_code == 200


async def test_password_change_wrong_old_password(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/users/me/password with wrong old password returns 401."""
    cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    headers = _cookie_header(cookies)

    resp = await client.post(
        "/auth/users/me/password",
        json={"old_password": "wrong-old-pw", "new_password": "new-pw-12345678"},
        headers=headers,
    )
    assert resp.status_code == 401


# ── 7. Admin user listing ────────────────────────────────


async def test_admin_list_users(
    client: httpx.AsyncClient,
) -> None:
    """GET /auth/users as admin returns the user list."""
    cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    headers = _cookie_header(cookies)

    resp = await client.get("/auth/users", headers=headers)
    assert resp.status_code == 200
    users = resp.json()["users"]
    user_ids = {u["id"] for u in users}
    assert _ADMIN_USERNAME in user_ids


async def test_non_admin_cannot_list_users(
    client: httpx.AsyncClient,
) -> None:
    """GET /auth/users as non-admin returns 403."""
    # Create a non-admin user first.
    admin_cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    admin_headers = _cookie_header(admin_cookies)
    invite_resp = await client.post(
        "/auth/invite", json={"is_admin": False}, headers=admin_headers
    )
    assert invite_resp.status_code == 200, invite_resp.text
    token = invite_resp.json()["token"]
    reg_resp = await client.post(
        "/auth/register",
        json={"invite": token, "username": "bob", "password": "bob-pw-123456"},
    )
    assert reg_resp.status_code == 200, reg_resp.text

    bob_cookies = await _login(client, "bob", "bob-pw-123456")
    bob_headers = _cookie_header(bob_cookies)
    resp = await client.get("/auth/users", headers=bob_headers)
    assert resp.status_code == 403


# ── 8. Logout ─────────────────────────────────────────────


async def test_logout_clears_session_cookie(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/logout returns 204 and clears the session cookie."""
    cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    headers = _cookie_header(cookies)

    resp = await client.post("/auth/logout", headers=headers)
    assert resp.status_code == 204

    # The Set-Cookie header should expire/delete the session cookie.
    set_cookie_headers = resp.headers.get_list("set-cookie")
    assert any("session" in h.lower() for h in set_cookie_headers)
    # Note: the JWT is stateless, so resending the raw cookie header
    # would still authenticate. The test verifies the Set-Cookie
    # deletion header was sent (which clears the browser cookie).


# ── 9. Magic link ────────────────────────────────────────


async def test_magic_link_mint_and_redeem(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/magic mints a token; GET /auth/magic/redeem consumes it."""
    cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    headers = _cookie_header(cookies)

    # Mint a magic link.
    mint_resp = await client.post("/auth/magic", headers=headers)
    assert mint_resp.status_code == 200
    mint_body = mint_resp.json()
    assert "redeem_url" in mint_body
    assert "expires_at" in mint_body

    # Extract the path + query from the redeem URL.
    parsed = urlparse(mint_body["redeem_url"])
    path_q = f"{parsed.path}?{parsed.query}"

    # Redeem the magic link (follow_redirects=False to inspect the redirect).
    redeem_resp = await client.get(path_q, follow_redirects=False)
    assert redeem_resp.status_code == 302
    assert redeem_resp.headers["location"] == "/"
    # A session cookie was set on the redirect response.
    assert any("session" in h.lower() for h in redeem_resp.headers.get_list("set-cookie"))


async def test_magic_link_is_single_use(
    client: httpx.AsyncClient,
) -> None:
    """A second redeem of the same magic token redirects to login with error."""
    cookies = await _login(client, _ADMIN_USERNAME, _ADMIN_PASSWORD)
    headers = _cookie_header(cookies)

    mint_resp = await client.post("/auth/magic", headers=headers)
    assert mint_resp.status_code == 200, mint_resp.text
    redeem_url = mint_resp.json()["redeem_url"]
    parsed = urlparse(redeem_url)
    path_q = f"{parsed.path}?{parsed.query}"

    # First redeem succeeds.
    first = await client.get(path_q, follow_redirects=False)
    assert first.status_code == 302
    assert first.headers["location"] == "/"

    # Second redeem fails (token already consumed).
    second = await client.get(path_q, follow_redirects=False)
    assert second.status_code == 302
    assert "magic=expired" in second.headers["location"]


async def test_unauthenticated_cannot_mint_magic_link(
    client: httpx.AsyncClient,
) -> None:
    """POST /auth/magic without a session returns 401."""
    resp = await client.post("/auth/magic")
    assert resp.status_code == 401
