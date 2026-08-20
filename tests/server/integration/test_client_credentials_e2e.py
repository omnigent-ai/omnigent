"""End-to-end integration for the OAuth 2.0 client-credentials grant.

Drives a production-shaped FastAPI app in accounts mode, using
``httpx.AsyncClient`` + ``ASGITransport`` so the async ASGI pipeline runs the
way it does in production. Most cases leave the device grant off, so the
client-credentials grant owns ``POST /oauth/token``; the last two turn it on to
cover standing down. Proves the whole path:

- with no machine client configured, ``POST /oauth/token`` is not routed at
  all — the grant is opt-in and default-off;
- a valid client mints a bearer token;
- that token authenticates the delegated session APIs (``/v1/sessions*``,
  ``/v1/agents``) but is rejected on the admin / user-management paths;
- the minted machine principal — a brand-new identity — can create a session
  (``ensure_user`` + owner grant), operate it (post events, resolve an
  elicitation), and delete it (the owner path);
- a scope token allowed on one path is NOT then accepted on a disallowed
  path (the token-keyed credential cache can't bypass the allowlist);
- with the device grant enabled, a configured machine client stands down
  rather than racing it for the shared endpoint;
- a half-configured machine client fails startup even while standing down, so
  a typo cannot hide until the device grant is switched off.

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
    :param device_grant: When True, enable the device grant, which owns
        ``POST /oauth/token`` and makes this grant stand down.
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
    # Device grant OFF by default: the client-credentials grant then owns
    # /oauth/token. Turned on, the device grant owns that path instead and this
    # grant stands down.
    if device_grant:
        monkeypatch.setenv("OMNIGENT_DEVICE_GRANT_ENABLED", "1")
    else:
        monkeypatch.delenv("OMNIGENT_DEVICE_GRANT_ENABLED", raising=False)
    # Machine client — the secret is stored only as its keyed hash. Leaving
    # these unset is the grant's opt-out, and leaves /oauth/token unrouted.
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


async def test_default_deployment_does_not_route_the_token_endpoint(
    unconfigured_env: httpx.AsyncClient,
) -> None:
    """BLOCKING contract: mounting this grant must not route /oauth/token for everyone.

    With no machine client configured the router is never built, so an
    accounts-mode deployment that never opted in sees the path exactly as it
    was before this grant existed: a 404 when nothing else claims it, or a 405
    when a built web SPA is mounted at ``/`` (its catch-all serves GET only).
    Either way no OAuth handler runs and no OAuth error shape comes back.
    """
    resp = await unconfigured_env.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code in (404, 405), resp.text
    assert "error" not in resp.json()


async def test_token_authenticates_session_apis_and_rejects_admin(env: SimpleNamespace) -> None:
    """The minted token reaches the delegated session APIs, not admin surfaces."""
    token = await _mint_token(env.client)
    auth = {"Authorization": f"Bearer {token}"}

    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200
    assert (await env.client.get("/v1/agents", headers=auth)).status_code == 200
    # /auth/users is not on the delegated allowlist → rejected at the door.
    assert (await env.client.get("/auth/users", headers=auth)).status_code in (401, 403)


async def test_bad_client_is_rejected(env: SimpleNamespace) -> None:
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


async def test_token_is_refused_on_off_allowlist_paths(env: SimpleNamespace) -> None:
    """The machine token cannot reach anything outside the delegated allowlist."""
    token = await _mint_token(env.client)
    auth = {"Authorization": f"Bearer {token}"}

    for path in _FORBIDDEN_PATHS:
        resp = await env.client.get(path, headers=auth)
        assert resp.status_code in (401, 403), f"{path} answered {resp.status_code}"


async def test_scope_token_cache_does_not_bypass_allowlist(env: SimpleNamespace) -> None:
    """A prior allowed call must not let a later disallowed path through.

    End-to-end mirror of the unit cache-bypass test: the credential cache is
    keyed by token, so caching a scope token would skip the allowlist on a
    replay. The same token, same process, must still be refused on
    ``/auth/users`` after succeeding on ``/v1/sessions``.
    """
    token = await _mint_token(env.client)
    auth = {"Authorization": f"Bearer {token}"}

    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200
    assert (await env.client.get("/auth/users", headers=auth)).status_code in (401, 403)
    # And once more, to be sure repetition never warms a bypassing cache entry.
    assert (await env.client.get("/auth/users", headers=auth)).status_code in (401, 403)
    assert (await env.client.get("/v1/sessions", headers=auth)).status_code == 200


async def test_machine_client_stands_down_when_the_device_grant_owns_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured machine client does not mint when the device grant is enabled.

    Both grants answer ``POST /oauth/token``, so only one may own it. The device
    grant wins, and this one stands down rather than racing it. The configured
    credentials must therefore be refused as an unsupported grant type instead of
    minting a token.
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
    clear_engine_cache()
    assert resp.status_code != 200, "the machine client must not mint while standing down"
    assert "access_token" not in resp.text
    assert resp.json()["error"] == "unsupported_grant_type"


async def test_malformed_machine_config_still_fails_startup_when_standing_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-configured machine client is a startup error even while standing down.

    The config is parsed whether or not this grant will own the path, so a deploy
    that misspelled one variable fails immediately rather than coming up healthy
    and revealing the mistake only once the device grant is switched off.
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
