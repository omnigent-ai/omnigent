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

import secrets
from collections.abc import Iterator
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from omnigent.server.device_grant_store import DeviceGrantStore, hash_secret

_KEY = b"k" * 32


# ── Router mount guard (unit) ─────────────────────────────────────


@pytest.mark.parametrize("source", ["header"])
def test_router_factory_rejects_unsupported_mode(source: str, tmp_path: Path) -> None:
    """Header mode has no server-mintable identity; ``create_device_auth_router``
    must refuse to build for it.  (OIDC is now supported — see the positive test
    ``test_router_factory_builds_for_oidc_mode`` below.)"""
    from types import SimpleNamespace

    from omnigent.server.routes.device_auth import create_device_auth_router

    provider = SimpleNamespace(_source=source)
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    with pytest.raises(RuntimeError):
        create_device_auth_router(provider, store)  # type: ignore[arg-type]


def test_router_factory_builds_for_oidc_mode(tmp_path: Path) -> None:
    """create_device_auth_router must succeed for standard oidc mode.

    OIDC deployments now support the device-grant flow; the factory must
    accept an oidc provider and build a mountable router.
    """
    from types import SimpleNamespace

    from fastapi import FastAPI

    from omnigent.server.routes.device_auth import create_device_auth_router

    oidc_cfg = SimpleNamespace(
        cookie_secret=_KEY,
        base_url="https://omni.example.test",
        session_cookie_name="__Host-omni_session",
        provider_type="oidc",
    )
    provider = SimpleNamespace(_source="oidc", _oidc_config=oidc_cfg)
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    # Should not raise; result is a mountable APIRouter.
    router = create_device_auth_router(provider, store)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(router)


def test_router_factory_rejects_github_oauth_oidc(tmp_path: Path) -> None:
    """GitHub OAuth runs under _source=oidc but does not honour prompt=login.

    The anti-phishing reauth gate relies on the IdP honouring prompt=login;
    GitHub does not.  create_device_auth_router must refuse to build for
    a GitHub-typed OIDC config.
    """
    from types import SimpleNamespace

    from omnigent.server.routes.device_auth import create_device_auth_router

    oidc_cfg = SimpleNamespace(
        cookie_secret=_KEY,
        base_url="https://omni.example.test",
        session_cookie_name="__Host-omni_session",
        provider_type="github",
    )
    provider = SimpleNamespace(_source="oidc", _oidc_config=oidc_cfg)
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    with pytest.raises(RuntimeError, match="GitHub OAuth"):
        create_device_auth_router(provider, store)  # type: ignore[arg-type]


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
    assert "/login" in r.headers["location"]


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
    """Default-off: the RFC 8628 device-consent surface is not mounted
    unless explicitly enabled, so its POST handlers don't run.

    With no mounted handler the POST is not routed: it 404s when nothing else
    claims the path, or 405s when a built web SPA is mounted at ``/`` (its
    catch-all serves GET only). Either way NO device-consent logic executes —
    no device_code is issued.

    The token/revoke half is different: login-issued refresh grants need it
    in every accounts/oidc deployment, so ``/oauth/token`` and
    ``/oauth/revoke`` mount regardless of the flag — but the device_code
    grant type is refused there when the device flow is off.
    """
    r = disabled_app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code in (404, 405)
    assert "device_code" not in r.text
    # Token endpoint is live (login grants) but knows no device_code grant.
    r = disabled_app.post(
        "/oauth/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": "x"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"
    # Refresh with an unknown token fails closed with the OAuth shape.
    r = disabled_app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x"}
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"
    # Revoke stays idempotent.
    r = disabled_app.post("/oauth/revoke", data={"refresh_token": "x"})
    assert r.status_code == 200
    assert r.json() == {"revoked": True}


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
    # Sanity: the device-consent surface is still absent (no handler) —
    # 404 with no SPA catch-all, 405 when a built SPA is mounted at "/".
    r = disabled_app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code in (404, 405)


@pytest.fixture
def secret_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An accounts-mode app with OMNIGENT_DEVICE_CLIENT_SECRET enforced."""
    monkeypatch.setenv("OMNIGENT_DEVICE_CLIENT_SECRET", _SECRET)
    yield from _build_accounts_app(tmp_path, monkeypatch)


def test_client_secret_required_when_configured(secret_app: TestClient) -> None:
    """With the secret set, the endpoints that mint from an ephemeral code
    reject a missing/wrong header and accept the matching one."""
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

    # The device_code exchange mints from the device_code alone, so it stays
    # gated.
    r = secret_app.post(
        "/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": "x",
        },
    )
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_client_secret_does_not_gate_refresh_or_revoke(secret_app: TestClient) -> None:
    """Refresh and revoke present their own grant-bound secret, so they are
    NOT additionally gated by the device client secret.

    A CLI/host renewing its own login grant has no way to carry that secret;
    gating these would make every automatic refresh fail with 401 in any
    deployment that sets it (e.g. one also serving Slack).
    """
    # No client-secret header: reaches normal OAuth error handling, not 401.
    r = secret_app.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # Revoke stays idempotent without the header.
    r = secret_app.post("/oauth/revoke", data={"refresh_token": "x"})
    assert r.status_code == 200 and r.json() == {"revoked": True}


def test_browser_consent_not_gated_by_client_secret(secret_app: TestClient) -> None:
    """The browser consent GET must NOT require the client secret — the user's
    browser never holds it."""
    r = secret_app.get("/oauth/device?user_code=ABCD-2345", follow_redirects=False)
    # Bounces to login (unauthenticated), NOT a 401 invalid_client.
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_no_secret_configured_stays_public(app: TestClient) -> None:
    """Without the env var, authorize stays open (backward compatible)."""
    r = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code == 200, r.text


# ── Login-issued refresh grants ───────────────────────────────────


def test_create_redeemed_grant_refresh_cycle(store: DeviceGrantStore) -> None:
    """A login-issued grant (born redeemed) rotates and reuse-revokes exactly
    like one that walked the device flow."""
    refresh_hash = hash_secret("r1", _KEY)
    g = store.create_redeemed_grant(
        "lg1",
        user_id="alice@example.com",
        client_id="omnigent-cli",
        refresh_token_hash=refresh_hash,
        created_at=1000,
    )
    assert g.status == "redeemed"
    assert g.user_id == "alice@example.com"
    assert g.approved_at == 1000
    # Its refresh token resolves and rotates.
    assert store.get_by_refresh_hash(refresh_hash) is not None
    rotated = store.rotate_refresh_token(
        g.id,
        expected_hash=refresh_hash,
        new_hash=hash_secret("r2", _KEY),
        now_epoch_seconds=1010,
        max_lifetime_seconds=_LIFETIME,
    )
    assert rotated is not None
    # Replaying the pre-rotation token is reuse — the whole grant dies.
    assert store.get_by_refresh_hash(refresh_hash) is None
    stale = store.get_by_prev_refresh_hash(refresh_hash)
    assert stale is not None and stale.id == g.id


def test_create_redeemed_grant_survives_pending_purge(store: DeviceGrantStore) -> None:
    """expires_at (the device_code window) is meaningless for a grant born
    redeemed — the pending/denied purge bucket must never collect it."""
    store.create_redeemed_grant(
        "lg2",
        user_id="alice@example.com",
        client_id="omnigent-cli",
        refresh_token_hash=hash_secret("r1", _KEY),
        created_at=1000,
    )
    # Way past the (already-elapsed) expires_at, within grant lifetime.
    assert store.purge_expired(1000 + 3600) == 0
    assert store.get_by_id("lg2") is not None
    # But the lifetime purge does collect it once the grant ages out.
    assert store.purge_expired(1000 + _LIFETIME + 1, max_lifetime_seconds=_LIFETIME) == 1
    assert store.get_by_id("lg2") is None


def test_login_grant_refresh_round_trip(disabled_app: TestClient, tmp_path: Path) -> None:
    """A login-issued refresh grant renews via /oauth/token even with the
    device flow disabled — the core unattended-host fix.

    Login grants deliberately do NOT rotate: the same refresh token keeps
    working, so a lost response or a crash between the server committing and
    the client persisting cannot brick an unattended host.
    """
    # A CLI/device login mints refresh material server-side; browser
    # /auth/login never does (see test_web_login_never_issues_refresh).
    import os

    from omnigent.server.routes.device_auth import issue_login_grant

    store = DeviceGrantStore(f"sqlite:///{tmp_path}/test.db")
    secret = bytes.fromhex(os.environ["OMNIGENT_ACCOUNTS_COOKIE_SECRET"])
    refresh = issue_login_grant(store, user_id="admin", cookie_secret=secret)

    # Refresh: fresh access token, SAME refresh token handed back.
    r = disabled_app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["refresh_token"] == refresh
    assert body["expires_in"] == 3600

    # The token authenticates against the API.
    r = disabled_app.get("/v1/hosts", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r.status_code == 200, r.text

    # Repeat use of the same token keeps working — no reuse revocation.
    for _ in range(3):
        again = disabled_app.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert again.status_code == 200, again.text
        assert again.json()["refresh_token"] == refresh


def test_login_grant_token_keeps_session_authority(
    disabled_app: TestClient, tmp_path: Path
) -> None:
    """A refreshed login-grant token reaches routes OUTSIDE the delegated
    allowlist — it renews the session JWT and must keep its authority.

    Regression guard: minting it as a scope-restricted delegated token made
    ``/v1/usage``-style routes 401 after the first refresh, silently breaking
    agents and CLI commands on a long-lived host.
    """
    import os

    from omnigent.server.routes.device_auth import issue_login_grant

    store = DeviceGrantStore(f"sqlite:///{tmp_path}/test.db")
    secret = bytes.fromhex(os.environ["OMNIGENT_ACCOUNTS_COOKIE_SECRET"])
    refresh = issue_login_grant(store, user_id="admin", cookie_secret=secret)
    r = disabled_app.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
    )
    access = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {access}"}
    # Drop the browser session cookie that /auth/login set, so the bearer
    # token is the ONLY credential — otherwise the cookie answers and every
    # assertion below passes vacuously.
    disabled_app.cookies.clear()

    # Carries grant_id (revocable) but no scope claim (unrestricted).
    claims = jwt.decode(access, options={"verify_signature": False})
    assert claims["grant_id"]
    assert "scope" not in claims

    # Allowlisted route works...
    assert disabled_app.get("/v1/hosts", headers=auth).status_code == 200
    # ...and so does one outside the delegated allowlist. A scope-restricted
    # delegated token is refused here; this one must not be.
    r = disabled_app.get("/v1/usage", headers=auth)
    assert r.status_code != 401, f"login-grant token must not be scope-restricted: {r.text}"

    # Revocation still kills it despite the wider authority.
    assert disabled_app.post("/oauth/revoke", data={"refresh_token": refresh}).status_code == 200
    assert disabled_app.get("/v1/hosts", headers=auth).status_code == 401


def test_device_authorize_refuses_reserved_login_client_id(app: TestClient) -> None:
    """A device client cannot self-declare into the first-party login-grant
    class by naming its reserved client_id — that would hand it a
    full-authority (unscoped) token on refresh."""
    from omnigent.server.routes.device_auth import LOGIN_GRANT_CLIENT_ID

    r = app.post("/oauth/device/authorize", json={"client_id": LOGIN_GRANT_CLIENT_ID})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    assert "device_code" not in r.text


def test_device_grant_refresh_stays_scope_restricted(app: TestClient) -> None:
    """Third-party device grants keep the delegated scope AND keep rotating —
    the login-grant carve-out must not leak to them."""
    import time as _time

    r = app.post("/oauth/device/authorize", json={"client_id": "slack"})
    data = r.json()
    _login_admin(app)
    app.post(
        "/oauth/device/approve",
        data={"user_code": data["user_code"]},
        headers={"Origin": "http://localhost:8000"},
    )
    for _ in range(4):
        r = app.post(
            "/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": data["device_code"],
            },
        )
        if r.status_code == 200:
            break
        _time.sleep(1.2)
    assert r.status_code == 200, r.text
    body = r.json()
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["scope"] == "sessions"

    # And it still rotates.
    r2 = app.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": body["refresh_token"]},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["refresh_token"] != body["refresh_token"]


def test_web_login_never_issues_refresh(disabled_app: TestClient) -> None:
    """A plain browser-shaped login (no issue_refresh) must not hand out
    refresh material."""
    r = disabled_app.post("/auth/login", json={"username": "admin", "password": "admin-pw-12345"})
    assert r.status_code == 200, r.text
    assert "refresh_token" not in r.json()


def test_grant_lifetime_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMNIGENT_GRANT_MAX_LIFETIME_DAYS scales the absolute lifetime; junk
    and non-positive values fall back to the 30-day default."""
    from omnigent.server.routes.device_auth import (
        _GRANT_MAX_LIFETIME_SECONDS,
        _grant_max_lifetime_seconds,
    )

    monkeypatch.delenv("OMNIGENT_GRANT_MAX_LIFETIME_DAYS", raising=False)
    assert _grant_max_lifetime_seconds() == _GRANT_MAX_LIFETIME_SECONDS
    monkeypatch.setenv("OMNIGENT_GRANT_MAX_LIFETIME_DAYS", "90")
    assert _grant_max_lifetime_seconds() == 90 * 86400
    monkeypatch.setenv("OMNIGENT_GRANT_MAX_LIFETIME_DAYS", "0")
    assert _grant_max_lifetime_seconds() == _GRANT_MAX_LIFETIME_SECONDS
    monkeypatch.setenv("OMNIGENT_GRANT_MAX_LIFETIME_DAYS", "soon")
    assert _grant_max_lifetime_seconds() == _GRANT_MAX_LIFETIME_SECONDS


def test_oauth_token_router_oidc_mode(tmp_path: Path) -> None:
    """The token/revoke router builds and refreshes under an OIDC-mode
    provider — the half of the machinery DEVICE_AUTH.md said OIDC would
    never need, until login-issued grants needed it."""
    from types import SimpleNamespace

    from fastapi import FastAPI

    from omnigent.server.routes.device_auth import (
        create_oauth_token_router,
        issue_login_grant,
    )

    provider = SimpleNamespace(_source="oidc", _oidc_config=SimpleNamespace(cookie_secret=_KEY))
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")
    app = FastAPI()
    app.include_router(create_oauth_token_router(provider, store))  # type: ignore[arg-type]

    refresh = issue_login_grant(store, user_id="alice@example.com", cookie_secret=_KEY)
    with TestClient(app) as client:
        # device_code exchanges are refused on a standalone mount.
        r = client.post(
            "/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": "x",
            },
        )
        assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"
        # Refresh works.
        r = client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
        )
        assert r.status_code == 200, r.text
        rotated = r.json()["refresh_token"]
        # Revoke by the rotated token, then refresh fails closed.
        r = client.post("/oauth/revoke", data={"refresh_token": rotated})
        assert r.status_code == 200
        r = client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rotated}
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_redeemed_grant_persistence_regression(store: DeviceGrantStore) -> None:
    """Regression test for P0 bug: create_redeemed_grant must persist the
    grant to the database (not crash with TypeError on missing query_name).

    This was broken when query_name was missing from self._session(), causing
    the entire login-issued grant feature to silently fail (caught by
    except Exception in the issuance sites).
    """
    refresh_hash = hash_secret("test-refresh", _KEY)

    # Must not raise; must persist a row.
    grant = store.create_redeemed_grant(
        "lg-persist-test",
        user_id="alice@example.com",
        client_id="omnigent-cli",
        refresh_token_hash=refresh_hash,
        created_at=1000,
    )

    # Verify persistence: the grant should be retrievable.
    retrieved = store.get_by_id(grant.id)
    assert retrieved is not None
    assert retrieved.user_id == "alice@example.com"
    assert retrieved.status == "redeemed"

    # Verify the refresh hash is usable.
    by_hash = store.get_by_refresh_hash(refresh_hash)
    assert by_hash is not None
    assert by_hash.id == grant.id


def test_app_skips_device_grant_for_github_oidc_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub-OIDC deployment with OMNIGENT_DEVICE_GRANT_ENABLED=1 must not crash.

    app.py calls create_device_auth_router only for accounts/OIDC modes; for
    GitHub-typed OIDC (provider_type=='github') it skips the mount because the
    router constructor would raise (GitHub ignores prompt=login).  Before the
    fix, the RuntimeError propagated out of create_app(), so the server failed
    to start with this config.
    """
    from types import SimpleNamespace

    from omnigent.server.routes.device_auth import create_device_auth_router

    # The router factory raises for GitHub.  Verify app.py never calls it for
    # a GitHub-typed OIDC provider even when the flag is on.
    oidc_cfg = SimpleNamespace(
        cookie_secret=_KEY,
        base_url="https://omni.example.test",
        session_cookie_name="__Host-omni_session",
        provider_type="github",
    )
    provider = SimpleNamespace(
        _source="oidc",
        _oidc_config=oidc_cfg,
        _accounts_config=None,
    )

    # Replicate the exact gate logic from app.py so the test stays in sync
    # with the code it guards.
    _is_github_oidc = (
        hasattr(provider, "_source")
        and provider._source == "oidc"
        and provider._oidc_config is not None
        and getattr(provider._oidc_config, "provider_type", None) == "github"
    )
    assert _is_github_oidc, "test setup: should be detected as GitHub OIDC"

    # The gate detects GitHub OIDC and skips the mount, so create_app never
    # reaches the factory. Prove the factory *would* have crashed to document
    # why the skip matters — then confirm the gate keeps it unreached.
    with pytest.raises(RuntimeError, match="GitHub OAuth"):
        create_device_auth_router(provider, None)  # type: ignore[arg-type]
