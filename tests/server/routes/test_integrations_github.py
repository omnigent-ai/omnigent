"""Tests for the GitHub App integration routes.

Builds a minimal FastAPI app with the integration router, a header-based
auth provider, and a fake GitHub client so the connect → callback →
status → disconnect flow is exercised end-to-end without the network.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from omnigent.connections.github import GithubConnectionStore
from omnigent.errors import OmnigentError
from omnigent.server.github_app import GitHubAppConfig, GitHubTokenSet
from omnigent.server.routes.connections_base import sanitize_return_to
from omnigent.server.routes.connections_github import (
    _derive_state_signing_key,
    create_connections_github_router,
)


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64
        import json

        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64
        import json

        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


class _HeaderAuth:
    """Auth provider reading the user id from ``X-Test-User``."""

    def get_user_id(self, request: object) -> str | None:
        return getattr(request, "headers", {}).get("x-test-user")


class _FakeClient:
    """Stand-in for :class:`GitHubAppClient`."""

    def __init__(self) -> None:
        self.exchanged: list[str] = []

    async def exchange_code(self, code: str) -> GitHubTokenSet:
        self.exchanged.append(code)
        return GitHubTokenSet(
            access_token="ghu_new",
            refresh_token="ghr_new",
            expires_at=None,
            refresh_token_expires_at=None,
            scopes="repo",
        )

    async def fetch_login(self, access_token: str) -> tuple[str, int]:
        return "octocat", 42


def _config() -> GitHubAppConfig:
    return GitHubAppConfig(
        app_id=None,
        client_id="Iv1abc",
        client_secret="shh",
        private_key=None,
        redirect_uri="https://x/v1/connections/github/callback",
        slug="omni-app",
    )


def _app(db_uri: str) -> tuple[TestClient, GithubConnectionStore, GitHubAppConfig, _FakeClient]:
    config = _config()
    # The store's cipher key is the credential store's own (OMNIGENT_CREDENTIAL_ENC_KEY),
    # independent of the GitHub App config.
    store = GithubConnectionStore(db_uri, SecretBox("store-enc-key"))
    client = _FakeClient()
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_connections_github_router(
            config, store, auth_provider=_HeaderAuth(), client=client
        ),
        prefix="/v1",
    )
    # TestClient must not chase the external GitHub redirect.
    return TestClient(app, follow_redirects=False), store, config, client


_USER = {"X-Test-User": "alice@example.com"}


def test_status_unconnected(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    resp = tc.get("/v1/connections/github/status", headers=_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["connected"] is False
    assert body["login"] is None
    assert body["install_url"] == "https://github.com/apps/omni-app/installations/new"


def test_status_requires_auth(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    # No X-Test-User header → require_user raises 401.
    resp = tc.get("/v1/connections/github/status")
    assert resp.status_code == 401


def test_connect_redirects_to_github_with_signed_state(db_uri: str) -> None:
    tc, _store, config, _client = _app(db_uri)
    resp = tc.get(
        "/v1/connections/github/connect", params={"return_to": "/settings"}, headers=_USER
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    # Pull the state back out and verify it is signed + bound to the user.
    state = location.split("state=", 1)[1].split("&", 1)[0]
    claims = jwt.decode(
        state, _derive_state_signing_key(config.client_secret), algorithms=["HS256"]
    )
    assert claims["sub"] == "alice@example.com"
    assert claims["return_to"] == "/settings"


def test_callback_stores_connection_and_redirects(db_uri: str) -> None:
    tc, store, config, client = _app(db_uri)
    state = jwt.encode(
        {"sub": "alice@example.com", "return_to": "/settings", "nonce": "n", "exp": 9999999999},
        _derive_state_signing_key(config.client_secret),
        algorithm="HS256",
    )
    resp = tc.get(
        "/v1/connections/github/callback",
        params={"code": "abc", "state": state},
        headers=_USER,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings?github=connected"
    assert client.exchanged == ["abc"]
    conn = store.get("alice@example.com", with_tokens=True)
    assert conn is not None
    assert conn.github_login == "octocat"
    assert conn.access_token == "ghu_new"


def test_callback_rejects_state_user_mismatch(db_uri: str) -> None:
    tc, store, config, _client = _app(db_uri)
    # State was signed for someone else — must not bind to alice.
    state = jwt.encode(
        {"sub": "mallory@example.com", "return_to": "/settings", "nonce": "n", "exp": 9999999999},
        _derive_state_signing_key(config.client_secret),
        algorithm="HS256",
    )
    resp = tc.get(
        "/v1/connections/github/callback",
        params={"code": "abc", "state": state},
        headers=_USER,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings?github=error"
    assert store.get("alice@example.com") is None


def test_callback_rejects_bad_state(db_uri: str) -> None:
    tc, _store, _config, _client = _app(db_uri)
    resp = tc.get(
        "/v1/connections/github/callback",
        params={"code": "abc", "state": "garbage"},
        headers=_USER,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings?github=error"


def test_callback_rejects_state_signed_with_raw_client_secret(db_uri: str) -> None:
    # State is signed with a subkey DERIVED from the client secret, not the raw
    # secret — so a token forged with the raw secret (a different key) fails.
    tc, store, config, _client = _app(db_uri)
    state = jwt.encode(
        {"sub": "alice@example.com", "return_to": "/settings", "nonce": "n", "exp": 9999999999},
        config.client_secret,
        algorithm="HS256",
    )
    resp = tc.get(
        "/v1/connections/github/callback",
        params={"code": "abc", "state": state},
        headers=_USER,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/settings?github=error"
    assert store.get("alice@example.com") is None


def test_disconnect(db_uri: str) -> None:
    tc, store, _config, _client = _app(db_uri)
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_a", "ghr_a", None, None, "repo"),
    )
    resp = tc.post("/v1/connections/github/disconnect", headers=_USER)
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True}
    assert store.get("alice@example.com") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/settings", "/settings"),
        ("/settings?tab=integrations", "/settings?tab=integrations"),
        (None, "/settings"),
        ("", "/settings"),
        ("https://evil.com", "/settings"),
        ("//evil.com", "/settings"),
        ("/\\evil.com", "/settings"),  # backslash → browser reads protocol-relative
        ("/\t//evil.com", "/settings"),  # decoded control char
        ("/\x7fevil", "/settings"),
    ],
)
def test_sanitize_return_to_blocks_off_origin(raw: str | None, expected: str) -> None:
    assert sanitize_return_to(raw) == expected
