"""Generic OIDC public clients, endpoint overrides, and PS256 verification."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig, derive_code_challenge
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_ISSUER = "https://issuer.example.test"
_ENDPOINTS = {
    "authorization_endpoint": "https://login.example.test/custom/authorize",
    "token_endpoint": "https://tokens.example.test/custom/token",
    "jwks_uri": "https://keys.example.test/custom/jwks",
}


@pytest.fixture
def oidc_env(monkeypatch):
    """Isolate operator configuration from the developer's login environment."""
    for name in os.environ:
        if name.startswith("OMNIGENT_OIDC_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "http://testserver/auth/callback")
    monkeypatch.setenv("OMNIGENT_OIDC_COOKIE_SECRET", "aa" * 32)
    monkeypatch.setenv("OMNIGENT_OIDC_TOKEN_ENDPOINT_AUTH_METHOD", "none")
    for name, endpoint in _ENDPOINTS.items():
        monkeypatch.setenv(f"OMNIGENT_OIDC_{name.upper()}", endpoint)


@pytest.mark.usefixtures("oidc_env")
def test_public_client_with_explicit_endpoints_skips_discovery(monkeypatch):
    """A fully configured provider needs neither discovery nor a client secret."""

    def unexpected_discovery(*args, **kwargs):
        pytest.fail("Explicit endpoints must not contact discovery")

    monkeypatch.setattr(httpx, "get", unexpected_discovery)
    config = OIDCConfig.from_env()
    assert config.client_secret == ""
    assert config.issuer == _ISSUER
    for name, endpoint in _ENDPOINTS.items():
        assert getattr(config, name) == endpoint


@pytest.mark.usefixtures("oidc_env")
@pytest.mark.parametrize("auth_method", ["none", "client_secret_post"])
def test_discovery_remains_available(monkeypatch, auth_method):
    """Both client types can keep using standard discovery."""
    monkeypatch.setenv("OMNIGENT_OIDC_TOKEN_ENDPOINT_AUTH_METHOD", auth_method)
    if auth_method == "client_secret_post":
        monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "secret")
    for name in _ENDPOINTS:
        monkeypatch.setenv(f"OMNIGENT_OIDC_{name.upper()}", "")
    requested = []

    def discovery(url, **kwargs):
        requested.append(url)
        return httpx.Response(200, json=_ENDPOINTS, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", discovery)
    assert OIDCConfig.from_env().token_endpoint == _ENDPOINTS["token_endpoint"]
    assert requested == [f"{_ISSUER}/.well-known/openid-configuration"]


@pytest.mark.usefixtures("oidc_env")
@pytest.mark.parametrize("auth_method", [None, "", "client_secret_post"])
def test_confidential_client_still_requires_secret(monkeypatch, auth_method):
    """A missing secret never silently selects public-client authentication."""
    if auth_method is None:
        monkeypatch.delenv("OMNIGENT_OIDC_TOKEN_ENDPOINT_AUTH_METHOD")
    else:
        monkeypatch.setenv("OMNIGENT_OIDC_TOKEN_ENDPOINT_AUTH_METHOD", auth_method)
    with pytest.raises(RuntimeError, match="OMNIGENT_OIDC_CLIENT_SECRET"):
        OIDCConfig.from_env()


@pytest.mark.usefixtures("oidc_env")
@pytest.mark.parametrize(
    ("setting", "value", "error"),
    [
        ("TOKEN_ENDPOINT_AUTH_METHOD", "unsupported", "client_secret_post or none"),
        ("CLIENT_SECRET", "unexpected-secret", "Unset OMNIGENT_OIDC_CLIENT_SECRET"),
        ("ISSUER", "https://github.com", "GitHub login requires"),
        ("TOKEN_ENDPOINT", "", "Set all three"),
        ("AUTHORIZATION_ENDPOINT", "", "Set all three"),
        ("JWKS_URI", "", "Set all three"),
    ],
)
def test_invalid_provider_configuration_fails_at_startup(monkeypatch, setting, value, error):
    """Reject ambiguous authentication modes and incomplete manual configuration."""
    monkeypatch.setenv(f"OMNIGENT_OIDC_{setting}", value)
    with pytest.raises(RuntimeError, match=error):
        OIDCConfig.from_env()


@pytest.mark.usefixtures("oidc_env")
@pytest.mark.parametrize("endpoint_name", list(_ENDPOINTS))
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://remote.example.test/token",
        "/relative",
        "https://user:secret@example.test/token",
        "https://example.test/token#fragment",
        "file:///tmp/key",
        "https://example.test:bad/token",
        "https://example.test:0/token",
        "https://example.test:99999/token",
        "https://exam\nple.test/token",
        "https://[broken/token",
    ],
)
def test_endpoint_overrides_reject_unsafe_urls(monkeypatch, endpoint_name, endpoint):
    """Manual endpoints must be transport-safe URLs, not user-controlled redirects."""
    monkeypatch.setenv(f"OMNIGENT_OIDC_{endpoint_name.upper()}", endpoint)
    with pytest.raises(RuntimeError, match=f"OMNIGENT_OIDC_{endpoint_name.upper()}"):
        OIDCConfig.from_env()


@pytest.mark.usefixtures("oidc_env")
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_http_endpoints_support_local_testing(monkeypatch, host):
    """Permit a local test IdP without weakening remote transport requirements."""
    for name in _ENDPOINTS:
        monkeypatch.setenv(f"OMNIGENT_OIDC_{name.upper()}", f"http://{host}:8080/{name}")
    assert OIDCConfig.from_env().token_endpoint == f"http://{host}:8080/token_endpoint"


@pytest.mark.usefixtures("oidc_env")
def test_github_configuration_keeps_fixed_endpoints(monkeypatch):
    """OIDC overrides must not redirect GitHub's credential exchange elsewhere."""
    monkeypatch.setenv("OMNIGENT_OIDC_ISSUER", "https://github.com")
    monkeypatch.setenv("OMNIGENT_OIDC_TOKEN_ENDPOINT_AUTH_METHOD", "client_secret_post")
    monkeypatch.setenv("OMNIGENT_OIDC_CLIENT_SECRET", "secret")
    with pytest.raises(RuntimeError, match="overrides are not supported for GitHub"):
        OIDCConfig.from_env()
    for name in _ENDPOINTS:
        monkeypatch.delenv(f"OMNIGENT_OIDC_{name.upper()}")
    config = OIDCConfig.from_env()
    assert config.token_endpoint == "https://github.com/login/oauth/access_token"
    assert config.client_secret == "secret"


@pytest.fixture
def provider_client(oidc_env, monkeypatch, tmp_path: Path, db_uri):
    """Use real signed tokens and a strict code/PKCE exchange at the IdP boundary."""
    config = OIDCConfig.from_env()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-key", "alg": "PS256", "use": "sig"})
    codes = {}
    claims = {}
    exchanges = []
    jwks_requests = []

    async def token_exchange(self, url, *, data, **kwargs):
        exchanges.append(data)
        assert url == config.token_endpoint
        assert "client_secret" not in data
        assert data["client_id"] == config.client_id
        assert data["redirect_uri"] == config.redirect_uri
        assert data["grant_type"] == "authorization_code"
        challenge = codes.pop(data["code"], None)
        if not challenge or derive_code_challenge(data["code_verifier"]) != challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        payload = {
            "iss": config.issuer,
            "aud": config.client_id,
            "sub": "test-user",
            "email": "alice@example.test",
            "email_verified": True,
            "iat": now,
            "exp": now + 300,
            "auth_time": now,
            **claims,
        }
        token = jwt.encode(payload, private_key, algorithm="PS256", headers={"kid": "test-key"})
        return httpx.Response(200, json={"id_token": token})

    def fetch_jwks(client):
        jwks_requests.append(client.uri)
        return {"keys": [jwk]}

    monkeypatch.setattr(httpx.AsyncClient, "post", token_exchange)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_jwks)
    admins = tmp_path / "admins"
    admins.write_text("")
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    app = FastAPI()
    app.include_router(
        create_auth_router(provider, SqlAlchemyPermissionStore(db_uri), AdminList(admins)),
        prefix="/auth",
    )
    with TestClient(app) as client:
        yield client, codes, claims, exchanges, jwks_requests


def begin_login(client, codes, params=None):
    """Capture the real login's S256 challenge and issue one matching authorization code."""
    response = client.get("/auth/login", params=params, follow_redirects=False)
    assert response.status_code == 302
    redirect = urlsplit(response.headers["location"])
    assert (
        f"{redirect.scheme}://{redirect.netloc}{redirect.path}"
        == _ENDPOINTS["authorization_endpoint"]
    )
    query = parse_qs(redirect.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == ["test-client"]
    code = secrets.token_urlsafe(24)
    codes[code] = query["code_challenge"][0]
    return {"code": code, "state": query["state"][0]}


def test_public_pkce_ps256_login_and_code_replay(provider_client):
    """Authenticate through custom endpoints, then reject reuse of the same code."""
    client, codes, _, exchanges, jwks_requests = provider_client
    params = begin_login(client, codes)
    state_cookies = dict(client.cookies)
    response = client.get("/auth/callback", params=params, follow_redirects=False)
    assert response.status_code == 302, response.text
    assert response.cookies.get("ap_session")
    assert len(exchanges) == 1
    assert jwks_requests == [_ENDPOINTS["jwks_uri"]]
    client.cookies.clear()
    client.cookies.update(state_cookies)
    replay = client.get("/auth/callback", params=params, follow_redirects=False)
    assert replay.status_code == 400
    assert not replay.cookies.get("ap_session")


@pytest.mark.parametrize("failure", ["pkce", "state", "issuer", "audience", "expired", "email"])
def test_public_login_rejects_invalid_proofs(provider_client, failure):
    """Public-client compatibility preserves callback and identity validation gates."""
    client, codes, claims, exchanges, _ = provider_client
    params = begin_login(client, codes)
    if failure == "pkce":
        codes[params["code"]] = "incorrect-challenge"
    elif failure == "state":
        params["state"] = "wrong-state"
    elif failure == "issuer":
        claims["iss"] = "https://other-issuer.example.test"
    elif failure == "audience":
        claims["aud"] = "different-client"
    elif failure == "expired":
        claims["exp"] = 1
    else:
        claims["email_verified"] = False
    response = client.get("/auth/callback", params=params, follow_redirects=False)
    assert response.status_code == 400
    assert not response.cookies.get("ap_session")
    if failure == "state":
        assert exchanges == []


def test_public_pkce_login_fulfills_cli_ticket(provider_client):
    """CLI login uses the same public-client exchange and validated identity token."""
    client, codes, _, _, _ = provider_client
    ticket = client.post("/auth/cli-login").json()
    params = begin_login(client, codes, {"ticket": ticket["ticket"]})
    response = client.get("/auth/callback", params=params, follow_redirects=False)
    assert response.status_code == 200, response.text
    poll = client.get("/auth/cli-poll", params={"ticket": ticket["ticket"]})
    assert poll.status_code == 200, poll.text
    assert poll.json()["token"]
