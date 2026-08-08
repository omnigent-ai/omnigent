"""Tests for the OAuth 2.0 Device Authorization Grant (RFC 8628).

Two layers:

1. :class:`omnigent.server.device_grant_store.DeviceGrantStore` — the
   atomic single-use / rotation / revocation invariants that back the
   flow's security (unit, against a real SQLite DB).
2. The ``/oauth/*`` routes end-to-end via a FastAPI TestClient in
   accounts mode — authorize → consent → approve → poll → refresh →
   revoke, plus the scope and revocation enforcement on the issued
   delegated access token.

See ``designs/DEVICE_AUTH.md``.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnigent.server.device_grant_store import DeviceGrantStore, hash_secret

_KEY = b"k" * 32


# ── Router mount guard (unit) ─────────────────────────────────────


def test_router_factory_rejects_header_mode(tmp_path: Path) -> None:
    """Header mode has no server-mintable session: identity is asserted by an
    upstream proxy, so there is nothing to delegate FROM and no login to bounce
    a consenting browser through. The factory must refuse to build."""
    from types import SimpleNamespace

    from omnigent.server.routes.device_auth import create_device_auth_router

    provider = SimpleNamespace(_source="header")
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    with pytest.raises(RuntimeError, match="no server-minted session"):
        create_device_auth_router(provider, store)  # type: ignore[arg-type]


def _oidc_provider(provider_type: str) -> object:
    """A minimal OIDC-shaped provider stub for the mount-guard tests."""
    from types import SimpleNamespace

    return SimpleNamespace(
        _source="oidc",
        _oidc_config=SimpleNamespace(
            cookie_secret=b"o" * 32,
            base_url="https://omnigent.example.com",
            session_cookie_name="__Host-ap_session",
            provider_type=provider_type,
        ),
        _accounts_config=None,
        login_url="/auth/login",
    )


def test_router_factory_rejects_github_oauth(tmp_path: Path) -> None:
    """GitHub is an ``oidc`` source pointed at a NON-OIDC endpoint.

    ``OIDCConfig.from_env`` sets ``provider_type="github"`` and
    ``authorization_endpoint=https://github.com/login/oauth/authorize`` —
    plain OAuth 2.0, which has no ``prompt`` parameter. It would ignore
    ``prompt=login``, reuse its session, and hand back a callback whose fresh
    ``iat`` clears the consent gate. Refused outright: a grant issued behind a
    gate that cannot hold is worse than no grant, because it looks protected.
    """
    from omnigent.server.routes.device_auth import create_device_auth_router

    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    with pytest.raises(RuntimeError, match="not full OIDC"):
        create_device_auth_router(_oidc_provider("github"), store)  # type: ignore[arg-type]


def test_unsupported_reason_admits_standard_oidc_and_accounts() -> None:
    """The predicate must not over-refuse: real OIDC and accounts both pass."""
    from types import SimpleNamespace

    from omnigent.server.routes.device_auth import unsupported_reason

    assert unsupported_reason(_oidc_provider("oidc")) is None  # type: ignore[arg-type]
    assert unsupported_reason(SimpleNamespace(_source="accounts")) is None  # type: ignore[arg-type]
    assert unsupported_reason(_oidc_provider("github")) is not None  # type: ignore[arg-type]
    assert unsupported_reason(SimpleNamespace(_source="header")) is not None  # type: ignore[arg-type]


def test_router_factory_builds_in_oidc_mode_from_the_oidc_config(tmp_path: Path) -> None:
    """OIDC owns the same HS256 session cookie accounts does, so the grant
    works there — the IdP simply decides how the user proves themselves.

    The config must come from ``_oidc_config``: reading ``_accounts_config``
    (None under OIDC) would trip the assert, and reading the wrong secret would
    sign device codes and refresh tokens with a key nothing else validates.
    """
    from omnigent.server.routes.device_auth import create_device_auth_router

    provider = _oidc_provider("oidc")
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")

    router = create_device_auth_router(provider, store)  # type: ignore[arg-type]

    paths = {route.path for route in router.routes}  # type: ignore[attr-defined]
    assert "/oauth/device/authorize" in paths
    assert "/oauth/token" in paths
    assert "/oauth/revoke" in paths


# ── Store invariants (unit) ───────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> DeviceGrantStore:
    return DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")


def _new_grant(store: DeviceGrantStore, device_code: str = "dc", now: int = 1000):
    return store.create_grant(
        secrets.token_urlsafe(8),
        device_code_hash=hash_secret(device_code, _KEY),
        user_code="ABCD-2345",
        client_id="slack",
        created_at=now,
        expires_at=now + 600,
    )


def test_poll_pending_then_slow_down(store: DeviceGrantStore) -> None:
    """A poll faster than the interval yields slow_down; slower is pending."""
    _new_grant(store)
    dch = hash_secret("dc", _KEY)
    assert (
        store.poll_for_token(dch, now_epoch_seconds=1001, min_interval_seconds=5)[0] == "pending"
    )
    # Immediately again → too fast.
    assert (
        store.poll_for_token(dch, now_epoch_seconds=1002, min_interval_seconds=5)[0] == "slow_down"
    )


_LIFETIME = 30 * 24 * 3600


def test_approve_binds_identity(store: DeviceGrantStore) -> None:
    """Approval binds the Omnigent identity and stamps approved_at."""
    g = _new_grant(store)
    ok = store.approve(g.id, user_id="a@x", now_epoch_seconds=1010)
    assert ok is not None and ok.status == "approved" and ok.user_id == "a@x"
    assert ok.approved_at == 1010
    # Re-approving a non-pending grant is a no-op.
    assert store.approve(g.id, user_id="b@x", now_epoch_seconds=1011) is None


def test_approve_works_with_null_client_id(store: DeviceGrantStore) -> None:
    """A grant created without a client_id is still approvable.

    Regression: an earlier client-identity guard made NULL-context grants
    permanently unapprovable (SQL ``NULL = ''`` is UNKNOWN).
    """
    g = store.create_grant(
        "gnull",
        device_code_hash=hash_secret("dc2", _KEY),
        user_code="ZZZZ-9999",
        client_id=None,
        created_at=1000,
        expires_at=1600,
    )
    ok = store.approve(g.id, user_id="a@x", now_epoch_seconds=1010)
    assert ok is not None and ok.status == "approved"


def test_device_code_single_use(store: DeviceGrantStore) -> None:
    """A device_code can be redeemed for tokens at most once."""
    g = _new_grant(store)
    store.approve(g.id, user_id="a@x", now_epoch_seconds=1010)
    first = store.redeem_approved(
        g.id, refresh_token_hash=hash_secret("r1", _KEY), now_epoch_seconds=1011
    )
    assert first is not None and first.status == "redeemed"
    second = store.redeem_approved(
        g.id, refresh_token_hash=hash_secret("r2", _KEY), now_epoch_seconds=1012
    )
    assert second is None


def test_refresh_rotation_and_reuse_detection(store: DeviceGrantStore) -> None:
    """Rotation invalidates the old token; replaying it fails to match."""
    g = _new_grant(store)
    store.approve(g.id, user_id="a@x", now_epoch_seconds=1010)
    store.redeem_approved(g.id, refresh_token_hash=hash_secret("r1", _KEY), now_epoch_seconds=1011)
    rotated = store.rotate_refresh_token(
        g.id,
        expected_hash=hash_secret("r1", _KEY),
        new_hash=hash_secret("r2", _KEY),
        now_epoch_seconds=1012,
        max_lifetime_seconds=_LIFETIME,
    )
    assert rotated is not None
    # Replaying the old token no longer matches (reuse signal).
    assert (
        store.rotate_refresh_token(
            g.id,
            expected_hash=hash_secret("r1", _KEY),
            new_hash=hash_secret("r3", _KEY),
            now_epoch_seconds=1013,
            max_lifetime_seconds=_LIFETIME,
        )
        is None
    )
    # The superseded token is discoverable via the prev-hash lookup so the
    # route layer can recognise reuse and revoke; the current one is not.
    assert store.get_by_prev_refresh_hash(hash_secret("r1", _KEY)) is not None
    assert store.get_by_prev_refresh_hash(hash_secret("r2", _KEY)) is None


def test_refresh_refused_past_absolute_lifetime(store: DeviceGrantStore) -> None:
    """A grant older than the absolute lifetime can no longer rotate."""
    g = _new_grant(store)
    store.approve(g.id, user_id="a@x", now_epoch_seconds=1010)
    store.redeem_approved(g.id, refresh_token_hash=hash_secret("r1", _KEY), now_epoch_seconds=1011)
    # Well past approved_at + lifetime → no rotation, and NOT a reuse signal.
    too_late = 1010 + _LIFETIME + 1
    assert (
        store.rotate_refresh_token(
            g.id,
            expected_hash=hash_secret("r1", _KEY),
            new_hash=hash_secret("r2", _KEY),
            now_epoch_seconds=too_late,
            max_lifetime_seconds=_LIFETIME,
        )
        is None
    )
    # The grant is not revoked by aging out — it is simply un-refreshable.
    assert store.is_revoked(g.id) is False


def test_purge_reclaims_aged_redeemed_grants(store: DeviceGrantStore) -> None:
    """purge_expired removes redeemed grants past their absolute lifetime."""
    g = _new_grant(store)
    store.approve(g.id, user_id="a@x", now_epoch_seconds=1010)
    store.redeem_approved(g.id, refresh_token_hash=hash_secret("r1", _KEY), now_epoch_seconds=1011)
    # Before lifetime: kept.
    assert store.purge_expired(1012, max_lifetime_seconds=_LIFETIME) == 0
    assert store.get_by_id(g.id) is not None
    # After lifetime: reclaimed.
    deleted = store.purge_expired(1010 + _LIFETIME + 1, max_lifetime_seconds=_LIFETIME)
    assert deleted == 1
    assert store.get_by_id(g.id) is None


def test_revoke_is_fail_closed(store: DeviceGrantStore) -> None:
    """Unknown grants read as revoked; revoked grants clear their token."""
    assert store.is_revoked("nonexistent") is True
    g = _new_grant(store)
    store.approve(g.id, user_id="a@x", now_epoch_seconds=1010)
    store.redeem_approved(g.id, refresh_token_hash=hash_secret("r1", _KEY), now_epoch_seconds=1011)
    assert store.is_revoked(g.id) is False
    assert store.revoke(g.id) is True
    assert store.is_revoked(g.id) is True
    # A revoked grant's refresh token no longer resolves.
    assert store.get_by_refresh_hash(hash_secret("r1", _KEY)) is None


# ── Route flow (integration) ──────────────────────────────────────


def _build_accounts_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, device_grant_enabled: bool = True
) -> Iterator[TestClient]:
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", "admin-pw-12345")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-creds"))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_AUTO_OPEN", "0")
    # Device grant is opt-in / default-off; the route tests need it mounted.
    if device_grant_enabled:
        monkeypatch.setenv("OMNIGENT_DEVICE_GRANT_ENABLED", "1")
    else:
        monkeypatch.delenv("OMNIGENT_DEVICE_GRANT_ENABLED", raising=False)

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
    with TestClient(app) as client:
        yield client


def _build_oidc_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_type: str = "oidc",
    provider: object | None = None,
) -> Iterator[TestClient]:
    """Build a real app through ``create_app`` with an OIDC auth provider.

    The provider is constructed directly rather than through
    ``create_auth_provider`` so no IdP discovery request is made; everything
    downstream — including the device-grant mount decision — is the
    production path. ``provider`` overrides it outright, for modes that need
    no OIDC config at all.
    """
    monkeypatch.setenv("OMNIGENT_DEVICE_GRANT_ENABLED", "1")
    if provider is None:
        monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)

    db_url = f"sqlite:///{tmp_path}/test.db"
    from omnigent.db.utils import get_or_create_engine
    from omnigent.runtime import init as init_runtime
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.oidc import OIDCConfig
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

    config = OIDCConfig(
        issuer="https://accounts.google.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=bytes.fromhex("bb" * 32),
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type=provider_type,
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=False,
    )
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=comment_store,
        permission_store=permission_store,
        host_store=host_store,
        auth_provider=provider or UnifiedAuthProvider(source="oidc", oidc_config=config),
    )
    with TestClient(app) as client:
        yield client


def test_create_app_mounts_the_grant_under_oidc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mount decision must be exercised through ``create_app`` itself.

    Testing the factory directly proves the router builds; it does not prove
    the app ever calls it. A typo in the mount condition would silently leave
    ``/oauth/*`` absent under OIDC with every other test still green.
    """
    for client in _build_oidc_app(tmp_path, monkeypatch):
        res = client.post("/oauth/device/authorize", json={"client_id": "polly"})
        assert res.status_code == 200, f"/oauth/device/authorize returned {res.status_code}"
        assert res.json()["user_code"]


def test_create_app_refuses_the_grant_for_github_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub is an ``oidc`` source that cannot honour the re-auth gate.

    The app must boot without it rather than mount routes whose security
    property does not hold.
    """
    for client in _build_oidc_app(tmp_path, monkeypatch, provider_type="github"):
        # Asserted against the route table, not a status code: the SPA
        # catch-all answers unmounted paths, so "not 200" would also pass if
        # the routes were mounted and merely erroring.
        oauth_routes = [
            route.path
            for route in client.app.routes  # type: ignore[attr-defined]
            if getattr(route, "path", "").startswith("/oauth/")
        ]
        assert oauth_routes == [], f"the grant must not mount for GitHub OAuth: {oauth_routes}"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _build_accounts_app(tmp_path, monkeypatch)


def _login_admin(client: TestClient) -> None:
    r = client.post("/auth/login", json={"username": "admin", "password": "admin-pw-12345"})
    assert r.status_code == 200, r.text


def test_full_device_flow(app: TestClient) -> None:
    """authorize → consent (as admin) → approve → poll → get delegated token."""
    # 1. Client (no auth) starts the flow.
    r = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code == 200, r.text
    data = r.json()
    device_code = data["device_code"]
    user_code = data["user_code"]
    assert data["verification_uri"].endswith("/oauth/device")

    # 2. Poll before approval → authorization_pending.
    r = app.post(
        "/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "authorization_pending"

    # 3. User signs in and approves. (Origin header satisfies the CSRF gate.)
    _login_admin(app)
    r = app.post(
        "/oauth/device/approve",
        data={"user_code": user_code},
        headers={"Origin": "http://localhost:8000"},
    )
    assert r.status_code == 200 and "Connected" in r.text

    # 4. Poll after approval (past the interval) → tokens.
    import time as _t

    _t.sleep(0)  # interval is enforced on wall clock; first post-approve poll is the 2nd poll
    # Force past the slow-down window by waiting out the interval is slow;
    # instead assert either slow_down or success and retry once.
    r = app.post(
        "/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
    )
    if r.json().get("error") == "slow_down":
        _t.sleep(5)
        r = app.post(
            "/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
        )
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token_type"] == "Bearer"
    access_token = tok["access_token"]
    refresh_token = tok["refresh_token"]

    # 5. The delegated token reaches session APIs but NOT admin endpoints.
    #    Clear the browser session cookie first so the bearer token is the
    #    only credential — otherwise the admin login cookie would answer.
    app.cookies.clear()
    auth = {"Authorization": f"Bearer {access_token}"}
    r = app.get("/v1/agents", headers=auth)
    assert r.status_code == 200, r.text
    r = app.get("/auth/users", headers=auth)
    assert r.status_code in (401, 403), r.text  # scope blocks admin surface

    # 6. Refresh rotates the token.
    r = app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
    assert r.status_code == 200, r.text
    new_refresh = r.json()["refresh_token"]
    assert new_refresh != refresh_token
    # Old refresh token is now dead (rotation) → revokes on reuse.
    r = app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

    # 7. After reuse-triggered revocation, the new refresh token is dead too.
    r = app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": new_refresh}
    )
    assert r.status_code == 400


def test_consent_page_requires_login(app: TestClient) -> None:
    """The consent page bounces an unauthenticated visitor to login."""
    r = app.get("/oauth/device?user_code=ABCD-2345", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login?"), r.headers["location"]


def test_consent_forces_reauth_even_with_no_session_at_all(app: TestClient) -> None:
    """The bounce for a caller with NO session must force re-authentication too.

    Under accounts, "no session" means no credential and the SPA shows the
    password form either way. Under OIDC it does not: the caller may still
    hold a live IdP session, which would satisfy an unforced bounce silently
    and return a fresh ``iat`` that clears the consent gate. The consent page
    cannot tell the two apart, so every bounce is forced.
    """
    r = app.get("/oauth/device?user_code=ABCD-2345", follow_redirects=False)
    assert "reauth=1" in r.headers["location"]


def test_consent_forces_reauth_for_stale_session(app: TestClient) -> None:
    """A session that predates the grant is bounced to a FORCED re-login.

    The whole anti-phishing property: even an already-signed-in user must
    re-authenticate for THIS flow. Logging in BEFORE authorize makes the
    session ``iat`` older than the grant's ``created_at``, so the consent GET
    must bounce with ``reauth=1`` rather than render the approve screen — and
    the approve POST must refuse the stale session outright.
    """
    # Sign in FIRST (stale session relative to the soon-to-be-created grant).
    _login_admin(app)
    import time as _t

    _t.sleep(1)  # ensure the grant's created_at is a later second than iat

    r = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code == 200, r.text
    user_code = r.json()["user_code"]

    # Consent GET bounces to a FORCED re-login (reauth=1), not the approve page.
    r = app.get(f"/oauth/device?user_code={user_code}", follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith("/login?"), loc
    assert "reauth=1" in loc

    # Approve POST refuses the stale session too (defense in depth — a direct
    # POST must not bypass the GET's gate).
    r = app.post(
        "/oauth/device/approve",
        data={"user_code": user_code},
        headers={"Origin": "http://localhost:8000"},
    )
    assert r.status_code == 200
    assert "too old" in r.text.lower()


def test_unsupported_grant_type(app: TestClient) -> None:
    r = app.post("/oauth/token", data={"grant_type": "password"})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"


def test_approve_rejects_missing_origin(app: TestClient) -> None:
    """Approve is browser-only, so a request with no Origin is refused (CSRF)."""
    r = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    user_code = r.json()["user_code"]
    _login_admin(app)
    # No Origin header → 403, independent of the session cookie's SameSite.
    r = app.post("/oauth/device/approve", data={"user_code": user_code})
    assert r.status_code == 403


def test_authorize_is_rate_limited(app: TestClient) -> None:
    """The public authorize endpoint throttles a flood from one client."""
    last = None
    for _ in range(15):
        last = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    # The default cap is 10/60s; the 11th+ within the window is throttled.
    assert last is not None and last.status_code == 429
    assert last.json()["error"] == "slow_down"


_SECRET = "s3cr3t-device-client"


@pytest.fixture
def disabled_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An accounts-mode app with the device grant left at its default (off)."""
    yield from _build_accounts_app(tmp_path, monkeypatch, device_grant_enabled=False)


def test_device_grant_routes_absent_by_default(disabled_app: TestClient) -> None:
    """Default-off: the /oauth/* router is not mounted unless explicitly
    enabled, so the device-grant POST handlers don't run.

    With no mounted handler the POST is not routed: it 404s when nothing else
    claims the path, or 405s when a built web SPA is mounted at ``/`` (its
    catch-all serves GET only). Either way NO device-grant logic executes —
    no device_code is issued and no OAuth error shape is returned.
    """
    r = disabled_app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code in (404, 405)
    assert "device_code" not in r.text
    r = disabled_app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x"}
    )
    assert r.status_code in (404, 405)
    r = disabled_app.post("/oauth/revoke", data={"refresh_token": "x"})
    assert r.status_code in (404, 405)


def test_account_auth_available_when_device_grant_disabled(disabled_app: TestClient) -> None:
    """Disabling the device grant must NOT take down account/OIDC ``/auth``
    routes — only the ``/oauth/*`` device surface is gated.

    The flag gates a separate router mount; the accounts ``/auth`` router is
    wired independently, so login/logout/user management keep working.
    """
    # The accounts auth router is mounted: /auth/login authenticates the admin
    # (the device grant being off doesn't affect it).
    r = disabled_app.post("/auth/login", json={"username": "admin", "password": "admin-pw-12345"})
    assert r.status_code == 200, r.text
    # And an authenticated account-management route works.
    r = disabled_app.get("/auth/users")
    assert r.status_code == 200, r.text
    # Sanity: the device-grant surface is still absent (no /oauth handler) —
    # 404 with no SPA catch-all, 405 when a built SPA is mounted at "/".
    r = disabled_app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code in (404, 405)


@pytest.fixture
def secret_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An accounts-mode app with OMNIGENT_DEVICE_CLIENT_SECRET enforced."""
    monkeypatch.setenv("OMNIGENT_DEVICE_CLIENT_SECRET", _SECRET)
    yield from _build_accounts_app(tmp_path, monkeypatch)


def test_client_secret_required_when_configured(secret_app: TestClient) -> None:
    """With the secret set, the client-facing endpoints reject a missing/wrong
    header and accept the matching one."""
    hdr = {"X-Omnigent-Client-Secret": _SECRET}

    # authorize: no header → 401 invalid_client; wrong → 401; correct → 200.
    r = secret_app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = secret_app.post(
        "/oauth/device/authorize",
        json={"client_id": "slack"},
        headers={"X-Omnigent-Client-Secret": "wrong"},
    )
    assert r.status_code == 401
    r = secret_app.post("/oauth/device/authorize", json={"client_id": "slack"}, headers=hdr)
    assert r.status_code == 200, r.text

    # token + revoke are gated the same way (checked before any body parsing).
    r = secret_app.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = secret_app.post("/oauth/revoke", data={"refresh_token": "x"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    # With the header, token reaches normal error handling (bad token, not 401).
    r = secret_app.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "x"},
        headers=hdr,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_browser_consent_not_gated_by_client_secret(secret_app: TestClient) -> None:
    """The browser consent GET must NOT require the client secret — the user's
    browser never holds it."""
    r = secret_app.get("/oauth/device?user_code=ABCD-2345", follow_redirects=False)
    # Bounces to login (unauthenticated), NOT a 401 invalid_client.
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login?"), r.headers["location"]


def test_no_secret_configured_stays_public(app: TestClient) -> None:
    """Without the env var, authorize stays open (backward compatible)."""
    r = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code == 200, r.text


def _capture_app_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builder: Callable[[], Iterator[TestClient]] | None = None,
) -> list[str]:
    """Build an app and return WARNING messages from ``server.app``.

    Attaches a handler directly to the ``omnigent.server.app`` logger rather
    than using ``caplog``: the server's telemetry/logging init runs during the
    build and reconfigures logging, which can detach pytest's capture handler.
    A directly-attached handler is immune to that.
    """
    captured: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                captured.append(record.getMessage())

    logger = logging.getLogger("omnigent.server.app")
    handler = _Collector()
    logger.addHandler(handler)
    try:
        for _ in builder() if builder else _build_accounts_app(tmp_path, monkeypatch):
            break  # build (and immediately tear down) so the mount runs
    finally:
        logger.removeHandler(handler)
    return captured


def test_startup_warns_when_client_secret_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mounting the device grant on a multi-user server without the client
    secret must log a loud warning — the authorize endpoint is public, so the
    operator should know to opt into OMNIGENT_DEVICE_CLIENT_SECRET."""
    monkeypatch.delenv("OMNIGENT_DEVICE_CLIENT_SECRET", raising=False)
    warnings = _capture_app_warnings(tmp_path, monkeypatch)
    assert any(
        "OMNIGENT_DEVICE_CLIENT_SECRET is not set" in m and "PUBLIC" in m for m in warnings
    ), warnings


def test_startup_silent_when_client_secret_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the secret configured, the public-endpoint warning must NOT fire."""
    monkeypatch.setenv("OMNIGENT_DEVICE_CLIENT_SECRET", _SECRET)
    warnings = _capture_app_warnings(tmp_path, monkeypatch)
    assert not any("OMNIGENT_DEVICE_CLIENT_SECRET is not set" in m for m in warnings)


def _build_header_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A header/proxy-mode app with the device grant requested."""
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "1")
    from omnigent.server.auth import UnifiedAuthProvider

    yield from _build_oidc_app(
        tmp_path, monkeypatch, provider=UnifiedAuthProvider(source="header")
    )


def test_refusal_is_explained_in_header_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Header mode must still learn WHY /oauth/* is missing.

    It is one of the two cases ``unsupported_reason`` exists to explain, and
    also the mode where the auth router itself never mounts (``login_url`` is
    None). Deciding device-grant support inside that mount block meant the
    operator most likely to be confused saw only silence.
    """
    warnings = _capture_app_warnings(
        tmp_path, monkeypatch, builder=lambda: _build_header_app(tmp_path, monkeypatch)
    )

    assert any(
        "OMNIGENT_DEVICE_GRANT_ENABLED is set" in message and "no server-minted session" in message
        for message in warnings
    ), warnings


def test_refusal_is_explained_for_github_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same for a GitHub-backed OIDC deployment."""
    warnings = _capture_app_warnings(
        tmp_path,
        monkeypatch,
        builder=lambda: _build_oidc_app(tmp_path, monkeypatch, provider_type="github"),
    )

    assert any(
        "OMNIGENT_DEVICE_GRANT_ENABLED is set" in message and "not full OIDC" in message
        for message in warnings
    ), warnings


def test_a_supported_deployment_logs_no_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The predicate must not over-refuse: real OIDC mounts silently."""
    warnings = _capture_app_warnings(
        tmp_path, monkeypatch, builder=lambda: _build_oidc_app(tmp_path, monkeypatch)
    )

    assert not any("routes are NOT mounted" in message for message in warnings), warnings


def test_unsupported_reason_refuses_an_unaudited_provider_type() -> None:
    """Allowlisted, not denylisted.

    ``from_env`` yields only ``github`` or ``oidc`` today, so denying the one
    known-bad value happened to be equivalent. The next OAuth dialect modelled
    under this source — or a directly-constructed config — would have been
    admitted by default, behind a gate that cannot hold for it.
    """

    from omnigent.server.routes.device_auth import unsupported_reason

    for provider_type in ("github", "gitlab", "oauth2", ""):
        reason = unsupported_reason(_oidc_provider(provider_type))  # type: ignore[arg-type]
        assert reason is not None, provider_type
        assert repr(provider_type) in reason, "the reason must name the value it refused"

    assert unsupported_reason(_oidc_provider("oidc")) is None  # type: ignore[arg-type]


def test_unsupported_reason_refuses_oidc_with_no_config() -> None:
    """A missing config is not a supported deployment."""
    from types import SimpleNamespace

    from omnigent.server.routes.device_auth import unsupported_reason

    provider = SimpleNamespace(_source="oidc", _oidc_config=None, _accounts_config=None)
    assert unsupported_reason(provider) is not None  # type: ignore[arg-type]


def test_bounce_percent_encodes_the_return_to(app: TestClient) -> None:
    """The consent URL must survive the bounce whatever the code contains.

    ``html.escape`` is an HTML escaper, not a URL one: it leaves ``?``,
    ``=``, ``#`` and ``+`` untouched. A ``#`` therefore ended the URL and
    turned everything after it into a fragment, silently truncating the
    ``return_to`` the user is meant to come back to. Percent-encoding is the
    correct tool for a query value.

    The ``&reauth=1`` that follows survived only because ``html.escape``
    happens to turn ``&`` into ``&amp;`` — an accident of the escaper, not a
    property the forced-re-auth invariant should rest on.
    """
    from urllib.parse import parse_qs, urlparse

    r = app.get("/oauth/device?user_code=AB%23CD%26reauth=0", follow_redirects=False)

    assert r.status_code == 302
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["return_to"] == ["/oauth/device?user_code=AB#CD&reauth=0"], (
        "the consent URL must round-trip intact"
    )
    assert query["reauth"] == ["1"], "and the forced re-auth must be the only one"
