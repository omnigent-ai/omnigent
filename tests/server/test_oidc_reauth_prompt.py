"""Forced re-authentication survives the handoff to an OIDC IdP.

The device-grant consent page refuses to render for a session that predates
the grant being approved, and bounces through the login page with
``?reauth=1``. That is the anti-phishing gate: approving a device grant must
cost a deliberate credential entry, so a victim handed a one-click link cannot
bind an attacker's grant by reflex.

In accounts mode the SPA login form enforces it — it holds back its
auto-redirect and demands a password. OIDC has no form to hold back; the IdP
owns the credential. The only way to ask is the OIDC ``prompt`` parameter, so
``/auth/login`` must forward ``reauth=1`` as ``prompt=login``.

Without that forwarding the flow still *works* — the IdP satisfies the bounce
from its own session, returns a token, and the callback mints a session whose
fresh ``iat`` clears the consent gate. Nothing errors, no test fails, and the
gate silently approves a user who proved nothing. These tests pin the
parameter so that regression cannot pass quietly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_TEST_SECRET = bytes.fromhex("aa" * 32)


def _oidc_config() -> OIDCConfig:
    """An OIDC config over plain HTTP so TestClient handles cookies."""
    return OIDCConfig(
        issuer="https://accounts.google.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=False,
    )


@pytest.fixture
def oidc_client(tmp_path: Path, db_uri: str) -> Iterator[TestClient]:
    """An OIDC auth router mounted on a TestClient, redirects not followed."""
    perm_store = SqlAlchemyPermissionStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    provider = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config())

    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, AdminList(admins)),
        prefix="/auth",
    )
    with TestClient(app) as client:
        yield client


def _authorize_params(client: TestClient, query: str) -> dict[str, list[str]]:
    """Follow ``/auth/login`` one hop and return the IdP authorize params."""
    res = client.get(f"/auth/login{query}", follow_redirects=False)
    assert res.status_code == 302, res.status_code
    return parse_qs(urlparse(res.headers["location"]).query)


def test_reauth_forwards_prompt_login_to_the_idp(oidc_client: TestClient) -> None:
    """``?reauth=1`` must reach the IdP as ``prompt=login``.

    This is the whole gate under OIDC. Drop the parameter and the IdP
    re-authenticates the user silently from its own session.
    """
    params = _authorize_params(oidc_client, "?reauth=1&return_to=/oauth/device")

    assert params.get("prompt") == ["login"]
    # `prompt` alone is only a request. `max_age=0` is what obliges a
    # conforming IdP to return `auth_time`, which is what the callback then
    # verifies — drop it and the gate degrades to asking politely.
    assert params.get("max_age") == ["0"]
    # The rest of the request must be unchanged — PKCE and state still apply.
    assert params["code_challenge_method"] == ["S256"]
    assert params["response_type"] == ["code"]
    assert params["code_challenge"] and params["state"]


def test_an_ordinary_login_does_not_re_prompt(oidc_client: TestClient) -> None:
    """No ``reauth`` ⇒ no ``prompt``.

    Sending ``prompt=login`` unconditionally would force a password entry on
    every single sign-in, which is how a security control ends up switched
    off by whoever finds it annoying.
    """
    for query in ("", "?return_to=/sessions"):
        params = _authorize_params(oidc_client, query)
        assert "prompt" not in params
        assert "max_age" not in params


@pytest.mark.parametrize("raw", ["0", "true", "yes", "", "1 ", "TRUE"])
def test_only_an_exact_1_forces_re_authentication(oidc_client: TestClient, raw: str) -> None:
    """Anything other than exactly ``1`` is not a re-auth request.

    Matched strictly because the consent page is the only caller and it sends
    exactly ``1``; accepting loose truthy spellings would let an unrelated
    query param turn on a re-prompt nobody asked for.
    """
    assert "prompt" not in _authorize_params(oidc_client, f"?reauth={raw}")


def test_re_authentication_still_round_trips_the_return_to(oidc_client: TestClient) -> None:
    """The consent URL must survive the re-auth bounce.

    Losing it would land the user on the dashboard after re-entering their
    password, with the pending grant abandoned and no way back to it.
    """
    import jwt

    # Percent-encoded exactly as `_bounce_to_login` emits it. Decoding the
    # stored value before asserting would have passed under either encoding
    # and hidden whether the query survived at all.
    oidc_client.get(
        "/auth/login?reauth=1&return_to=%2Foauth%2Fdevice%3Fuser_code%3DK7M2-QP9X",
        follow_redirects=False,
    )
    cookie = oidc_client.cookies.get("ap_auth_state")
    assert cookie is not None
    claims = jwt.decode(cookie, _TEST_SECRET, algorithms=["HS256"])

    assert claims["return_to"] == "/oauth/device?user_code=K7M2-QP9X"


# ── The consent page against a REAL OIDCConfig ────────────────────


def test_consent_page_renders_for_a_real_oidc_session_cookie(tmp_path: Path) -> None:
    """A genuine OIDC session must satisfy the consent page's own cookie read.

    ``_session_iat`` reads ``cookie_config.session_cookie_name`` and verifies
    with ``cookie_config.cookie_secret``. If either diverged from what
    ``/auth/callback`` actually sets, it would return ``None`` on every
    request and the consent page would bounce forever — a login loop with no
    error and no failing test. A hand-built ``SimpleNamespace`` cannot catch
    that; this drives the real config and the real minting helper.
    """
    import time

    from fastapi import FastAPI

    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.oidc import mint_session_cookie
    from omnigent.server.routes.device_auth import create_device_auth_router

    config = _oidc_config()
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    store = DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")

    app = FastAPI()
    app.include_router(create_device_auth_router(provider, store))

    with TestClient(app) as client:
        res = client.post("/oauth/device/authorize", json={"client_id": "polly"})
        assert res.status_code == 200, res.text
        user_code = res.json()["user_code"]

        # A session with no proven authentication — the shape an ordinary
        # OIDC login produces when the IdP reuses its own session.
        unproven = mint_session_cookie("alice@example.com", config.cookie_secret, 8, "oidc")
        client.cookies.set(config.session_cookie_name, unproven)
        bounced = client.get(f"/oauth/device?user_code={user_code}", follow_redirects=False)

        # An authentication proven AFTER the grant began, exactly as the
        # forced bounce produces.
        time.sleep(1)
        proven = mint_session_cookie(
            "alice@example.com",
            config.cookie_secret,
            8,
            "oidc",
            auth_time=int(time.time()),
        )
        client.cookies.set(config.session_cookie_name, proven)
        page = client.get(f"/oauth/device?user_code={user_code}", follow_redirects=False)

    assert bounced.status_code == 302, "an unproven session must not reach consent"
    # `/auth/login`, not the accounts SPA's `/login` — the other half of
    # `login_url`, which a `"/login" in location` substring cannot tell apart.
    assert bounced.headers["location"].startswith("/auth/login?"), bounced.headers["location"]
    assert "reauth=1" in bounced.headers["location"]

    assert page.status_code == 200, f"consent bounced instead of rendering: {page.headers}"
    assert "alice@example.com" in page.text, "the consent screen must name the identity"
    assert "polly" in page.text, "and the client asking for access"


def test_reauth_is_signed_into_the_state_cookie(oidc_client: TestClient) -> None:
    """`/auth/login` must record the demand where the callback can trust it.

    The callback refuses a login that failed to re-authenticate only when
    the state carries ``reauth_at``. Signed into the cookie rather than read
    back off the URL, so it cannot be stripped by editing the redirect.
    """
    import jwt

    oidc_client.get("/auth/login?reauth=1&return_to=/oauth/device", follow_redirects=False)
    claims = jwt.decode(oidc_client.cookies["ap_auth_state"], _TEST_SECRET, algorithms=["HS256"])
    assert isinstance(claims.get("reauth_at"), int)


def test_an_ordinary_login_signs_no_reauth_marker(oidc_client: TestClient) -> None:
    """Without it the callback must not demand proof of every sign-in."""
    import jwt

    oidc_client.get("/auth/login?return_to=/sessions", follow_redirects=False)
    claims = jwt.decode(oidc_client.cookies["ap_auth_state"], _TEST_SECRET, algorithms=["HS256"])
    assert "reauth_at" not in claims
