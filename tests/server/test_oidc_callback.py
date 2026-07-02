"""Tests for the generic-OIDC ``/auth/callback`` email resolution gate.

These drive the *real* callback route end-to-end for a non-GitHub
(generic OIDC) provider: a genuinely RS256-signed ``id_token`` is fed
through the production ``jwt.decode`` path (real signature + ``iss`` /
``aud`` / ``exp`` verification), and the only thing the tests vary is
the ``email_verified`` claim.

This is the regression coverage for "OIDC login accepts
unverified email claim (account takeover)". Before the fix the
callback minted a session for any signature-valid ``id_token``,
ignoring ``email_verified``; an IdP that lets a user assert an
arbitrary unverified email could be used to sign in as a victim in an
allowed domain.

The token endpoint (``httpx``) and the JWKS signing-key lookup are the
only mocked boundaries — everything between the HTTP request and the
minted session cookie is the production code path.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import _AUTH_STATE_COOKIE_PLAIN, create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_TEST_SECRET = bytes.fromhex("aa" * 32)
_ISSUER = "https://accounts.google.com"
_CLIENT_ID = "cid"


def _oidc_config(skip_email_verification: bool = False) -> OIDCConfig:
    """Build a generic-OIDC config over plain HTTP (so TestClient cookies stick).

    ``allowed_domains=None`` means admit-all, so the test isolates the
    ``email_verified`` gate from the domain-allowlist check.

    :param skip_email_verification: Waive the ``email_verified`` gate,
        as ``OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION`` would.
    """
    return OIDCConfig(
        issuer=_ISSUER,
        client_id=_CLIENT_ID,
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint=f"{_ISSUER}/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=False,
        skip_email_verification=skip_email_verification,
    )


class _IdpKeys:
    """An RSA keypair plus the JWKS signing key derived from its public half.

    :param private_key: The RSA private key used to sign test
        ``id_token`` JWTs.
    :param signing_key: A :class:`jwt.PyJWK` wrapping the public key,
        shaped exactly like what ``PyJWKClient.get_signing_key_from_jwt``
        returns — its ``.key`` is consumed by the production
        ``jwt.decode`` call for real signature verification.
    """

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk_dict = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk_dict["alg"] = "RS256"
        self.signing_key = jwt.PyJWK.from_dict(jwk_dict)

    def sign_id_token(self, claims: dict[str, object]) -> str:
        """Sign ``claims`` into an RS256 ``id_token``, filling iss/aud/exp.

        :param claims: Claims to embed, e.g.
            ``{"email": "alice@example.com", "email_verified": True}``.
            ``iss``/``aud``/``exp``/``iat`` are added if absent.
        :returns: A compact-serialized signed JWT string.
        """
        now = int(time.time())
        payload: dict[str, object] = {
            "iss": _ISSUER,
            "aud": _CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "sub": "idp-subject-123",
            **claims,
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")


@pytest.fixture
def callback_client(
    tmp_path: Path,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, _IdpKeys]]:
    """Mount the OIDC router and stub the IdP token endpoint + JWKS lookup.

    The token endpoint is driven per-test by mutating the mutable
    one-element ``pending_id_token`` list captured by the monkeypatched
    ``post`` — exposed on ``app.state.pending_id_token`` so ``_do_callback``
    can set the signed token the IdP should return.

    Indirect parametrization (``request.param``, default ``False``) sets
    the config's ``skip_email_verification`` flag.
    """
    keys = _IdpKeys()
    perm_store = SqlAlchemyPermissionStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")

    config = _oidc_config(skip_email_verification=getattr(request, "param", False))
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)

    # The signed id_token the mocked token endpoint will return. Each
    # test sets this before calling /auth/callback.
    pending_id_token: list[str] = [""]

    async def _fake_post(
        self: httpx.AsyncClient,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Stand in for the IdP token endpoint, returning the test's id_token."""
        return httpx.Response(200, json={"id_token": pending_id_token[0]})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    # The production code constructs PyJWKClient(jwks_uri) then calls
    # get_signing_key_from_jwt; return our public key so the real
    # jwt.decode performs genuine signature verification offline.
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, token: keys.signing_key,
    )

    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, AdminList(admins)),
        prefix="/auth",
    )
    app.state.pending_id_token = pending_id_token

    with TestClient(app) as client:
        yield client, keys


def _do_callback(client: TestClient, id_token: str) -> httpx.Response:
    """Drive a full ``/auth/callback`` with a valid state cookie.

    Crafts the signed state cookie the way ``/auth/login`` would, sets
    the matching ``state`` query param, and stashes ``id_token`` for the
    mocked token endpoint to return. Redirects are not followed so the
    302 (and its ``Set-Cookie``) is observable.

    :param client: The TestClient mounting the OIDC router.
    :param id_token: The signed ``id_token`` the IdP should return.
    :returns: The raw callback response.
    """
    client.app.state.pending_id_token[0] = id_token
    state = "state-token-xyz"
    state_jwt = jwt.encode(
        {
            "state": state,
            "code_verifier": "verifier",
            "return_to": "/",
            "exp": int(time.time()) + 300,
        },
        _TEST_SECRET,
        algorithm="HS256",
    )
    client.cookies.set(_AUTH_STATE_COOKIE_PLAIN, state_jwt)
    return client.get(
        f"/auth/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )


def test_callback_verified_email_mints_session(
    callback_client: tuple[TestClient, _IdpKeys],
) -> None:
    """A signed id_token with ``email_verified=true`` logs the user in.

    Proves the golden path still works after the fix: 302 back to the
    app and a session cookie whose ``sub`` is the verified email
    (normalized to lowercase).
    """
    client, keys = callback_client
    token = keys.sign_id_token({"email": "Alice@Example.com", "email_verified": True})

    resp = _do_callback(client, token)

    # 302 redirect (not 400) means the email was accepted as identity.
    assert resp.status_code == 302, resp.text
    session_cookie = resp.cookies.get("ap_session")
    # The session cookie must be set on success; absence would mean the
    # callback bailed before minting (the bug we're guarding against,
    # inverted).
    assert session_cookie is not None
    decoded = jwt.decode(session_cookie, _TEST_SECRET, algorithms=["HS256"])
    # sub is the normalized (lowercased) verified email — proves the
    # decoded claim flowed all the way into the minted session.
    assert decoded["sub"] == "alice@example.com"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"email": "victim@example.com", "email_verified": False}, id="false"),
        pytest.param({"email": "victim@example.com", "email_verified": "false"}, id="str-false"),
        pytest.param({"email": "victim@example.com"}, id="absent"),
        pytest.param({"email": "victim@example.com", "email_verified": None}, id="null"),
        pytest.param({"email": "victim@example.com", "email_verified": 1}, id="int-one"),
    ],
)
def test_callback_unverified_email_rejected(
    callback_client: tuple[TestClient, _IdpKeys],
    claims: dict[str, object],
) -> None:
    """An unverified/absent ``email_verified`` claim is rejected.

    The id_token is genuinely signature-valid (same RSA key as the
    happy path), so a failure here is *exclusively* the missing
    verification gate, not a signature/iss/aud rejection. Before the
    fix every one of these minted a session for ``victim@example.com``.
    """
    client, keys = callback_client
    token = keys.sign_id_token(claims)

    resp = _do_callback(client, token)

    # 400 (not 302): the callback refused to treat an unverified email
    # as identity. Anything else means the gate let it through.
    assert resp.status_code == 400, resp.text
    assert "Could not determine user email" in resp.json()["error"]
    # No session was minted for the spoofable email.
    assert resp.cookies.get("ap_session") is None


@pytest.mark.parametrize("callback_client", [True], indirect=True)
@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"email": "carol@example.com"}, id="absent"),
        pytest.param({"email": "carol@example.com", "email_verified": False}, id="false"),
    ],
)
def test_callback_skip_verification_flag_admits_unverified(
    callback_client: tuple[TestClient, _IdpKeys],
    claims: dict[str, object],
) -> None:
    """With ``skip_email_verification`` on, the gate is waived.

    Models Okta tiers that drop ``email_verified`` for
    directory-provisioned users: the same absent-claim token rejected
    by default (covered above) mints a session when the operator has
    opted out via ``OMNIGENT_OIDC_SKIP_EMAIL_VERIFICATION``.
    """
    client, keys = callback_client
    token = keys.sign_id_token(claims)

    resp = _do_callback(client, token)

    assert resp.status_code == 302, resp.text
    session_cookie = resp.cookies.get("ap_session")
    assert session_cookie is not None
    decoded = jwt.decode(session_cookie, _TEST_SECRET, algorithms=["HS256"])
    assert decoded["sub"] == "carol@example.com"


@pytest.mark.parametrize("verified_value", [True, "true", "True", "TRUE"])
def test_callback_accepts_boolean_and_string_true(
    callback_client: tuple[TestClient, _IdpKeys],
    verified_value: object,
) -> None:
    """Both boolean ``true`` and the string ``"true"`` are accepted.

    OIDC Core §5.1 notes ``email_verified`` may arrive as a string;
    accepting ``"true"`` keeps spec-compliant-but-string IdPs working
    while still rejecting ``"false"`` / absent (covered above).
    """
    client, keys = callback_client
    token = keys.sign_id_token({"email": "bob@example.com", "email_verified": verified_value})

    resp = _do_callback(client, token)

    # Accepted as a verified identity → redirect + session.
    assert resp.status_code == 302, resp.text
    assert resp.cookies.get("ap_session") is not None
