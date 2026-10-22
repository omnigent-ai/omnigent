"""A minimal, real HTTP OIDC identity provider for e2e login-flow recordings.

The OIDC login journey can't be filmed today because an unauthenticated
Playwright navigation is bounced to the deployment's *real* SSO provider. This
stands up a fake IdP the recorder can drive headlessly instead: a genuine HTTP
server (not an in-process mock) so a spawned ``omnigent server`` subprocess can
reach it over the network, exactly as it would a real issuer.

It serves the endpoints stock OIDC mode expects (see
``omnigent/server/oidc.py::OIDCConfig.from_env`` and
``routes/auth.py::_resolve_oidc_email``). Explicit-endpoint mode disables
discovery and can use an issuer on a different host:

- ``GET /.well-known/openid-configuration`` — discovery doc naming the
  authorize/token/jwks endpoints (fetched at server boot).
- ``GET /authorize`` — the user-facing sign-in page: a single "Continue as
  <email>" link that 302s back to the server's ``/auth/callback`` with the
  ``code`` + ``state`` echoed. This is the frame the recording captures.
- ``POST /token`` — exchanges the code for a response carrying an
  **RS256- or PS256-signed ``id_token``** (``iss`` = issuer, ``aud`` = client_id,
  ``email`` + ``email_verified: true``).
- ``GET /jwks`` — the RSA public key the server verifies that ``id_token``
  against.

The signing keypair is generated per-instance, so the token the server
verifies is genuinely signed by the key the JWKS advertises — a real crypto
round-trip, not a bypass.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qs, quote

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from omnigent.server.oidc import derive_code_challenge
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
    :param base_url: Reachable HTTP address of the test provider.
    :param endpoint_prefix: Optional path prefix for explicit endpoint testing.
    """

    issuer: str
    client_id: str
    client_secret: str
    email: str
    base_url: str
    endpoint_prefix: str = ""


def _build_app(
    issuer: str,
    client_id: str,
    email: str,
    private_pem: bytes,
    *,
    client_secret: str,
    signing_algorithm: str,
    endpoint_prefix: str,
) -> FastAPI:
    app = FastAPI()
    codes: dict[str, tuple[str, str]] = {}

    @app.get("/.well-known/openid-configuration")
    async def discovery() -> JSONResponse:
        if endpoint_prefix:
            return JSONResponse({"error": "discovery unavailable"}, status_code=404)
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
                "userinfo_endpoint": f"{issuer}/userinfo",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": [signing_algorithm],
            }
        )

    @app.get(f"{endpoint_prefix}/jwks")
    async def jwks() -> JSONResponse:
        # Advertise the RSA public key the id_token is signed with, as a JWK.
        from cryptography.hazmat.primitives import serialization

        private_key = serialization.load_pem_private_key(private_pem, password=None)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        jwk["kid"] = _KID
        jwk["use"] = "sig"
        jwk["alg"] = signing_algorithm
        return JSONResponse({"keys": [jwk]})

    @app.get(f"{endpoint_prefix}/authorize")
    async def authorize(
        request: Request,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> HTMLResponse:
        if request.query_params.get("client_id") != client_id or code_challenge_method != "S256":
            return HTMLResponse("Invalid authorization request", status_code=400)
        if endpoint_prefix and request.query_params.get("p") != "policy":
            return HTMLResponse("Missing provider policy", status_code=400)
        code = secrets.token_urlsafe(32)
        codes[code] = (code_challenge, redirect_uri)
        continue_url = (
            f"{quote(redirect_uri, safe=':/?#[]@!$&()*+,;=')}"
            f"?code={code}&state={quote(state, safe='')}"
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

    @app.post(f"{endpoint_prefix}/token")
    async def token(request: Request) -> JSONResponse:
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        if (
            form.get("client_id") != [client_id]
            or (client_secret and form.get("client_secret") != [client_secret])
            or (not client_secret and "client_secret" in form)
        ):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        code = form.get("code", [""])[0]
        issued = codes.pop(code, None)
        verifier = form.get("code_verifier", [""])[0]
        if (
            issued is None
            or not verifier
            or not verifier.isascii()
            or derive_code_challenge(verifier) != issued[0]
            or form.get("redirect_uri") != [issued[1]]
            or form.get("grant_type") != ["authorization_code"]
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": issuer,
                "aud": client_id,
                "sub": "fake-idp-subject",
                "email": email,
                "email_verified": True,
                "iat": now,
                "auth_time": now,
                "exp": now + 300,
            },
            private_pem,
            algorithm=signing_algorithm,
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
    *,
    client_id: str = "e2e-client",
    client_secret: str = "e2e-secret",
    signing_algorithm: str = "RS256",
    endpoint_prefix: str = "",
    canonical_issuer: str | None = None,
) -> Iterator[FakeIdP]:
    """Run a fake OIDC IdP on a background thread; yield its handle.

    Endpoints use plain HTTP loopback. An optional canonical issuer models
    providers whose reachable endpoints live on a different host. Authorization
    codes are single-use and bound to an S256 challenge and redirect URI.

    :param client_id: Expected OAuth client ID.
    :param client_secret: Expected secret, or empty for a public client.
    :param signing_algorithm: JWT signing algorithm (RS256 or PS256).
    :param endpoint_prefix: Endpoint path prefix; disables discovery when set.
    :param canonical_issuer: Optional separate issuer asserted in ID tokens.
    :yields: The running provider's connection details.
    """
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    issuer = canonical_issuer or base_url
    app = _build_app(
        issuer,
        client_id,
        FAKE_IDP_EMAIL,
        private_pem,
        client_secret=client_secret,
        signing_algorithm=signing_algorithm,
        endpoint_prefix=endpoint_prefix,
    )

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
                r = httpx.get(f"{base_url}{endpoint_prefix}/jwks", timeout=1.0)
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
            base_url=base_url,
            endpoint_prefix=endpoint_prefix,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
