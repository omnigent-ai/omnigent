"""Unit tests for the RFC 7523 JWT bearer grant.

Network-free: the issuer's key set is served by an in-process
:class:`jwt.PyJWKClient` whose fetch returns a locally generated JWKS.

1. :class:`JwtBearerConfig` env parsing and the handler factory's mount checks.
2. Exchanges through the real ``POST /oauth/token`` router: the issued token's
   shape and lifetime, every assertion rejection, principal vetting and the
   throttle.
3. The auth layer's treatment of an exchanged token: confined to the delegated
   allowlist and never cached.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.entities import Account
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes import jwt_bearer
from omnigent.server.routes.device_auth import (
    create_device_auth_router,
    create_oauth_token_router,
)
from omnigent.server.routes.jwt_bearer import (
    JWT_BEARER_GRANT_TYPE,
    JwtBearerConfig,
    create_jwt_bearer_handler,
)

_COOKIE_SECRET = b"c" * 32
_ISSUER = "https://sso.example.com/realms/main"
_AUDIENCE = "omnigent-api"
_JWKS_URI = "https://sso.example.com/realms/main/certs"
_CLIENT = "ci-triage"
_PRINCIPAL = "ci-triage@ci.invalid"
_OTHER_CLIENT = "ci-release"
_OTHER_PRINCIPAL = "ci-release@ci.invalid"


class _Issuer:
    """An identity provider: one signing key, its JWKS and an assertion signer."""

    def __init__(self, *, kid: str = "key-1") -> None:
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": kid, "use": "sig", "alg": "RS256"}]}

    def sign(
        self,
        *,
        alg: str = "RS256",
        kid: str | None = None,
        key: object | None = None,
        lifetime: int = 300,
        iat_offset: int = 0,
        drop: tuple[str, ...] = (),
        **claims: object,
    ) -> str:
        now = int(time.time()) + iat_offset
        payload: dict[str, object] = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "azp": _CLIENT,
            "iat": now,
            "exp": now + lifetime,
            "jti": "assertion-1",
        }
        payload.update(claims)
        for name in drop:
            payload.pop(name, None)
        return jwt.encode(
            payload,
            self.key if key is None else key,
            algorithm=alg,
            headers={"kid": self.kid if kid is None else kid},
        )


class _FakeJwksClient(jwt.PyJWKClient):
    """A PyJWKClient whose fetch returns a fixed document and counts calls."""

    def __init__(self, document: dict[str, object]) -> None:
        super().__init__(_JWKS_URI)
        self.document = document
        self.fetches = 0

    def fetch_data(self) -> object:
        self.fetches += 1
        if self.jwk_set_cache is not None:
            self.jwk_set_cache.put(self.document)
        return self.document


class _FakeStore:
    def __init__(self, *, admins: tuple[str, ...] = (), users: tuple[str, ...] = ()) -> None:
        self.admins = set(admins)
        self.users = set(users)

    def is_admin(self, user_id: str) -> bool:
        return user_id in self.admins

    def list_users(self, *, limit: int = 1000) -> list[Account]:
        return [
            Account(id=u, is_admin=False, created_at=None, last_login_at=None, has_password=False)
            for u in sorted(self.users)[:limit]
        ]


class _FakeAdminList:
    def __init__(self, *admins: str) -> None:
        self.admins = set(admins)

    def is_admin(self, identity: str) -> bool:
        return identity in self.admins


class _FakeGrantStore:
    def get_by_refresh_hash(self, refresh_hash: str) -> None:
        return None

    def get_by_prev_refresh_hash(self, refresh_hash: str) -> None:
        return None

    def purge_expired(self, now_epoch_seconds: int, *, max_lifetime_seconds: int) -> int:
        return 0


def _provider(*, issuer: str = _ISSUER, jwks_uri: str | None = None) -> UnifiedAuthProvider:
    config = OIDCConfig(
        issuer=issuer,
        client_id="oidc-client",
        client_secret="oidc-secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_COOKIE_SECRET,
        scopes="openid email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=frozenset({"example.com"}),
        provider_type="oidc",
        authorization_endpoint=f"{issuer}/auth",
        token_endpoint=f"{issuer}/token",
        jwks_uri=jwks_uri,
        userinfo_endpoint=None,
        allow_invites=False,
    )
    return UnifiedAuthProvider(source="oidc", oidc_config=config)


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    values: dict[str, str | None] = {
        "OMNIGENT_JWT_BEARER_ISSUER": _ISSUER,
        "OMNIGENT_JWT_BEARER_AUDIENCE": _AUDIENCE,
        "OMNIGENT_JWT_BEARER_CLIENTS": (
            f"{_CLIENT}={_PRINCIPAL}, {_OTHER_CLIENT}={_OTHER_PRINCIPAL}"
        ),
        "OMNIGENT_JWT_BEARER_CLIENT_CLAIM": None,
        "OMNIGENT_JWT_BEARER_JWKS_URI": _JWKS_URI,
        "OMNIGENT_JWT_BEARER_TOKEN_TTL": None,
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in jwt_bearer._ALL_ENV:
        monkeypatch.delenv(name, raising=False)


def _handler(
    monkeypatch: pytest.MonkeyPatch,
    issuer: _Issuer,
    *,
    store: _FakeStore | None = None,
    admin_list: _FakeAdminList | None = None,
    jwks_client: _FakeJwksClient | None = None,
    **env: str | None,
):
    _configure(monkeypatch, **env)
    return create_jwt_bearer_handler(
        _provider(),
        _FakeStore() if store is None else store,  # type: ignore[arg-type]
        admin_list=admin_list,  # type: ignore[arg-type]
        jwks_client=jwks_client or _FakeJwksClient(issuer.jwks),
    )


def _client(monkeypatch: pytest.MonkeyPatch, issuer: _Issuer, **kwargs) -> TestClient:
    handler = _handler(monkeypatch, issuer, **kwargs)
    assert handler is not None
    app = FastAPI()
    app.include_router(
        create_oauth_token_router(
            _provider(),
            _FakeGrantStore(),  # type: ignore[arg-type]
            handle_jwt_bearer=handler,
        )
    )
    return TestClient(app)


def _exchange(client: TestClient, assertion: str):
    return client.post(
        "/oauth/token", data={"grant_type": JWT_BEARER_GRANT_TYPE, "assertion": assertion}
    )


@pytest.fixture
def issuer() -> _Issuer:
    return _Issuer()


# ── Configuration ─────────────────────────────────────────────────


def test_config_off_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert JwtBearerConfig.from_env() is None
    assert create_jwt_bearer_handler(_provider(), None) is None


def test_config_reads_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, OMNIGENT_JWT_BEARER_TOKEN_TTL="900")
    config = JwtBearerConfig.from_env()
    assert config is not None
    assert config.issuer == _ISSUER
    assert config.audience == _AUDIENCE
    assert config.clients == {_CLIENT: _PRINCIPAL, _OTHER_CLIENT: _OTHER_PRINCIPAL}
    assert config.client_claim == "azp"
    assert config.jwks_uri == _JWKS_URI
    assert config.token_ttl_seconds == 900


def test_config_defaults_ttl_to_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    config = JwtBearerConfig.from_env()
    assert config is not None and config.token_ttl_seconds == 3600


@pytest.mark.parametrize(
    "missing",
    ["OMNIGENT_JWT_BEARER_ISSUER", "OMNIGENT_JWT_BEARER_AUDIENCE", "OMNIGENT_JWT_BEARER_CLIENTS"],
)
def test_config_partial_is_an_error(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _configure(monkeypatch, **{missing: None})
    with pytest.raises(RuntimeError, match="must all be set"):
        JwtBearerConfig.from_env()


def test_config_any_single_variable_enables_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("OMNIGENT_JWT_BEARER_JWKS_URI", _JWKS_URI)
    with pytest.raises(RuntimeError, match="must all be set"):
        JwtBearerConfig.from_env()


@pytest.mark.parametrize(
    ("clients", "match"),
    [
        ("ci-triage", "not a client=principal pair"),
        ("=ci@ci.invalid", "not a client=principal pair"),
        ("ci-triage=", "not a client=principal pair"),
        ("a=a@ci.invalid,a=b@ci.invalid", "twice"),
        ("a=local", "reserved identity"),
        (" , ", "names no client"),
    ],
)
def test_config_malformed_clients(
    monkeypatch: pytest.MonkeyPatch, clients: str, match: str
) -> None:
    _configure(monkeypatch, OMNIGENT_JWT_BEARER_CLIENTS=clients)
    with pytest.raises(RuntimeError, match=match):
        JwtBearerConfig.from_env()


@pytest.mark.parametrize("ttl", ["abc", "0", "-5", "3601"])
def test_config_bad_ttl(monkeypatch: pytest.MonkeyPatch, ttl: str) -> None:
    _configure(monkeypatch, OMNIGENT_JWT_BEARER_TOKEN_TTL=ttl)
    with pytest.raises(RuntimeError, match="TOKEN_TTL"):
        JwtBearerConfig.from_env()


def test_factory_needs_jwks_for_a_foreign_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, OMNIGENT_JWT_BEARER_JWKS_URI=None)
    with pytest.raises(RuntimeError, match="OMNIGENT_JWT_BEARER_JWKS_URI"):
        create_jwt_bearer_handler(_provider(issuer="https://login.example.com"), None)


def test_factory_reuses_the_login_issuer_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, OMNIGENT_JWT_BEARER_JWKS_URI=None)
    built = MagicMock(wraps=jwt.PyJWKClient)
    monkeypatch.setattr(jwt_bearer.jwt, "PyJWKClient", built)
    assert create_jwt_bearer_handler(_provider(jwks_uri=_JWKS_URI), None) is not None
    assert built.call_args.args[0] == _JWKS_URI


def test_factory_rejects_header_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    with pytest.raises(RuntimeError, match="oidc or accounts"):
        create_jwt_bearer_handler(UnifiedAuthProvider(source="header"), None)


def test_mount_logs_admin_principals(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer, caplog: pytest.LogCaptureFixture
) -> None:
    _handler(monkeypatch, issuer, admin_list=_FakeAdminList(_PRINCIPAL))
    assert any(
        "ci-triage" in r.getMessage() and "an admin" in r.getMessage() for r in caplog.records
    )


def test_mount_warns_on_shared_identities(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer, caplog: pytest.LogCaptureFixture
) -> None:
    _handler(
        monkeypatch,
        issuer,
        store=_FakeStore(users=(_PRINCIPAL,)),
        OMNIGENT_JWT_BEARER_CLIENTS=f"{_CLIENT}={_PRINCIPAL},{_OTHER_CLIENT}=bot@example.com",
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("existing account" in m and _PRINCIPAL in m for m in messages)
    assert any("allowed login domain" in m and "bot@example.com" in m for m in messages)


# ── Exchanges ─────────────────────────────────────────────────────


def test_exchange_issues_a_delegated_token(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    resp = _exchange(_client(monkeypatch, issuer), issuer.sign())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "sessions"
    assert resp.headers["cache-control"] == "no-store"
    claims = jwt.decode(body["access_token"], _COOKIE_SECRET, algorithms=["HS256"])
    assert claims["sub"] == _PRINCIPAL
    assert claims["scope"] == "sessions"
    assert claims["act"] == {"client_id": _CLIENT}
    assert "grant_id" not in claims


def test_each_client_acts_as_its_own_principal(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    resp = _exchange(_client(monkeypatch, issuer), issuer.sign(azp=_OTHER_CLIENT))
    assert resp.status_code == 200, resp.text
    claims = jwt.decode(resp.json()["access_token"], _COOKIE_SECRET, algorithms=["HS256"])
    assert claims["sub"] == _OTHER_PRINCIPAL


def test_issued_token_never_outlives_the_assertion(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    resp = _exchange(_client(monkeypatch, issuer), issuer.sign(lifetime=120))
    assert resp.status_code == 200, resp.text
    assert 0 < resp.json()["expires_in"] <= 120
    claims = jwt.decode(resp.json()["access_token"], _COOKIE_SECRET, algorithms=["HS256"])
    assert claims["exp"] <= int(time.time()) + 120


def test_issued_token_is_capped_by_the_configured_ttl(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    client = _client(monkeypatch, issuer, OMNIGENT_JWT_BEARER_TOKEN_TTL="60")
    resp = _exchange(client, issuer.sign(lifetime=3000))
    assert resp.status_code == 200, resp.text
    assert resp.json()["expires_in"] == 60


def test_client_claim_is_configurable(monkeypatch: pytest.MonkeyPatch, issuer: _Issuer) -> None:
    client = _client(monkeypatch, issuer, OMNIGENT_JWT_BEARER_CLIENT_CLAIM="client_id")
    assert _exchange(client, issuer.sign(azp="other", client_id=_CLIENT)).status_code == 200
    assert _exchange(client, issuer.sign(client_id="other")).status_code == 400


def test_missing_assertion_is_invalid_request(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    resp = _client(monkeypatch, issuer).post(
        "/oauth/token", data={"grant_type": JWT_BEARER_GRANT_TYPE}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def _hs256_with_public_key(issuer: _Issuer) -> str:
    """An HS256 token keyed with the issuer's public key: the algorithm-confusion attack."""
    public_pem = issuer.key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": issuer.kid}).encode())
    payload = b64(
        json.dumps(
            {"iss": _ISSUER, "aud": _AUDIENCE, "azp": _CLIENT, "iat": now, "exp": now + 60}
        ).encode()
    )
    signature = hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(signature)}"


def _unsigned(issuer: _Issuer) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": _ISSUER, "aud": _AUDIENCE, "azp": _CLIENT, "iat": now, "exp": now + 60},
        None,
        algorithm="none",
        headers={"kid": issuer.kid},
    )


@pytest.mark.parametrize(
    ("case", "build", "reason"),
    [
        ("wrong issuer", lambda i: i.sign(iss="https://evil.example.com"), "untrusted issuer"),
        ("wrong audience", lambda i: i.sign(aud="another-api"), "Audience"),
        ("expired", lambda i: i.sign(iat_offset=-600, lifetime=300), "expired"),
        ("issued in the future", lambda i: i.sign(iat_offset=600), "not yet valid"),
        ("missing exp", lambda i: i.sign(drop=("exp",)), '"exp"'),
        ("missing iat", lambda i: i.sign(drop=("iat",)), '"iat"'),
        ("missing aud", lambda i: i.sign(drop=("aud",)), '"aud"'),
        ("lifetime above the ceiling", lambda i: i.sign(lifetime=3601), "exceeds 3600s"),
        ("unlisted client", lambda i: i.sign(azp="someone-else"), "not an accepted client"),
        ("no client claim", lambda i: i.sign(drop=("azp",)), "not an accepted client"),
        (
            "email names a principal",
            lambda i: i.sign(azp="someone-else", email=_PRINCIPAL),
            "not an accepted client",
        ),
        (
            "sub names a principal",
            lambda i: i.sign(azp="someone-else", sub=_PRINCIPAL),
            "not an accepted client",
        ),
        ("unknown signing key", lambda i: i.sign(kid="key-2"), "no signing key"),
        ("foreign signing key", lambda i: i.sign(key=_Issuer().key), "Signature verification"),
        ("HS256 keyed with the public key", _hs256_with_public_key, "asymmetric"),
        ("alg none", _unsigned, "asymmetric"),
        ("garbage", lambda i: "not-a-jwt", "malformed"),
    ],
)
def test_bad_assertions_are_invalid_grant(
    monkeypatch: pytest.MonkeyPatch,
    issuer: _Issuer,
    caplog: pytest.LogCaptureFixture,
    case: str,
    build,
    reason: str,
) -> None:
    client = _client(monkeypatch, issuer)
    caplog.set_level("INFO", logger="omnigent.server.routes.jwt_bearer")
    resp = _exchange(client, build(issuer))
    assert resp.status_code == 400, case
    assert resp.json()["error"] == "invalid_grant", case
    assert "access_token" not in resp.text, case
    rejections = [r.getMessage() for r in caplog.records if "rejected assertion" in r.getMessage()]
    assert len(rejections) == 1 and reason in rejections[0], (case, rejections)


def test_key_algorithm_must_match_the_assertion(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    issuer.jwks["keys"][0]["alg"] = "RS512"
    assert _exchange(_client(monkeypatch, issuer), issuer.sign()).status_code == 400


def test_ec_keys_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    issuer = _Issuer()
    issuer.key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(issuer.key.public_key()))
    issuer.jwks = {"keys": [{**jwk, "kid": issuer.kid, "use": "sig", "alg": "ES256"}]}
    resp = _exchange(_client(monkeypatch, issuer), issuer.sign(alg="ES256"))
    assert resp.status_code == 200, resp.text


def test_unknown_key_ids_refresh_the_key_set_at_most_once_per_interval(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    jwks_client = _FakeJwksClient(issuer.jwks)
    client = _client(monkeypatch, issuer, jwks_client=jwks_client)
    assert _exchange(client, issuer.sign()).status_code == 200
    baseline = jwks_client.fetches
    for n in range(5):
        assert _exchange(client, issuer.sign(kid=f"forged-{n}")).status_code == 400
    assert jwks_client.fetches == baseline + 1


def test_rotated_key_is_picked_up(monkeypatch: pytest.MonkeyPatch, issuer: _Issuer) -> None:
    jwks_client = _FakeJwksClient(issuer.jwks)
    client = _client(monkeypatch, issuer, jwks_client=jwks_client)
    assert _exchange(client, issuer.sign()).status_code == 200
    rotated = _Issuer(kid="key-2")
    jwks_client.document = {"keys": issuer.jwks["keys"] + rotated.jwks["keys"]}
    assert _exchange(client, rotated.sign()).status_code == 200


def test_unreachable_key_set_is_invalid_grant(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    jwks_client = _FakeJwksClient(issuer.jwks)

    def fail() -> object:
        raise jwt.PyJWKClientConnectionError("down")

    jwks_client.fetch_data = fail  # type: ignore[method-assign]
    resp = _exchange(_client(monkeypatch, issuer, jwks_client=jwks_client), issuer.sign())
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


# ── Principal vetting ─────────────────────────────────────────────


def test_admin_flagged_principal_is_refused(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    client = _client(monkeypatch, issuer, store=_FakeStore(admins=(_PRINCIPAL,)))
    resp = _exchange(client, issuer.sign())
    assert resp.status_code == 400
    assert resp.json()["error"] == "unauthorized_client"
    assert _exchange(client, issuer.sign(azp=_OTHER_CLIENT)).status_code == 200


def test_admin_listed_principal_is_refused(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    client = _client(monkeypatch, issuer, admin_list=_FakeAdminList(_PRINCIPAL))
    assert _exchange(client, issuer.sign()).json()["error"] == "unauthorized_client"


def test_principal_promoted_after_mount_is_refused(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    store = _FakeStore()
    client = _client(monkeypatch, issuer, store=store)
    assert _exchange(client, issuer.sign()).status_code == 200
    store.admins.add(_PRINCIPAL)
    assert _exchange(client, issuer.sign()).json()["error"] == "unauthorized_client"


def test_unanswerable_admin_check_fails_closed(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    store = _FakeStore()
    client = _client(monkeypatch, issuer, store=store)

    def broken(user_id: str) -> bool:
        raise RuntimeError("store down")

    store.is_admin = broken  # type: ignore[method-assign]
    assert _exchange(client, issuer.sign()).json()["error"] == "unauthorized_client"


# ── Routing and throttle ──────────────────────────────────────────


def test_unconfigured_grant_is_an_unsupported_grant_type(issuer: _Issuer) -> None:
    app = FastAPI()
    app.include_router(create_oauth_token_router(_provider(), _FakeGrantStore()))  # type: ignore[arg-type]
    resp = _exchange(TestClient(app), issuer.sign())
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_grant_answers_under_the_device_grant_router(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    handler = _handler(monkeypatch, issuer)
    app = FastAPI()
    app.include_router(
        create_device_auth_router(
            _provider(),
            _FakeGrantStore(),  # type: ignore[arg-type]
            handle_jwt_bearer=handler,
        )
    )
    assert [r.path for r in app.routes].count("/oauth/token") == 1
    assert _exchange(TestClient(app), issuer.sign()).status_code == 200


def test_other_grant_types_still_dispatch(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    resp = _client(monkeypatch, issuer).post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "nope"}
    )
    assert resp.json()["error"] == "invalid_grant"


def test_rejected_assertions_do_not_spend_a_client_budget(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    client = _client(monkeypatch, issuer)
    forged = issuer.sign(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    for _ in range(jwt_bearer._TOKEN_RATE_MAX * 2):
        assert _exchange(client, "not-a-jwt").status_code == 400
        assert _exchange(client, forged).status_code == 400
    assert _exchange(client, issuer.sign()).status_code == 200


def test_exchanges_are_throttled_per_client(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    client = _client(monkeypatch, issuer)
    for _ in range(jwt_bearer._TOKEN_RATE_MAX):
        assert _exchange(client, issuer.sign()).status_code == 200
    resp = _exchange(client, issuer.sign())
    assert resp.status_code == 429
    assert resp.json()["error"] == "slow_down"
    assert resp.headers["Retry-After"] == str(jwt_bearer._TOKEN_RATE_WINDOW_SECONDS)
    assert _exchange(client, issuer.sign(azp=_OTHER_CLIENT)).status_code == 200


# ── Auth-layer treatment of the issued token ──────────────────────


def _req(path: str, token: str) -> MagicMock:
    request = MagicMock()
    request.cookies = {}
    request.headers = {"Authorization": f"Bearer {token}"}
    request.url.path = path
    return request


def test_exchanged_token_is_confined_and_never_cached(
    monkeypatch: pytest.MonkeyPatch, issuer: _Issuer
) -> None:
    token = _exchange(_client(monkeypatch, issuer), issuer.sign()).json()["access_token"]
    provider = _provider()
    assert provider._check_cookie(_req("/v1/sessions", token)) == _PRINCIPAL
    assert provider._check_cookie(_req("/v1/sessions/abc/events", token)) == _PRINCIPAL
    assert provider._check_cookie(_req("/v1/me", token)) is None
    assert provider._check_cookie(_req("/auth/users", token)) is None
    assert provider._cookie_cache == {}
    assert provider._check_cookie(_req("/v1/me", token)) is None


def test_assertion_itself_is_not_an_omnigent_credential(issuer: _Issuer) -> None:
    provider = _provider()
    assert provider._check_cookie(_req("/v1/sessions", issuer.sign())) is None
