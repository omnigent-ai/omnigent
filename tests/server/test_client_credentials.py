"""Unit tests for the OAuth 2.0 client-credentials (machine-auth) grant.

Three layers, all network-free:

1. :class:`MachineClientConfig` env parsing — enabled, cleanly off, and the
   misconfigurations (partial, malformed secret hash, reserved principal, bad
   or over-ceiling TTL) that raise.
2. The handler factory and the folded ``POST /oauth/token`` route on a minimal
   OIDC-mode app — default-off (no handler at all when unconfigured or when the
   principal is refused), the token shape, the RFC 6749 §5.1 / §5.2 response
   headers, the per-source throttle in front of the pre-authentication client
   check, coexistence with the device/refresh grants on the one route, and
   every error shape.
3. The ``UnifiedAuthProvider._check_cookie`` claim gate — which of the three
   token shapes the server mints is confined to the delegated path allowlist,
   which keeps full authority, and which consults the revocation denylist.
   :func:`test_token_authority_table` pins all three together.
"""

from __future__ import annotations

import base64
import time
from unittest.mock import MagicMock
from urllib.parse import quote_plus

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from omnigent.entities import Account
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import hash_secret
from omnigent.server.oidc import OIDCConfig, mint_session_token
from omnigent.server.routes.client_credentials import (
    _SECRET_HASH_RE,
    _TOKEN_RATE_MAX,
    MachineClientConfig,
    _client_matches,
    _presented_client,
    create_client_credentials_handler,
)
from omnigent.server.routes.device_auth import (
    LOGIN_GRANT_CLIENT_ID,
    create_device_auth_router,
    create_oauth_token_router,
    mint_delegated_token,
)

_COOKIE_SECRET = b"c" * 32
_CLIENT_ID = "svc-omnigent"
_CLIENT_SECRET = "top-secret-machine-key"
_SECRET_HASH = hash_secret(_CLIENT_SECRET, _COOKIE_SECRET)
_MACHINE_SUB = "machine@example.com"


class _FakeStore:
    """Duck-typed permission store: only what the grant actually calls.

    The grant touches the store through ``is_admin`` — at mount and again
    before every mint — and through ``list_users`` once at mount, for the
    dedicated-identity warning. A minimal stand-in avoids implementing the
    whole ABC.
    """

    def __init__(self, *, admins: tuple[str, ...] = (), users: tuple[str, ...] = ()) -> None:
        self.admins = set(admins)
        self.users = set(users)

    def is_admin(self, user_id: str) -> bool:
        return user_id in self.admins

    def list_users(self, *, limit: int = 1000) -> list[Account]:
        return [
            Account(
                id=user_id,
                is_admin=user_id in self.admins,
                created_at=None,
                last_login_at=None,
                has_password=False,
            )
            for user_id in sorted(self.users)[:limit]
        ]


class _FakeGrantStore:
    """Duck-typed device-grant store: enough for the non-machine grant types.

    Only the refresh path is exercised here, and only to prove the machine
    branch did not shadow it — so every lookup misses and the housekeeping
    purge is a no-op.
    """

    def get_by_refresh_hash(self, refresh_hash: str) -> None:
        return None

    def get_by_prev_refresh_hash(self, refresh_hash: str) -> None:
        return None

    def purge_expired(self, now_epoch_seconds: int, *, max_lifetime_seconds: int) -> int:
        return 0


# ── Fixtures / helpers ────────────────────────────────────────────


def _make_oidc_provider(*, provider_type: str = "github") -> UnifiedAuthProvider:
    """An OIDC provider (no discovery fetch, no network).

    Defaults to GitHub-flavoured. The device-grant router refuses that
    provider type, so the test that mounts the full device flow asks for a
    standard OIDC one instead.
    """
    config = OIDCConfig(
        issuer="https://github.com",
        client_id="oidc-client",
        client_secret="oidc-secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_COOKIE_SECRET,
        scopes="read:user user:email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type=provider_type,
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        jwks_uri=None,
        userinfo_endpoint="https://api.github.com/user",
        allow_invites=False,
    )
    return UnifiedAuthProvider(source="oidc", oidc_config=config)


def _configure(monkeypatch: pytest.MonkeyPatch, *, ttl: str | None = None) -> None:
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_SECRET_HASH", _SECRET_HASH)
    monkeypatch.setenv("OMNIGENT_MACHINE_SUB", _MACHINE_SUB)
    if ttl is not None:
        monkeypatch.setenv("OMNIGENT_MACHINE_TOKEN_TTL", ttl)
    else:
        monkeypatch.delenv("OMNIGENT_MACHINE_TOKEN_TTL", raising=False)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OMNIGENT_MACHINE_CLIENT_ID",
        "OMNIGENT_MACHINE_CLIENT_SECRET_HASH",
        "OMNIGENT_MACHINE_SUB",
        "OMNIGENT_MACHINE_TOKEN_TTL",
    ):
        monkeypatch.delenv(var, raising=False)


def _handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    configure: bool = True,
    ttl: str | None = None,
    store: _FakeStore | None = None,
):
    """Build the grant handler (or ``None``) the way ``app.py`` does.

    Env must be set BEFORE the handler is created — the machine-client config
    is read once at mount, matching how the other auth env vars behave.
    """
    _clear(monkeypatch)
    if configure:
        _configure(monkeypatch, ttl=ttl)
    return create_client_credentials_handler(
        _make_oidc_provider(),
        _FakeStore() if store is None else store,  # type: ignore[arg-type]
    )


def _token_app(handler) -> FastAPI:
    """Build the real token router carrying *handler* as its machine branch."""
    app = FastAPI()
    app.include_router(
        create_oauth_token_router(
            _make_oidc_provider(),
            _FakeGrantStore(),  # type: ignore[arg-type]
            handle_client_credentials=handler,
        )
    )
    return app


def _app_with(handler) -> TestClient:
    """A TestClient over the real, folded ``POST /oauth/token``."""
    return TestClient(_token_app(handler))


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ttl: str | None = None,
    store: _FakeStore | None = None,
) -> TestClient:
    """Build a minimal app whose /oauth/token carries the machine grant.

    The store defaults to one where the machine sub is a non-admin identity,
    so the grant is enabled.
    """
    handler = _handler(monkeypatch, ttl=ttl, store=store)
    assert handler is not None, "expected the grant to be enabled for this config"
    return _app_with(handler)


# ── MachineClientConfig.from_env ──────────────────────────────────


def test_config_enabled_reads_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, ttl="900")
    config = MachineClientConfig.from_env()
    assert config is not None
    assert config.client_id == _CLIENT_ID
    assert config.secret_hash == _SECRET_HASH
    assert config.sub == _MACHINE_SUB
    assert config.token_ttl_seconds == 900


def test_config_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert MachineClientConfig.from_env() is None


@pytest.mark.parametrize(
    "present",
    [
        "OMNIGENT_MACHINE_CLIENT_ID",
        "OMNIGENT_MACHINE_CLIENT_SECRET_HASH",
        "OMNIGENT_MACHINE_SUB",
    ],
)
def test_config_partial_is_an_error(monkeypatch: pytest.MonkeyPatch, present: str) -> None:
    """A half-config raises instead of quietly resolving to "off".

    An operator who set one variable meant to enable the grant; coming up
    without it would surface later as a grant that answers every request
    ``unsupported_grant_type``.
    """
    _clear(monkeypatch)
    monkeypatch.setenv(present, "value")
    with pytest.raises(RuntimeError, match="must all be set"):
        MachineClientConfig.from_env()


def test_config_raw_secret_in_place_of_hash_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored form must be the digest — the raw secret is the trap.

    Unvalidated it would parse fine and then never match, leaving a token
    endpoint that 401s every correct credential for no visible reason.
    """
    _configure(monkeypatch)
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_SECRET_HASH", _CLIENT_SECRET)
    with pytest.raises(RuntimeError, match="hash_secret digest"):
        MachineClientConfig.from_env()


@pytest.mark.parametrize("bad", ["deadbeef", "z" * 64, _SECRET_HASH + "00"])
def test_config_malformed_secret_hash_is_an_error(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Too short, non-hex, and too long are all refused."""
    _configure(monkeypatch)
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_SECRET_HASH", bad)
    with pytest.raises(RuntimeError, match="hash_secret digest"):
        MachineClientConfig.from_env()


def test_secret_hash_pattern_is_anchored() -> None:
    """The pattern carries its own anchors, not just the caller's ``fullmatch``.

    Were the strictness only in the call site, a later refactor to ``match`` or
    ``search`` would silently widen the check to "contains 64 hex characters
    somewhere" — which the raw-secret trap could then slip through.
    """
    assert _SECRET_HASH_RE.fullmatch(_SECRET_HASH) is not None
    assert _SECRET_HASH_RE.match(_SECRET_HASH + "extra") is None
    assert _SECRET_HASH_RE.search("junk" + _SECRET_HASH) is None
    # ``\Z`` rather than ``$``: ``$`` would accept a trailing newline.
    assert _SECRET_HASH_RE.match(_SECRET_HASH + "\n") is None


def test_config_uppercase_secret_hash_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    """An uppercased digest still matches — the comparison is exact-string."""
    _configure(monkeypatch)
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_SECRET_HASH", _SECRET_HASH.upper())
    config = MachineClientConfig.from_env()
    assert config is not None
    assert _client_matches(_CLIENT_ID, _CLIENT_SECRET, config, _COOKIE_SECRET) is True


@pytest.mark.parametrize("reserved", ["local", "__public__"])
def test_config_reserved_principal_is_an_error(
    monkeypatch: pytest.MonkeyPatch, reserved: str
) -> None:
    """The machine principal must be a distinct identity, never a sentinel."""
    _configure(monkeypatch)
    monkeypatch.setenv("OMNIGENT_MACHINE_SUB", reserved)
    with pytest.raises(RuntimeError, match="reserved identity"):
        MachineClientConfig.from_env()


def test_config_default_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    config = MachineClientConfig.from_env()
    assert config is not None and config.token_ttl_seconds == 3600


@pytest.mark.parametrize(
    ("bad", "message"),
    [("not-an-int", "not an integer"), ("0", "must be a positive"), ("-5", "must be a positive")],
)
def test_config_bad_ttl_is_an_error(
    monkeypatch: pytest.MonkeyPatch, bad: str, message: str
) -> None:
    """An unusable TTL raises rather than silently taking the default."""
    _configure(monkeypatch, ttl=bad)
    with pytest.raises(RuntimeError, match=message):
        MachineClientConfig.from_env()


def test_config_ttl_above_ceiling_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expiry is the only revocation, so the TTL ceiling is enforced, not advised."""
    _configure(monkeypatch, ttl="3601")
    with pytest.raises(RuntimeError, match="exceeds the 3600s ceiling"):
        MachineClientConfig.from_env()
    _configure(monkeypatch, ttl="3600")
    config = MachineClientConfig.from_env()
    assert config is not None and config.token_ttl_seconds == 3600


# ── Credential presentation + verification ────────────────────────


def _basic(client_id: str, secret: str, *, scheme: str = "Basic") -> str:
    raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    return f"{scheme} {raw}"


def test_presented_client_from_form() -> None:
    request = MagicMock()
    request.headers = {}
    form = FormData([("client_id", _CLIENT_ID), ("client_secret", _CLIENT_SECRET)])
    assert _presented_client(request, form) == (_CLIENT_ID, _CLIENT_SECRET)


def test_presented_client_from_basic_header_takes_precedence() -> None:
    request = MagicMock()
    request.headers = {"Authorization": _basic("basic-id", "basic-secret")}
    form = FormData([("client_id", _CLIENT_ID), ("client_secret", _CLIENT_SECRET)])
    assert _presented_client(request, form) == ("basic-id", "basic-secret")


@pytest.mark.parametrize("scheme", ["basic", "BASIC", "BaSiC"])
def test_presented_client_basic_scheme_is_case_insensitive(scheme: str) -> None:
    """RFC 7235 §2.1: the auth scheme is a case-insensitive token."""
    request = MagicMock()
    request.headers = {"Authorization": _basic(_CLIENT_ID, _CLIENT_SECRET, scheme=scheme)}
    assert _presented_client(request, FormData([])) == (_CLIENT_ID, _CLIENT_SECRET)


def test_presented_client_none_when_secret_absent() -> None:
    request = MagicMock()
    request.headers = {}
    form = FormData([("client_id", _CLIENT_ID)])
    assert _presented_client(request, form) is None


def test_presented_client_basic_credentials_are_form_urldecoded() -> None:
    """RFC 6749 §2.3.1: both halves are form-urlencoded before the base64.

    Without the decode a secret containing ``:``, ``%``, ``+`` or a space is
    read as its encoded form and never matches.
    """
    request = MagicMock()
    raw = base64.b64encode(b"svc%3Aone:p%40ss+word%25").decode("ascii")
    request.headers = {"Authorization": f"Basic {raw}"}
    assert _presented_client(request, FormData([])) == ("svc:one", "p@ss word%")


def test_presented_client_none_on_malformed_basic() -> None:
    request = MagicMock()
    request.headers = {"Authorization": "Basic !!!not-base64!!!"}
    assert _presented_client(request, FormData([])) is None


def test_client_matches_true_for_correct_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    config = MachineClientConfig.from_env()
    assert config is not None
    assert _client_matches(_CLIENT_ID, _CLIENT_SECRET, config, _COOKIE_SECRET) is True


def test_client_matches_false_for_wrong_secret_or_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    config = MachineClientConfig.from_env()
    assert config is not None
    assert _client_matches(_CLIENT_ID, "wrong", config, _COOKIE_SECRET) is False
    assert _client_matches("wrong-id", _CLIENT_SECRET, config, _COOKIE_SECRET) is False


# ── Enabling the grant: opt-in, default-off ───────────────────────


def test_unconfigured_grant_builds_no_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLOCKING contract: no machine client → no handler.

    The grant is opt-in like the device grant next door; a deployment that
    configures none must not gain a working ``client_credentials`` exchange.
    """
    assert _handler(monkeypatch, configure=False) is None


def test_unconfigured_grant_is_an_unsupported_grant_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end an operator sees: the grant type is simply not served.

    ``/oauth/token`` itself stays routed — main mounts it for login-issued
    refresh grants regardless — so the default-off signal is the grant-type
    refusal, not a 404 on the path.
    """
    _clear(monkeypatch)
    client = _app_with(_handler(monkeypatch, configure=False))
    resp = client.post("/oauth/token", data={"grant_type": "client_credentials"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_admin_sub_is_not_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BLOCKING guard: an admin ``sub`` refuses to enable the grant.

    The path allowlist confines the token to the session APIs but not its
    privilege there — /v1/sessions' ``is_admin → LEVEL_OWNER`` override would
    make such a client OWNER of every tenant's session.
    """
    with caplog.at_level("ERROR"):
        handler = _handler(monkeypatch, store=_FakeStore(admins=(_MACHINE_SUB,)))
    assert handler is None
    assert any("admin principal" in r.message for r in caplog.records)


def test_non_admin_sub_enables_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-admin ``sub`` enables an active grant and mints a token."""
    client = _client(monkeypatch, store=_FakeStore(admins=("someone-else@example.com",)))
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 200, resp.text


class _BrokenStore(_FakeStore):
    """A permission store whose admin lookup always raises."""

    def is_admin(self, user_id: str) -> bool:
        raise RuntimeError("store down")


class _FlakyStore(_FakeStore):
    """A store that answers until ``broken`` is set — a fault after mount."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False

    def is_admin(self, user_id: str) -> bool:
        if self.broken:
            raise RuntimeError("store down")
        return False


def test_store_error_at_mount_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the admin check raises at mount, fail closed — no handler is built."""
    assert _handler(monkeypatch, store=_BrokenStore()) is None


def test_store_error_at_mount_is_not_reported_as_an_admin_sub(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal names the store fault instead of blaming the ``sub``.

    Both outcomes refuse the grant, but an operator told their ``sub`` is an
    admin will go audit a config that is fine while the store stays broken.
    """
    with caplog.at_level("ERROR"):
        assert _handler(monkeypatch, store=_BrokenStore()) is None
    refusals = [r.message for r in caplog.records if "refusing to enable" in r.message]
    assert refusals, "expected a refusal to be logged"
    assert not any("is an admin principal" in m for m in refusals)
    assert any("could not say whether" in m for m in refusals)


def test_factory_rejects_non_cookie_mode() -> None:
    """The grant needs the HS256 cookie secret, so header mode can't build it."""
    with pytest.raises(RuntimeError, match="oidc or accounts"):
        create_client_credentials_handler(UnifiedAuthProvider(source="header"), None)


def test_machine_sub_matching_a_real_account_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing forces the machine sub to be a DEDICATED identity.

    Pointing it at a real person's address silently mints tokens acting as
    that person, so the operator is told at mount. Advisory: the grant is
    still enabled.
    """
    with caplog.at_level("WARNING"):
        handler = _handler(monkeypatch, store=_FakeStore(users=(_MACHINE_SUB, "someone@x.com")))
    assert handler is not None
    assert any("is an existing account" in r.message for r in caplog.records)


def test_dedicated_machine_sub_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A sub that matches no account is the intended shape and stays quiet."""
    with caplog.at_level("WARNING"):
        handler = _handler(monkeypatch, store=_FakeStore(users=("someone@x.com",)))
    assert handler is not None
    assert not any("is an existing account" in r.message for r in caplog.records)


# ── /oauth/token route: success + token shape ─────────────────────


def test_token_form_credentials_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, ttl="1800")
    resp = client.post(
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

    claims = jwt.decode(body["access_token"], _COOKIE_SECRET, algorithms=["HS256"])
    assert claims["sub"] == _MACHINE_SUB
    assert claims["scope"] == "sessions"
    assert claims["act"] == {"client_id": _CLIENT_ID}
    # Rotation-model MVP: a client-credentials token carries NO grant_id.
    assert "grant_id" not in claims
    assert claims["exp"] - claims["iat"] == 1800


def test_token_response_carries_the_granted_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 6749 §5.1: ``scope`` is REQUIRED when it differs from the request.

    This grant ignores a client-sent ``scope`` (§4.4.2 permits sending one) and
    always issues ``DELEGATED_SCOPE``, so the granted scope always differs from
    whatever was asked for and the response always states it.
    """
    client = _client(monkeypatch)
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "scope": "admin everything",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "sessions"
    claims = jwt.decode(resp.json()["access_token"], _COOKIE_SECRET, algorithms=["HS256"])
    assert claims["scope"] == "sessions"


def test_token_basic_auth_credentials_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": _basic(_CLIENT_ID, _CLIENT_SECRET)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["token_type"] == "Bearer"


def test_token_basic_auth_urlencoded_secret_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret needing form-urlencoding round-trips through the Basic header."""
    secret = "p@ss word/+%"
    _clear(monkeypatch)
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_SECRET_HASH", hash_secret(secret, _COOKIE_SECRET))
    monkeypatch.setenv("OMNIGENT_MACHINE_SUB", _MACHINE_SUB)
    handler = create_client_credentials_handler(
        _make_oidc_provider(),
        _FakeStore(),  # type: ignore[arg-type]
    )
    assert handler is not None

    credential = f"{quote_plus(_CLIENT_ID)}:{quote_plus(secret)}".encode()
    resp = _app_with(handler).post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {base64.b64encode(credential).decode('ascii')}"},
    )
    assert resp.status_code == 200, resp.text


def test_token_response_is_not_cacheable(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 6749 §5.1: a response carrying a token must not be cached."""
    client = _client(monkeypatch)
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"


# ── /oauth/token route: one endpoint, three grant types ───────────


def test_machine_grant_registers_no_second_token_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLOCKING contract: the grant is a branch, never a second route.

    FastAPI resolves first-match-wins with no warning, so a parallel router on
    ``POST /oauth/token`` would leave whichever registered second as dead code
    behind a startup log claiming it was enabled.
    """
    handler = _handler(monkeypatch)
    assert handler is not None
    router = create_oauth_token_router(
        _make_oidc_provider(),
        _FakeGrantStore(),  # type: ignore[arg-type]
        handle_client_credentials=handler,
    )
    paths = [getattr(route, "path", None) for route in router.routes]
    assert paths.count("/oauth/token") == 1


def test_machine_grant_answers_under_the_device_grant_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCKING regression: the device flow must not shadow the machine grant.

    The device-grant router builds its own token endpoint, so a machine grant
    living on a parallel router went silent whenever the device grant was
    enabled. Folded in as a ``grant_type`` branch, it answers in both mounts.
    """
    handler = _handler(monkeypatch)
    assert handler is not None
    app = FastAPI()
    app.include_router(
        create_device_auth_router(
            _make_oidc_provider(provider_type="oidc"),
            _FakeGrantStore(),  # type: ignore[arg-type]
            handle_client_credentials=handler,
        )
    )
    resp = TestClient(app).post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "sessions"


def test_other_grant_types_still_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding the machine branch must not shadow the grants already there.

    ``refresh_token`` reaches its own handler (rejected here only because no
    such token exists), and an unknown grant is still refused.
    """
    client = _client(monkeypatch)
    refresh = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "nope"},
    )
    assert refresh.status_code == 400 and refresh.json()["error"] == "invalid_grant"

    unknown = client.post("/oauth/token", data={"grant_type": "password"})
    assert unknown.status_code == 400 and unknown.json()["error"] == "unsupported_grant_type"


def test_machine_grant_does_not_throttle_other_grant_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The throttle is scoped to this grant, not hoisted onto the whole route.

    Hoisting it would newly rate-limit login-grant refreshes at the same
    ceiling — costly behind a shared NAT egress IP, where many hosts renew
    through one source address.
    """
    client = _client(monkeypatch)
    for _ in range(_TOKEN_RATE_MAX * 2):
        resp = client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": "nope"},
        )
        assert resp.status_code == 400, resp.text


# ── /oauth/token route: error shapes ──────────────────────────────


def test_token_wrong_secret_is_invalid_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": "wrong",
        },
    )
    assert resp.status_code == 401 and resp.json()["error"] == "invalid_client"
    # Form credentials: no header attempt to challenge.
    assert "www-authenticate" not in resp.headers
    assert resp.headers["cache-control"] == "no-store"


def test_token_rejected_basic_header_gets_a_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC 6749 §5.2: a 401 that rejected an Authorization header must challenge."""
    client = _client(monkeypatch)
    resp = client.post(
        "/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": _basic(_CLIENT_ID, "wrong")},
    )
    assert resp.status_code == 401 and resp.json()["error"] == "invalid_client"
    assert resp.headers["www-authenticate"].startswith("Basic realm=")


def test_token_absent_credentials_is_invalid_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    resp = client.post("/oauth/token", data={"grant_type": "client_credentials"})
    assert resp.status_code == 401 and resp.json()["error"] == "invalid_client"


def test_token_wrong_client_id_is_invalid_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "someone-else",
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 401 and resp.json()["error"] == "invalid_client"


def test_sub_promoted_to_admin_stops_minting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin guard is re-run per mint, not only at mount.

    A ``sub`` promoted after startup would otherwise keep minting tokens that
    inherit /v1/sessions' ``is_admin → LEVEL_OWNER`` override until a restart.
    RFC 6749 §5.2 puts ``unauthorized_client`` at 400.
    """
    store = _FakeStore()
    client = _client(monkeypatch, store=store)
    credentials = {
        "grant_type": "client_credentials",
        "client_id": _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
    }
    assert client.post("/oauth/token", data=credentials).status_code == 200

    store.admins.add(_MACHINE_SUB)
    resp = client.post("/oauth/token", data=credentials)
    assert resp.status_code == 400 and resp.json()["error"] == "unauthorized_client"


def test_store_error_at_mint_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that breaks after mount stops new tokens rather than trusting the sub."""
    store = _FlakyStore()
    client = _client(monkeypatch, store=store)
    store.broken = True
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 400 and resp.json()["error"] == "unauthorized_client"


def test_store_error_at_mint_is_not_reported_as_an_admin_sub(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The mint refusal, too, distinguishes a store fault from a promoted sub."""
    store = _FlakyStore()
    client = _client(monkeypatch, store=store)
    store.broken = True
    with caplog.at_level("ERROR"):
        resp = client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
            },
        )
    assert resp.status_code == 400
    refusals = [r.message for r in caplog.records if "refusing to mint" in r.message]
    assert refusals, "expected a mint refusal to be logged"
    assert not any("is now an admin" in m for m in refusals)
    assert any("could not say whether" in m for m in refusals)


# ── /oauth/token route: throttle ──────────────────────────────────


def test_token_endpoint_is_throttled_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-authentication client check is not free to hammer.

    Nothing has authenticated when the secret comparison runs, so the grant
    carries the same per-IP sliding window the device grant applies to its
    public authorize endpoint.
    """
    client = _client(monkeypatch)
    wrong = {
        "grant_type": "client_credentials",
        "client_id": _CLIENT_ID,
        "client_secret": "wrong",
    }
    for _ in range(_TOKEN_RATE_MAX):
        assert client.post("/oauth/token", data=wrong).status_code == 401
    throttled = client.post("/oauth/token", data=wrong)
    assert throttled.status_code == 429 and throttled.json()["error"] == "slow_down"
    assert throttled.headers["cache-control"] == "no-store"


def test_throttle_gates_ahead_of_the_credential_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the ceiling, even a correct credential is refused.

    A limiter that only counted failed authentications would still answer the
    guess that happened to be right, leaving the guess rate unbounded.
    """
    client = _client(monkeypatch)
    for _ in range(_TOKEN_RATE_MAX):
        client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": _CLIENT_ID,
                "client_secret": "wrong",
            },
        )
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert resp.status_code == 429 and resp.json()["error"] == "slow_down"


# ── _check_cookie: which claims confine a token ───────────────────


def _req(path: str, *, bearer: str) -> MagicMock:
    """Build a minimal mock HTTPConnection carrying a bearer token.

    MagicMock is acceptable here: the claim gate reads only
    ``request.headers``, ``request.cookies`` and ``request.url.path``, and
    ``HTTPConnection`` cannot be trivially constructed without a real scope.

    :param path: The request path the allowlist is checked against.
    :param bearer: The raw JWT to present as ``Authorization: Bearer``.
    :returns: A mock with ``.headers``, ``.cookies`` and ``.url.path`` set.
    """
    mock = MagicMock()
    mock.cookies = {}
    mock.headers = {"Authorization": f"Bearer {bearer}"}
    mock.url.path = path
    return mock


def _machine_token() -> str:
    """A client-credentials token: ``scope``, no ``grant_id``."""
    return mint_delegated_token(
        _MACHINE_SUB,
        _COOKIE_SECRET,
        3600,
        "oidc",
        grant_id=None,
        client_id=_CLIENT_ID,
        jti="jti-machine",
    )


def _device_grant_token(grant_id: str = "device-1") -> str:
    """A third-party device-grant token: ``scope`` AND ``grant_id``."""
    return mint_delegated_token(
        _MACHINE_SUB,
        _COOKIE_SECRET,
        3600,
        "oidc",
        grant_id=grant_id,
        client_id="slack",
        jti="jti-device",
    )


def _login_grant_token(grant_id: str = "login-1") -> str:
    """A first-party login-grant token: ``grant_id``, NO ``scope``."""
    return mint_delegated_token(
        _MACHINE_SUB,
        _COOKIE_SECRET,
        3600,
        "oidc",
        grant_id=grant_id,
        client_id=LOGIN_GRANT_CLIENT_ID,
        jti="jti-login",
        scope=None,
    )


@pytest.mark.parametrize(
    ("shape", "token_factory", "allowlisted", "non_allowlisted"),
    [
        ("device grant (scope + grant_id)", _device_grant_token, _MACHINE_SUB, None),
        ("login grant (grant_id, no scope)", _login_grant_token, _MACHINE_SUB, _MACHINE_SUB),
        ("client credentials (scope, no grant_id)", _machine_token, _MACHINE_SUB, None),
    ],
)
def test_token_authority_table(
    shape: str,
    token_factory,
    allowlisted: str | None,
    non_allowlisted: str | None,
) -> None:
    """Pin all three minted token shapes against the path allowlist together.

    The three rows encode one invariant each, and they have inverted against
    each other before — a change that confines on ``grant_id``'s ABSENCE
    rather than on ``scope``'s PRESENCE swaps rows 2 and 3 silently:

    ==========================================  ==============  =============
    token shape                                 /v1/sessions    /auth/users
    ==========================================  ==============  =============
    device grant (``scope`` + ``grant_id``)     allow           deny
    login grant (``grant_id``, no ``scope``)    allow           allow
    client creds (``scope``, no ``grant_id``)   allow           deny
    ==========================================  ==============  =============

    Row 2 is deliberate: a login grant renews the session JWT it replaced and
    keeps that authority. Rows 1 and 3 are restricted at mint and confined
    here; row 3 is the whole security argument for the machine grant.
    """
    provider = _make_oidc_provider()
    token = token_factory()
    assert provider._check_cookie(_req("/v1/sessions", bearer=token)) == allowlisted, shape
    assert provider._check_cookie(_req("/auth/users", bearer=token)) == non_allowlisted, shape


@pytest.mark.parametrize(
    ("shape", "token_factory", "revocable"),
    [
        ("device grant", _device_grant_token, True),
        ("login grant", _login_grant_token, True),
        ("client credentials", _machine_token, False),
    ],
)
def test_revocation_denylist_applies_only_to_stored_grants(
    shape: str, token_factory, revocable: bool
) -> None:
    """A ``grant_id``-less token must SKIP the denylist, not fail it.

    The lookup fails closed on an unknown grant, so running it with ``None``
    for a machine token — which carries no ``grant_id`` — would reject every
    one of them and silently kill the grant.
    """
    provider = _make_oidc_provider()
    provider.set_grant_revocation_check(lambda grant_id: True)
    result = provider._check_cookie(_req("/v1/sessions", bearer=token_factory()))
    assert result == (None if revocable else _MACHINE_SUB), shape


def test_no_minted_token_shape_is_ever_cached() -> None:
    """None of the three shapes may reach the token-keyed credential cache.

    The cache is keyed by token, not path, so a cached entry replayed on
    another path would skip both the allowlist and the revocation lookup.
    """
    for token_factory in (_device_grant_token, _login_grant_token, _machine_token):
        provider = _make_oidc_provider()
        provider._check_cookie(_req("/v1/sessions", bearer=token_factory()))
        assert provider._cookie_cache == {}


def test_scope_token_allowed_on_allowlisted_path() -> None:
    provider = _make_oidc_provider()
    assert provider._check_cookie(_req("/v1/sessions", bearer=_machine_token())) == _MACHINE_SUB
    assert (
        provider._check_cookie(_req("/v1/sessions/abc/events", bearer=_machine_token()))
        == _MACHINE_SUB
    )


def test_scope_token_rejected_on_non_allowlisted_path() -> None:
    provider = _make_oidc_provider()
    assert provider._check_cookie(_req("/auth/users", bearer=_machine_token())) is None
    assert provider._check_cookie(_req("/v1/me", bearer=_machine_token())) is None


def test_scope_token_never_cached_so_no_path_bypass() -> None:
    """A prior allowed call must NOT let a later disallowed path through.

    The credential cache is keyed by token, not path; caching a scope token
    would let a replay on a non-allowlisted path skip the allowlist. The
    scope branch returns before the cache, so every request re-checks.
    """
    provider = _make_oidc_provider()
    token = _machine_token()
    # Allowed path first — this must not populate the cache.
    assert provider._check_cookie(_req("/v1/sessions", bearer=token)) == _MACHINE_SUB
    assert provider._cookie_cache == {}
    # Same token, disallowed path: still rejected (allowlist re-runs).
    assert provider._check_cookie(_req("/auth/users", bearer=token)) is None
    assert provider._cookie_cache == {}


def test_plain_session_token_still_cached_and_path_agnostic() -> None:
    """Regression: a non-scoped session token is cached and works anywhere."""
    provider = _make_oidc_provider()
    token = mint_session_token("alice@example.com", _COOKIE_SECRET, 3600, "google")
    assert provider._check_cookie(_req("/auth/users", bearer=token)) == "alice@example.com"
    assert len(provider._cookie_cache) == 1
    # A second call on any path is served identically.
    assert provider._check_cookie(_req("/v1/me", bearer=token)) == "alice@example.com"


def test_grant_id_token_still_hits_revocation_denylist() -> None:
    """Device tokens (scope + grant_id) still consult the revocation check."""
    provider = _make_oidc_provider()
    provider.set_grant_revocation_check(lambda grant_id: grant_id == "revoked-1")

    assert (
        provider._check_cookie(_req("/v1/sessions", bearer=_device_grant_token("revoked-1")))
        is None
    )
    assert (
        provider._check_cookie(_req("/v1/sessions", bearer=_device_grant_token("live-1")))
        == _MACHINE_SUB
    )


def test_non_string_grant_id_is_rejected() -> None:
    """A malformed ``grant_id`` claim fails closed rather than being ignored."""
    provider = _make_oidc_provider()
    payload = {
        "sub": _MACHINE_SUB,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "provider": "oidc",
        "grant_id": 12345,
        "scope": "sessions",
        "act": {"client_id": _CLIENT_ID},
    }
    token = jwt.encode(payload, _COOKIE_SECRET, algorithm="HS256")
    assert provider._check_cookie(_req("/v1/sessions", bearer=token)) is None


def test_expired_scope_token_rejected() -> None:
    provider = _make_oidc_provider()
    payload = {
        "sub": _MACHINE_SUB,
        "iat": int(time.time()) - 7200,
        "exp": int(time.time()) - 1,
        "provider": "oidc",
        "scope": "sessions",
        "act": {"client_id": _CLIENT_ID},
    }
    token = jwt.encode(payload, _COOKIE_SECRET, algorithm="HS256")
    assert provider._check_cookie(_req("/v1/sessions", bearer=token)) is None
