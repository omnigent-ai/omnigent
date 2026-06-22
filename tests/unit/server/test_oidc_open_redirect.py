"""Regression tests for OIDC open redirect via ``return_to``.

The OIDC login flow accepts a caller-supplied ``return_to`` query param,
signs it into the short-lived state cookie, and after authentication
issues a server-side 302 to it. Without validation that is an open
redirect — ``/auth/login?return_to=https://evil.example`` would land the
user on an attacker page under the app's own domain.

These tests pin both halves of the fix in ``omnigent/server/routes/auth.py``:

1. **Ingest** (``/auth/login``): a malicious ``return_to`` is reduced to
   ``"/"`` *before* it is signed into the state cookie, so the cookie
   never carries a value the callback would trust. Same-origin relative
   paths (including deep links with query strings) round-trip unchanged.
2. **Egress** (``/auth/callback``): even when a state cookie carrying a
   malicious ``return_to`` is presented (a pre-fix cookie still in its
   5-minute window, or a forged-but-validly-signed one), the post-auth
   302 targets ``"/"``, not the attacker URL.

The callback path drives the real route; the external IdP token exchange
and id_token→email resolution are the only mocked boundaries.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import omnigent.server.routes.auth as auth_module
from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_TEST_SECRET = bytes.fromhex("aa" * 32)
# The plain (non-__Host-) state cookie name, used because the test
# config runs over http:// so secure_cookies is False.
_STATE_COOKIE = "ap_auth_state"


def _oidc_config() -> OIDCConfig:
    """Build an OIDCConfig over plain HTTP so TestClient cookies work.

    HTTP (not HTTPS) keeps ``secure_cookies`` False, which the
    ``TestClient`` needs to send the state cookie back on the callback
    request, and selects the plain ``ap_auth_state`` cookie name.

    :returns: A generic-OIDC config pointing at Google's endpoints (no
        network is touched — the token exchange is mocked in tests).
    """
    return OIDCConfig(
        issuer="https://accounts.google.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,  # empty allowlist == admit any IdP user
        provider_type="oidc",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=False,
    )


@pytest.fixture
def oidc_client(tmp_path: Path, db_uri: str) -> Iterator[TestClient]:
    """An OIDC auth router (invites off) mounted on a TestClient.

    :param tmp_path: Pytest temp dir for the (empty) admin-list file.
    :param db_uri: Shared SQLite URI for the permission store.
    :yields: A ``TestClient`` with ``/auth/login`` and ``/auth/callback``
        mounted under ``/auth``.
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    config = _oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, AdminList(admins)),
        prefix="/auth",
    )
    with TestClient(app) as client:
        yield client


def _decode_state_cookie(client: TestClient) -> dict[str, Any]:
    """Decode the state cookie the last ``/auth/login`` response set.

    :param client: The test client whose cookie jar holds the state
        cookie set by a preceding ``/auth/login`` call.
    :returns: The verified state-cookie claims (``state``,
        ``code_verifier``, ``return_to``, ``exp``).
    """
    raw = client.cookies.get(_STATE_COOKIE)
    assert raw is not None, "login did not set the auth state cookie"
    return jwt.decode(raw, _TEST_SECRET, algorithms=["HS256"])


# ── Ingest: /auth/login sanitizes before signing into the cookie ──


@pytest.mark.parametrize(
    "malicious",
    [
        "https://evil.example",  # absolute cross-origin URL (the PoC)
        "http://evil.example/path",  # absolute, explicit scheme
        "//evil.example",  # protocol-relative — browsers treat as cross-origin
        "/\\evil.example",  # backslash trick some browsers normalize to //
        "javascript:alert(1)",  # scheme with no leading slash
        "",  # empty string
    ],
)
def test_login_sanitizes_malicious_return_to(oidc_client: TestClient, malicious: str) -> None:
    """A non-same-origin ``return_to`` is stored as ``"/"`` in the cookie.

    Asserts on the decoded cookie payload (not just status) so we prove
    the attacker value never reaches the signed state the callback
    trusts — sanitization happens at ingest, before signing.
    """
    resp = oidc_client.get("/auth/login", params={"return_to": malicious}, follow_redirects=False)
    assert resp.status_code == 302
    claims = _decode_state_cookie(oidc_client)
    assert claims["return_to"] == "/", (
        f"malicious return_to {malicious!r} was signed into the cookie as "
        f"{claims['return_to']!r}; expected it reduced to '/'"
    )


@pytest.mark.parametrize(
    "safe",
    [
        "/",
        "/sessions",
        "/sessions/abc123",
        "/sessions/abc123?tab=files",  # deep link with query string preserved
        "/search?q=a&sort=desc#frag",  # query + fragment preserved
    ],
)
def test_login_preserves_same_origin_return_to(oidc_client: TestClient, safe: str) -> None:
    """A same-origin relative path (incl. query/fragment) survives intact.

    Guards against an over-aggressive fix that would break legitimate
    deep-link returns produced by ``identity.ts``.
    """
    resp = oidc_client.get("/auth/login", params={"return_to": safe}, follow_redirects=False)
    assert resp.status_code == 302
    claims = _decode_state_cookie(oidc_client)
    assert claims["return_to"] == safe


def test_login_absent_return_to_defaults_to_root(oidc_client: TestClient) -> None:
    """Omitting ``return_to`` entirely stores the ``"/"`` default."""
    resp = oidc_client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    claims = _decode_state_cookie(oidc_client)
    assert claims["return_to"] == "/"


# ── Egress: /auth/callback re-sanitizes a hostile/pre-fix cookie ──


def _mint_state_cookie(*, state: str, return_to: str) -> str:
    """Forge a validly-signed state cookie carrying ``return_to``.

    Models a state cookie minted before the ingest fix shipped (or a
    tampering attempt that somehow produced a valid HS256 signature) —
    the callback must still not honor a hostile ``return_to``.

    :param state: The CSRF ``state`` value; must match the ``state``
        query param presented to ``/auth/callback``.
    :param return_to: The (hostile) post-auth redirect target to embed,
        e.g. ``"https://evil.example"``.
    :returns: An HS256 JWT signed with the test cookie secret.
    """
    payload = {
        "state": state,
        "code_verifier": "v" * 43,
        "return_to": return_to,
        "exp": int(time.time()) + 300,
    }
    return jwt.encode(payload, _TEST_SECRET, algorithm="HS256")


class _FakeResponse:
    """Minimal stand-in for an ``httpx.Response`` from the token endpoint."""

    status_code = 200

    def json(self) -> dict[str, str]:
        """Return a token-endpoint body with an opaque id_token.

        :returns: ``{"id_token": "..."}`` — the value is irrelevant
            because email resolution is mocked.
        """
        return {"id_token": "stub", "access_token": "stub"}


class _FakeAsyncClient:
    """Async-context-manager stub replacing ``httpx.AsyncClient``.

    Returns a canned 200 from the token endpoint so the callback can
    proceed to email resolution (itself mocked) without any network.
    """

    async def __aenter__(self) -> _FakeAsyncClient:
        """Enter the async context, yielding self."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the async context; nothing to clean up."""
        return

    async def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        """Stub the token-exchange POST with a canned 200 response."""
        return _FakeResponse()


@pytest.mark.parametrize("malicious", ["https://evil.example", "//evil.example"])
def test_callback_redirects_to_root_for_hostile_cookie(
    oidc_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    malicious: str,
) -> None:
    """The post-auth 302 targets ``"/"`` even when the cookie is hostile.

    Defense-in-depth: presents a validly-signed state cookie whose
    ``return_to`` is an attacker URL (a pre-fix cookie or forgery), drives
    the real callback, and asserts the redirect lands on ``"/"`` with a
    session cookie set — proving the open redirect is closed at egress
    too, not only at ingest.
    """
    state = "csrf-state-token"
    # Mock the two external-IdP boundaries: token exchange + email.
    # Rebind on the auth module's own namespace (contained, auto-undone)
    # rather than walking through the global httpx module.
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth_module, "_resolve_oidc_email", lambda token_json, config: "u@x.com")

    oidc_client.cookies.set(_STATE_COOKIE, _mint_state_cookie(state=state, return_to=malicious))
    resp = oidc_client.get(
        "/auth/callback",
        params={"code": "authcode", "state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/", (
        f"callback honored hostile return_to {malicious!r}: Location={resp.headers['location']!r}"
    )
    # The login still succeeded — a session cookie was issued.
    assert any(
        c.strip().startswith(f"{_oidc_config().session_cookie_name}=")
        for c in resp.headers.get_list("set-cookie")
    )


def test_callback_preserves_safe_cookie_return_to(
    oidc_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-origin ``return_to`` in the cookie is honored at callback."""
    state = "csrf-state-token"
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth_module, "_resolve_oidc_email", lambda token_json, config: "u@x.com")

    oidc_client.cookies.set(
        _STATE_COOKIE, _mint_state_cookie(state=state, return_to="/sessions/abc?tab=files")
    )
    resp = oidc_client.get(
        "/auth/callback",
        params={"code": "authcode", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/sessions/abc?tab=files"
