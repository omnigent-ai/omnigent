"""Unit tests for the OAuth 2.0 client-credentials (machine-auth) grant.

Three layers, all network-free:

1. :class:`MachineClientConfig` env parsing — enabled, cleanly off, and the
   misconfigurations (partial, malformed secret hash, reserved principal, bad
   or over-ceiling TTL) that raise.
2. The router factory and the ``POST /oauth/token`` route on a minimal
   OIDC-mode app — default-off (no router at all when unconfigured or when the
   principal is refused), the token shape, the RFC 6749 §5.1 / §5.2 response
   headers, the per-source throttle in front of the pre-authentication client
   check, and every error shape.
3. The ``UnifiedAuthProvider._check_cookie`` scope gate — a scope-carrying
   token is confined to the delegated path allowlist, never cached (so a
   prior allowed call can't let a later disallowed path through), and the
   revocation denylist still fires for device tokens that carry a grant_id.
"""

from __future__ import annotations

import base64
import time
from unittest.mock import MagicMock
from urllib.parse import quote_plus

import jwt
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.device_grant_store import hash_secret
from omnigent.server.oidc import OIDCConfig, mint_session_token
from omnigent.server.routes.client_credentials import (
    _SECRET_HASH_RE,
    _TOKEN_RATE_MAX,
    MachineClientConfig,
    _client_matches,
    _presented_client,
    create_client_credentials_router,
)
from omnigent.server.routes.device_auth import mint_delegated_token

_COOKIE_SECRET = b"c" * 32
_CLIENT_ID = "svc-omnigent"
_CLIENT_SECRET = "top-secret-machine-key"
_SECRET_HASH = hash_secret(_CLIENT_SECRET, _COOKIE_SECRET)
_MACHINE_SUB = "machine@example.com"


class _FakeStore:
    """Duck-typed permission store: only the admin guard the router calls.

    The router touches the store solely through ``is_admin`` — at mount and
    again before every mint — so a minimal stand-in avoids implementing the
    whole ABC.
    """

    def __init__(self, *, admins: tuple[str, ...] = ()) -> None:
        self.admins = set(admins)

    def is_admin(self, user_id: str) -> bool:
        return user_id in self.admins


# ── Fixtures / helpers ────────────────────────────────────────────


def _make_oidc_provider() -> UnifiedAuthProvider:
    """A GitHub-flavoured OIDC provider (no discovery fetch, no network)."""
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
        provider_type="github",
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


def _router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    configure: bool = True,
    ttl: str | None = None,
    store: _FakeStore | None = None,
) -> APIRouter | None:
    """Build the router (or ``None``) the way ``app.py`` does.

    Env must be set BEFORE the router is created — the machine-client config
    is read once at mount, matching how the other auth env vars behave.
    """
    _clear(monkeypatch)
    if configure:
        _configure(monkeypatch, ttl=ttl)
    return create_client_credentials_router(
        _make_oidc_provider(),
        _FakeStore() if store is None else store,  # type: ignore[arg-type]
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ttl: str | None = None,
    store: _FakeStore | None = None,
) -> TestClient:
    """Build a minimal app with only the client-credentials router mounted.

    The store defaults to one where the machine sub is a non-admin identity,
    so the router mounts.
    """
    router = _router(monkeypatch, ttl=ttl, store=store)
    assert router is not None, "expected the grant to mount for this config"
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


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


def test_config_is_validated_even_when_another_grant_owns_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed config raises regardless of whether this grant will mount.

    The device grant owns ``POST /oauth/token`` when it is enabled, so this
    grant stands down. Parsing still happens, because a deploy that misspelled
    one variable should fail at startup rather than come up looking healthy and
    reveal the mistake only after the other grant is switched off.
    """
    _clear(monkeypatch)
    monkeypatch.setenv("OMNIGENT_MACHINE_CLIENT_ID", "partial-config-only")
    with pytest.raises(RuntimeError, match="must all be set"):
        MachineClientConfig.from_env()


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
    without it would surface later as an unrouted token endpoint.
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


# ── Mounting: opt-in, default-off ─────────────────────────────────


def test_unconfigured_grant_is_not_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLOCKING contract: no machine client → no router, so /oauth/token 404s.

    The grant is opt-in like the device grant next door; a deployment that
    configures none must not see POST /oauth/token go from unrouted to routed.
    """
    assert _router(monkeypatch, configure=False) is None


def test_unconfigured_grant_leaves_the_path_unrouted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end an operator sees: nothing answers on /oauth/token."""
    _clear(monkeypatch)
    app = FastAPI()
    router = _router(monkeypatch, configure=False)
    if router is not None:  # pragma: no cover — guarded by the test above
        app.include_router(router)
    resp = TestClient(app).post("/oauth/token", data={"grant_type": "client_credentials"})
    assert resp.status_code == 404


def test_admin_sub_is_not_mounted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BLOCKING guard: an admin ``sub`` refuses to mount the grant.

    The path allowlist confines the token to the session APIs but not its
    privilege there — /v1/sessions' ``is_admin → LEVEL_OWNER`` override would
    make such a client OWNER of every tenant's session.
    """
    with caplog.at_level("ERROR"):
        router = _router(monkeypatch, store=_FakeStore(admins=(_MACHINE_SUB,)))
    assert router is None
    assert any("admin principal" in r.message for r in caplog.records)


def test_non_admin_sub_enables_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-admin ``sub`` mounts an active grant and mints a token."""
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
    """If the admin check raises at mount, fail closed — nothing is mounted."""
    assert _router(monkeypatch, store=_BrokenStore()) is None


def test_store_error_at_mount_is_not_reported_as_an_admin_sub(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal names the store fault instead of blaming the ``sub``.

    Both outcomes refuse the grant, but an operator told their ``sub`` is an
    admin will go audit a config that is fine while the store stays broken.
    """
    with caplog.at_level("ERROR"):
        assert _router(monkeypatch, store=_BrokenStore()) is None
    refusals = [r.message for r in caplog.records if "refusing to mount" in r.message]
    assert refusals, "expected a mount refusal to be logged"
    assert not any("is an admin principal" in m for m in refusals)
    assert any("could not say whether" in m for m in refusals)


def test_router_rejects_non_cookie_mode() -> None:
    """The grant needs the HS256 cookie secret, so header mode can't mount it."""
    with pytest.raises(RuntimeError, match="oidc or accounts"):
        create_client_credentials_router(UnifiedAuthProvider(source="header"), None)


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
    router = create_client_credentials_router(
        _make_oidc_provider(),
        _FakeStore(),  # type: ignore[arg-type]
    )
    assert router is not None
    app = FastAPI()
    app.include_router(router)

    credential = f"{quote_plus(_CLIENT_ID)}:{quote_plus(secret)}".encode()
    resp = TestClient(app).post(
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


def test_token_other_grant_type_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/oauth/token",
        data={"grant_type": "password", "username": "x", "password": "y"},
    )
    assert resp.status_code == 400 and resp.json()["error"] == "unsupported_grant_type"


def test_sub_promoted_to_admin_stops_minting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin guard is re-run per mint, not only at mount.

    A ``sub`` promoted after startup would otherwise keep minting tokens that
    inherit /v1/sessions' ``is_admin → LEVEL_OWNER`` override until a restart.
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
    assert resp.status_code == 403 and resp.json()["error"] == "unauthorized_client"


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
    assert resp.status_code == 403 and resp.json()["error"] == "unauthorized_client"


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
    assert resp.status_code == 403
    refusals = [r.message for r in caplog.records if "refusing to mint" in r.message]
    assert refusals, "expected a mint refusal to be logged"
    assert not any("is now an admin" in m for m in refusals)
    assert any("could not say whether" in m for m in refusals)


# ── /oauth/token route: throttle ──────────────────────────────────


def test_token_endpoint_is_throttled_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-authentication client check is not free to hammer.

    Nothing has authenticated when the secret comparison runs, so the endpoint
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


# ── _check_cookie: scope allowlist + cache-bypass close ────────────


def _req(path: str, *, bearer: str) -> MagicMock:
    """Build a minimal mock HTTPConnection carrying a bearer token.

    MagicMock is acceptable here: the scope gate reads only
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
    return mint_delegated_token(
        _MACHINE_SUB,
        _COOKIE_SECRET,
        3600,
        "oidc",
        client_id=_CLIENT_ID,
        jti="jti-1",
    )


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

    revoked = mint_delegated_token(
        _MACHINE_SUB,
        _COOKIE_SECRET,
        3600,
        "oidc",
        grant_id="revoked-1",
        client_id="slack",
        jti="a",
    )
    live = mint_delegated_token(
        _MACHINE_SUB, _COOKIE_SECRET, 3600, "oidc", grant_id="live-1", client_id="slack", jti="b"
    )
    assert provider._check_cookie(_req("/v1/sessions", bearer=revoked)) is None
    assert provider._check_cookie(_req("/v1/sessions", bearer=live)) == _MACHINE_SUB


def test_grant_id_without_scope_still_confined_to_allowlist() -> None:
    """A token carrying only ``grant_id`` is confined and stays uncached.

    Minting always sets ``scope``, so this shape only arises from a token
    that lost the claim. It must still take the delegated branch — otherwise
    it would reach the token-keyed cache and become path-agnostic.
    """
    provider = _make_oidc_provider()
    payload = {
        "sub": _MACHINE_SUB,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "provider": "oidc",
        "grant_id": "grant-1",
        "act": {"client_id": _CLIENT_ID},
    }
    token = jwt.encode(payload, _COOKIE_SECRET, algorithm="HS256")
    assert provider._check_cookie(_req("/v1/sessions", bearer=token)) == _MACHINE_SUB
    assert provider._check_cookie(_req("/auth/users", bearer=token)) is None
    assert provider._cookie_cache == {}


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
