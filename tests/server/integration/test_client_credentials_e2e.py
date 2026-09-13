"""End-to-end integration for the OAuth 2.0 client-credentials grant.

Drives a production-shaped FastAPI app in accounts mode, using
``httpx.AsyncClient`` + ``ASGITransport`` so the async ASGI pipeline runs the
way it does in production. ``POST /oauth/token`` is mounted either way — the
app serves login-issued refresh grants there regardless — and this grant is one
``grant_type`` branch of it. Proves the whole path:

- with no machine client configured, ``client_credentials`` is refused as an
  unsupported grant type — the grant is opt-in and default-off;
- a valid client mints a bearer token;
- that token authenticates the delegated session APIs (``/v1/sessions*``,
  ``/v1/agents``) but is rejected on the admin / user-management paths;
- a login-grant token minted through the real ``/auth/login`` + refresh flow in
  the SAME app KEEPS full authority — confinement keys off ``scope``, not off
  ``grant_id``'s absence;
- the minted machine principal — a brand-new identity — can create a session
  (``ensure_user`` + owner grant), operate it (post events, resolve an
  elicitation), and delete it (the owner path);
- a scope token allowed on one path is NOT then accepted on a disallowed
  path (the token-keyed credential cache can't bypass the allowlist);
- with the device grant enabled the machine client still mints — both grants
  dispatch from the one token endpoint rather than racing for the route;
- a half-configured machine client fails startup, so a typo cannot hide.

Network-free: accounts mode needs no IdP discovery.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.device_grant_store import hash_secret
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio

_COOKIE_SECRET_HEX = "ab" * 32
_COOKIE_SECRET = bytes.fromhex(_COOKIE_SECRET_HEX)
_CLIENT_ID = "svc-integration"
_CLIENT_SECRET = "top-secret-machine-key"
_MACHINE_SUB = "machine@example.com"


def _build_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_client: bool = True,
    device_grant: bool = False,
    partial_machine_config: bool = False,
) -> SimpleNamespace:
    """Build an accounts-mode app, with or without the machine client configured.

    :param tmp_path: Per-test directory for HOME, the sqlite db and artifacts.
    :param monkeypatch: Fixture used to set the server's env.
    :param machine_client: When False, leave every ``OMNIGENT_MACHINE_*``
        variable unset — the grant's default-off state.
    :param device_grant: When True, enable the device grant, which shares
        ``POST /oauth/token`` with this grant — both dispatch from it.
    :param partial_machine_config: When True, set only one ``OMNIGENT_MACHINE_*``
        variable, which the config parser rejects as an operator error.
    :returns: The built app and its permission store.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", _COOKIE_SECRET_HEX)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", "admin-pw-12345")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-creds"))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_AUTO_OPEN", "0")
    # Device grant OFF by default. Either way /oauth/token is mounted, for
    # login-issued refresh grants; turning the device flow on just adds its own
    # grant types and endpoints beside this one.
    if device_grant:
        monkeypatch.setenv("OMNIGENT_DEVICE_GRANT_ENABLED", "1")
    else:
        monkeypatch.delenv("OMNIGENT_DEVICE_GRANT_ENABLED", raising=False)
    # Machine client — the secret is stored only as its keyed hash. Leaving
    # these unset is the grant's opt-out: /oauth/token stays mounted and
    # answers client_credentials with unsupported_grant_type.
    for var in (
        "OMNIGENT_MACHINE_CLIENT_ID",
        "OMNIGENT_MACHINE_CLIENT_SECRET_HASH",
        "OMNIGENT_MACHINE_SUB",
        "OMNIGENT_MACHINE_TOKEN_TTL",
    ):
        monkeypatch.delenv(var, raising=False)
    if machine_client:
        monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_ID", _CLIENT_ID)
        monkeypatch.setenv(
            "OMNIGENT_MACHINE_CLIENT_SECRET_HASH", hash_secret(_CLIENT_SECRET, _COOKIE_SECRET)
        )
        monkeypatch.setenv("OMNIGENT_MACHINE_SUB", _MACHINE_SUB)
        monkeypatch.setenv("OMNIGENT_MACHINE_TOKEN_TTL", "1800")
    if partial_machine_config:
        monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_ID", _CLIENT_ID)

    db_url = f"sqlite:///{tmp_path}/test.db"
    from omnigent.db.utils import get_or_create_engine
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

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
    app = create_app(
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
    return SimpleNamespace(app=app, permission_store=permission_store)


@pytest_asyncio.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    """The app, its permission store, and an in-process HTTP client."""
    from omnigent.db.utils import clear_engine_cache

    built = _build_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            app=built.app,
            permission_store=built.permission_store,
        )
    clear_engine_cache()


@pytest_asyncio.fixture
async def unconfigured_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The same app with no machine client configured — the default deployment."""
    from omnigent.db.utils import clear_engine_cache

    built = _build_app(tmp_path, monkeypatch, machine_client=False)
    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    clear_engine_cache()


async def _mint_token(client: httpx.AsyncClient) -> str:
    """Run the grant and return the minted access token."""
    resp = await client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 1800
    token: str = body["access_token"]
    return token


async def test_default_deployment_does_not_serve_the_grant(
    unconfigured_env: httpx.AsyncClient,
) -> None:
    """The grant stays off for a deployment that never opted in.

    ``/oauth/token`` itself is routed regardless — the app mounts it for
    login-issued refresh grants — so the default-off signal is the grant type
    being refused, with no token minted and no credential even examined.
    """
    resp = await unconfigured_env.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "unsupported_grant_type"
    assert "access_token" not in resp.text


async def test_token_authenticates_session_apis_and_rejects_admin(env: SimpleNamespace) -> None:
    """The minted token reaches the delegated session APIs, not admin surfaces."""
    token = await _mint_token(env.client)
    auth = {"Authorization": f"Bearer {token}"}

    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200
    assert (await env.client.get("/v1/agents", headers=auth)).status_code == 200
    # /auth/users is not on the delegated allowlist → rejected at the door.
    assert (await env.client.get("/auth/users", headers=auth)).status_code in (401, 403)


async def _login_grant_token(client: httpx.AsyncClient) -> str:
    """Run the real login + refresh flow and return a login-grant access token.

    ``/auth/login`` with ``issue_refresh`` hands back refresh material; trading
    it at ``/oauth/token`` mints the first-party token shape — ``grant_id``
    present, ``scope`` absent.
    """
    login = await client.post(
        "/auth/login",
        json={"username": "admin", "password": "admin-pw-12345", "issue_refresh": True},
    )
    assert login.status_code == 200, login.text
    refresh_token = login.json()["refresh_token"]

    refreshed = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    assert refreshed.status_code == 200, refreshed.text
    token: str = refreshed.json()["access_token"]
    # Login set a session cookie, and the auth layer reads the cookie BEFORE
    # the bearer header. Drop it so each request below is decided by the token
    # it actually carries.
    client.cookies.clear()
    return token


async def test_login_grant_keeps_full_authority_beside_the_machine_grant(
    env: SimpleNamespace,
) -> None:
    """The two token shapes must not swap authority.

    In one app, one process: a machine token (``scope``, no ``grant_id``) is
    confined to the delegated allowlist, while a login-grant token
    (``grant_id``, no ``scope``) keeps the authority of the session JWT it
    renewed. Confinement keys off ``scope`` being PRESENT — keying it off
    ``grant_id`` being ABSENT inverts both rows at once.
    """
    machine_auth = {"Authorization": f"Bearer {await _mint_token(env.client)}"}
    login_auth = {"Authorization": f"Bearer {await _login_grant_token(env.client)}"}

    # Both reach the allowlisted session APIs.
    assert (await env.client.get("/v1/sessions", headers=machine_auth)).status_code == 200
    assert (await env.client.get("/v1/sessions", headers=login_auth)).status_code == 200

    # /v1/me is off the allowlist and NOT separately admin-gated, so it
    # separates "confined by the allowlist" from "allowed but refused later" —
    # which /auth/users cannot, since it 403s a non-admin either way.
    assert (await env.client.get("/v1/me", headers=machine_auth)).status_code in (401, 403)
    assert (await env.client.get("/v1/me", headers=login_auth)).status_code == 200

    # The admin surface: reachable only by the full-authority login grant.
    assert (await env.client.get("/auth/users", headers=machine_auth)).status_code in (401, 403)
    assert (await env.client.get("/auth/users", headers=login_auth)).status_code == 200


async def test_bad_client_is_rejected(env: SimpleNamespace) -> None:
    """A wrong secret is 401 invalid_client; an unknown grant type is 400."""
    bad = await env.client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": "wrong",
        },
    )
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_client"

    other = await env.client.post("/oauth/token", data={"grant_type": "password"})
    assert other.status_code == 400 and other.json()["error"] == "unsupported_grant_type"


async def test_machine_client_owns_and_operates_its_own_session(env: SimpleNamespace) -> None:
    """The brand-new principal can create, operate, and delete its session.

    Create runs ``ensure_user`` + an owner grant, so the machine client is
    LEVEL_OWNER on the session it made and passes every owner/edit gate that
    follows.
    """
    token = await _mint_token(env.client)
    auth = {"Authorization": f"Bearer {token}"}

    # Create (multipart bundled create) — the ensure_user +
    # LEVEL_OWNER-on-create path. The principal has never been seen before.
    bundle = build_agent_bundle(name="cc-machine-agent")
    created = await env.client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]

    # ensure_user + owner grant actually landed for the machine principal.
    grant = env.permission_store.get(_MACHINE_SUB, session_id)
    assert grant is not None and grant.level >= LEVEL_OWNER

    # Owner can read its own session's agent.
    agent = await env.client.get(f"/v1/sessions/{session_id}/agent", headers=auth)
    assert agent.status_code == 200

    # Post an event (owner satisfies the LEVEL_EDIT gate — not rejected).
    # Bounded above too: a 500 is a failure, not a pass, for "not rejected".
    posted = await env.client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "interrupt", "data": {}},
        headers=auth,
    )
    assert posted.status_code not in (401, 403) and posted.status_code < 500, posted.text

    # Resolve an elicitation (owner passes the gate; a missing elicitation
    # degrades gracefully rather than being an authz failure).
    resolved = await env.client.post(
        f"/v1/sessions/{session_id}/elicitations/{secrets.token_hex(8)}/resolve",
        json={"action": "cancel"},
        headers=auth,
    )
    assert resolved.status_code not in (401, 403) and resolved.status_code < 500, resolved.text

    # Delete its own session (the owner-only path).
    deleted = await env.client.delete(f"/v1/sessions/{session_id}", headers=auth)
    assert deleted.status_code == 200, deleted.text


# Paths off the delegated allowlist that a machine token must never reach:
# the admin user list, and the identity endpoint the auth layer keeps out of
# the allowlist. Named individually rather than snapshotted as a route
# inventory — the allowlist is fail-closed, so what this grant has to prove is
# that off-allowlist paths stay shut, not that the app's route table is frozen.
_FORBIDDEN_PATHS = ("/auth/users", "/v1/me")

# The subset of the above that actually DISCRIMINATES. /auth/users is
# separately admin-gated, so it answers 403 for an unauthenticated caller, a
# confined token, and a full-authority non-admin token alike — asserting on it
# alone would pass with confinement deleted. /v1/me is off the allowlist and
# not admin-gated, so only confinement can refuse it.
_CONFINEMENT_PROBE = "/v1/me"


async def test_token_is_refused_on_off_allowlist_paths(env: SimpleNamespace) -> None:
    """The machine token cannot reach anything outside the delegated allowlist.

    The probe carries its own positive control: the same path answers 200 to a
    full-authority login-grant token, so a refusal here is confinement doing
    the work rather than the endpoint refusing everyone.
    """
    machine_auth = {"Authorization": f"Bearer {await _mint_token(env.client)}"}

    for path in _FORBIDDEN_PATHS:
        resp = await env.client.get(path, headers=machine_auth)
        assert resp.status_code in (401, 403), f"{path} answered {resp.status_code}"

    login_auth = {"Authorization": f"Bearer {await _login_grant_token(env.client)}"}
    control = await env.client.get(_CONFINEMENT_PROBE, headers=login_auth)
    assert control.status_code == 200, (
        f"positive control failed: {_CONFINEMENT_PROBE} answered {control.status_code} "
        "to a full-authority token, so the refusals above prove nothing"
    )


async def test_scope_token_cache_does_not_bypass_allowlist(env: SimpleNamespace) -> None:
    """A prior allowed call must not let a later disallowed path through.

    End-to-end mirror of the unit cache-bypass test: the credential cache is
    keyed by token, so caching a scope token would skip the allowlist on a
    replay. The same token, same process, must still be refused on
    :data:`_CONFINEMENT_PROBE` after succeeding on ``/v1/sessions``.

    Probes ``/v1/me`` rather than ``/auth/users``: the latter is separately
    admin-gated and refuses every non-admin caller, so it would answer 403
    whether or not the allowlist ran. The positive control below pins that
    distinction — the same path answers 200 to a full-authority token.
    """
    auth = {"Authorization": f"Bearer {await _mint_token(env.client)}"}

    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200
    assert (await env.client.get(_CONFINEMENT_PROBE, headers=auth)).status_code in (401, 403)
    # And once more, to be sure repetition never warms a bypassing cache entry.
    assert (await env.client.get(_CONFINEMENT_PROBE, headers=auth)).status_code in (401, 403)
    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200

    # Positive control: without it, deleting confinement outright would leave
    # this test green.
    login_auth = {"Authorization": f"Bearer {await _login_grant_token(env.client)}"}
    control = await env.client.get(_CONFINEMENT_PROBE, headers=login_auth)
    assert control.status_code == 200, (
        f"positive control failed: {_CONFINEMENT_PROBE} answered {control.status_code} "
        "to a full-authority token, so the refusals above prove nothing"
    )


async def test_machine_client_still_mints_with_the_device_grant_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device grant must not shadow the machine grant.

    Both used to claim ``POST /oauth/token`` with their own router. FastAPI
    registers duplicates and resolves first-match-wins with no warning, so
    whichever lost went quiet behind a startup log still calling it enabled.
    They now share one endpoint and dispatch on ``grant_type``, so enabling the
    device flow leaves the machine client minting.
    """
    from omnigent.db.utils import clear_engine_cache

    built = _build_app(tmp_path, monkeypatch, machine_client=True, device_grant=True)
    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
            },
        )
        # The device flow's own endpoints are up in the same app.
        device = await client.post(
            "/oauth/device/authorize",
            data={"client_id": "some-device-client"},
        )
    clear_engine_cache()
    assert resp.status_code == 200, resp.text
    assert resp.json()["token_type"] == "Bearer"
    assert resp.json()["scope"] == "sessions"
    assert device.status_code == 200, device.text


async def test_malformed_machine_config_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-configured machine client is a startup error, not a silent off.

    An operator who set one variable meant to enable the grant, so the deploy
    fails immediately rather than coming up healthy with the grant absent.
    """
    from omnigent.db.utils import clear_engine_cache

    with pytest.raises(RuntimeError, match="must all be set"):
        _build_app(
            tmp_path,
            monkeypatch,
            machine_client=False,
            device_grant=True,
            partial_machine_config=True,
        )
    clear_engine_cache()
