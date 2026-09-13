"""A minimal, real HTTP OIDC identity provider for e2e login-flow recordings.

The OIDC login journey can't be filmed today because an unauthenticated
Playwright navigation is bounced to the deployment's *real* SSO provider. This
stands up a fake IdP the recorder can drive headlessly instead: a genuine HTTP
server (not an in-process mock) so a spawned ``omnigent server`` subprocess can
reach it over the network, exactly as it would a real issuer.

It serves the four things stock OIDC mode expects (see
``omnigent/server/oidc.py::OIDCConfig.from_env`` and
``routes/auth.py::_resolve_oidc_email``), so **no product code changes**:

- ``GET /.well-known/openid-configuration`` — discovery doc naming the
  authorize/token/jwks endpoints (fetched at server boot).
- ``GET /authorize`` — the user-facing sign-in page: a single "Continue as
  <email>" link that 302s back to the server's ``/auth/callback`` with the
  ``code`` + ``state`` echoed. This is the frame the recording captures.
- ``POST /token`` — exchanges the code for a response carrying an
  **RS256-signed ``id_token``** (``iss`` = issuer, ``aud`` = client_id,
  ``email`` + ``email_verified: true``).
- ``GET /jwks`` — the RSA public key the server verifies that ``id_token``
  against.

The signing keypair is generated per-instance, so the token the server
verifies is genuinely signed by the key the JWKS advertises — a real crypto
round-trip, not a bypass.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from urllib.parse import quote

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from tests.e2e_ui.conftest import _find_free_port

# The email the fake IdP asserts. Admission is unrestricted by default (empty
# allowlist admits any IdP user), so any address works; a stable one keeps the
# recorded journey legible.
FAKE_IDP_EMAIL = "e2e-user@example.test"
_KID = "e2e-fake-idp-key"


@dataclass
class FakeIdP:
    """A running fake OIDC IdP.

    :param issuer: The issuer URL (also the discovery base) — feed this to
        ``OMNIGENT_OIDC_ISSUER``.
    :param client_id: The client id the server must present (and that the
        signed ``id_token`` carries as its audience).
    :param client_secret: The client secret the server presents at ``/token``.
    :param email: The email the signed ``id_token`` asserts.
    """

    issuer: str
    client_id: str
    client_secret: str
    email: str


def _build_app(issuer: str, client_id: str, email: str, private_pem: bytes) -> FastAPI:
    app = FastAPI()

    @app.get("/.well-known/openid-configuration")
    async def discovery() -> JSONResponse:
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
                "userinfo_endpoint": f"{issuer}/userinfo",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        )

    @app.get("/jwks")
    async def jwks() -> JSONResponse:
        # Advertise the RSA public key the id_token is signed with, as a JWK.
        from cryptography.hazmat.primitives import serialization

        private_key = serialization.load_pem_private_key(private_pem, password=None)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        jwk["kid"] = _KID
        jwk["use"] = "sig"
        jwk["alg"] = "RS256"
        return JSONResponse({"keys": [jwk]})

    @app.get("/authorize")
    async def authorize(redirect_uri: str, state: str) -> HTMLResponse:
        # The user-facing sign-in page. One click continues the OAuth flow by
        # bouncing back to the server's callback with a fixed code + the state.
        # redirect_uri/state are request query params, so escape them before
        # reflecting into HTML: URL-encode into the href, HTML-escape for text.
        continue_url = (
            f"{quote(redirect_uri, safe=':/?#[]@!$&()*+,;=')}"
            f"?code=fake-auth-code&state={quote(state, safe='')}"
        )
        safe_href = escape(continue_url)
        safe_email = escape(email)
        return HTMLResponse(
            "<!doctype html><html><head><title>Fake IdP Sign-in</title></head>"
            "<body style='font-family:sans-serif;padding:2rem'>"
            "<h1>Fake IdP</h1>"
            f"<p>Sign in as <b>{safe_email}</b> to continue.</p>"
            f"<a id='fake-idp-continue' href='{safe_href}'>Continue as {safe_email}</a>"
            "</body></html>"
        )

    @app.post("/token")
    async def token() -> JSONResponse:
        # Any code is accepted (PKCE code_verifier/challenge is intentionally
        # NOT validated — the goal is filming the journey, so this lane does not
        # exercise/guard the server's PKCE handling). Return an RS256-signed
        # id_token the server verifies against /jwks. iss/aud/email must match
        # what the callback checks (issuer, client_id, verified email).
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": issuer,
                "aud": client_id,
                "sub": "fake-idp-subject",
                "email": email,
                "email_verified": True,
                "iat": now,
                "exp": now + 300,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": _KID},
        )
        return JSONResponse(
            {
                "access_token": "fake-access-token",
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": id_token,
            }
        )

    return app


@contextmanager
def fake_idp(
    *, client_id: str = "e2e-client", client_secret: str = "e2e-secret"
) -> Iterator[FakeIdP]:
    """Run a fake OIDC IdP on a background thread; yield its handle.

    The issuer is a plain-``http`` loopback URL so the spawned server can fetch
    discovery and JWKS without TLS. Generates a fresh RSA keypair per instance.
    """
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    port = _find_free_port()
    issuer = f"http://127.0.0.1:{port}"
    app = _build_app(issuer, client_id, FAKE_IDP_EMAIL, private_pem)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        # Wait for the discovery endpoint to answer before yielding, so a
        # consumer that boots a server against this issuer won't race the boot.
        import httpx

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=1.0)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"fake IdP did not come up on {issuer}")

        yield FakeIdP(
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            email=FAKE_IDP_EMAIL,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
