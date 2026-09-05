"""Databricks OAuth (U2M) configuration and credential handling.

Config half of the *Connect Databricks* integration: it lets a signed-in user
authorize their Databricks workspace (OAuth U2M authorization-code + PKCE) so
their managed sandboxes reach the Databricks **AI Gateway** MCP as them. Mirrors
:mod:`omnigent.server.github_app`. See ``designs/DATABRICKS_CONNECT.md``.

This module owns everything that *touches the app's secret* — reading it from
env, building the OAuth form fields, deriving the PKCE verifier — but makes **no
network calls**; the HTTP client lives in
:mod:`omnigent.server.databricks_app_client`.

Two Databricks-specific twists vs GitHub:

* **Per-workspace host.** Databricks is multi-workspace, so the OAuth endpoints
  are workspace-relative (``https://<workspace-host>/oidc/v1/{authorize,token}``)
  and the workspace host is supplied by the user at connect time, carried in the
  signed state, and stored in the connection metadata.
* **PKCE.** The ``code_verifier`` is derived deterministically from
  ``(client_secret, nonce)`` via :func:`derive_pkce`, so only the ``nonce`` need
  ride in the (signed, short-TTL) state — the verifier itself never travels in
  the browser and is recomputed at the callback.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

# Workspace-relative OAuth endpoints (OIDC). Joined onto the per-connection
# workspace host, which is validated to be an https origin before use.
_AUTHORIZE_PATH = "/oidc/v1/authorize"
_TOKEN_PATH = "/oidc/v1/token"

#: Default scopes. ``all-apis`` covers the AI Gateway MCP surface; ``offline_access``
#: is required to receive a refresh token (server-side refresh, like GitHub).
_DEFAULT_SCOPES = "all-apis offline_access"


@dataclass(frozen=True)
class DatabricksTokenSet:
    """Result of a code exchange or refresh.

    :param access_token: The Databricks user OAuth access token.
    :param refresh_token: The refresh token (present when ``offline_access`` was
        granted), or ``None``.
    :param expires_at: Unix epoch seconds the access token expires at, or ``None``.
    :param refresh_token_expires_at: Unix epoch seconds the refresh token expires
        at, or ``None`` (Databricks does not always report this).
    :param scopes: Space-separated granted scopes reported by the token endpoint.
    """

    access_token: str
    refresh_token: str | None
    expires_at: int | None
    refresh_token_expires_at: int | None
    scopes: str


@dataclass(frozen=True)
class DatabricksConfig:
    """Validated Databricks OAuth (custom app integration) configuration.

    Built once at startup via :meth:`from_env`; ``None`` when unconfigured (the
    feature stays dormant and the connect UI is hidden). The client secret is
    required — it both authenticates the confidential app at the token endpoint
    and signs the OAuth state / derives the PKCE verifier.

    :param client_id: The account custom-app-integration id (OAuth client id).
    :param client_secret: The app's client secret (confidential app).
    :param redirect_uri: OAuth callback URL registered on the app.
    :param scopes: Space-separated scopes to request.
    """

    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str

    def code_exchange_fields(self, code: str, *, code_verifier: str) -> dict[str, str]:
        """Form fields for exchanging an authorization code for tokens (PKCE)."""
        return {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code_verifier": code_verifier,
        }

    def token_refresh_fields(self, refresh_token: str) -> dict[str, str]:
        """Form fields for refreshing an access token."""
        return {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scopes,
        }

    @staticmethod
    def from_env() -> DatabricksConfig | None:
        """Build config from ``OMNIGENT_DATABRICKS_*`` env, or ``None``.

        Requires a client id, a client secret, and a resolvable redirect URI
        (explicit ``OMNIGENT_DATABRICKS_REDIRECT_URI`` or derived from
        ``OMNIGENT_DOMAIN``). Missing any of these disables the feature.
        """
        client_id = os.environ.get("OMNIGENT_DATABRICKS_CLIENT_ID", "").strip()
        client_secret = os.environ.get("OMNIGENT_DATABRICKS_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            return None

        redirect_uri = os.environ.get("OMNIGENT_DATABRICKS_REDIRECT_URI", "").strip()
        if not redirect_uri:
            domain = os.environ.get("OMNIGENT_DOMAIN", "").strip()
            if not domain:
                _logger.warning(
                    "Databricks client id/secret are set but neither "
                    "OMNIGENT_DATABRICKS_REDIRECT_URI nor OMNIGENT_DOMAIN is — "
                    "Databricks integration stays disabled."
                )
                return None
            redirect_uri = f"https://{domain}/v1/connections/databricks/callback"

        scopes = os.environ.get("OMNIGENT_DATABRICKS_SCOPES", "").strip() or _DEFAULT_SCOPES
        return DatabricksConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )


def _b64url(raw: bytes) -> str:
    """URL-safe base64 without padding (the OAuth/PKCE encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def derive_pkce(signing_secret: str, nonce: str) -> tuple[str, str]:
    """Deterministically derive ``(code_verifier, code_challenge)`` from a nonce.

    The verifier is ``base64url(SHA256(secret:nonce))`` (43 chars, all in the
    PKCE charset) and the challenge is ``base64url(SHA256(verifier))``
    (``S256``). Because the verifier depends on the app's client secret, only the
    ``nonce`` needs to travel in the signed state — the callback recomputes the
    verifier, so it never appears in the browser. Not hand-rolled crypto: SHA-256
    + base64url per RFC 7636.
    """
    verifier = _b64url(hashlib.sha256(f"{signing_secret}:{nonce}".encode()).digest())
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def normalize_workspace_host(raw: str) -> str | None:
    """Return a clean ``https://host`` origin for a user-supplied workspace, or None.

    Accepts ``host``, ``https://host``, or ``https://host/path``; rejects
    non-https schemes and anything without a hostname. The result has no trailing
    slash so the OIDC paths join cleanly.
    """
    import re
    from urllib.parse import urlparse

    value = (raw or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    # Require https and a hostname[:port] netloc (no spaces, credentials, or junk
    # — a Databricks workspace host is a plain DNS name).
    if parsed.scheme != "https" or not re.fullmatch(
        r"[A-Za-z0-9.\-]+(?::\d+)?", parsed.netloc or ""
    ):
        return None
    return f"https://{parsed.netloc}"


def authorize_url(
    config: DatabricksConfig, *, workspace_host: str, state: str, code_challenge: str
) -> str:
    """Build the workspace authorization URL to redirect the user to."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": config.scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{workspace_host}{_AUTHORIZE_PATH}?{urlencode(params)}"


def token_url(workspace_host: str) -> str:
    """The workspace token endpoint."""
    return f"{workspace_host}{_TOKEN_PATH}"


def token_set_from_payload(payload: dict) -> DatabricksTokenSet:
    """Parse a token-endpoint JSON payload into a :class:`DatabricksTokenSet`."""
    if "error" in payload:
        detail = payload.get("error_description", payload["error"])
        raise DatabricksAppError(f"Databricks token exchange failed: {detail}")
    access = payload.get("access_token")
    if not access:
        raise DatabricksAppError("Databricks token response missing access_token")
    now = int(time.time())
    expires_in = payload.get("expires_in")
    refresh_expires_in = payload.get("refresh_token_expires_in")
    return DatabricksTokenSet(
        access_token=str(access),
        refresh_token=payload.get("refresh_token") or None,
        expires_at=now + int(expires_in) if expires_in else None,
        refresh_token_expires_at=(now + int(refresh_expires_in) if refresh_expires_in else None),
        scopes=str(payload.get("scope", "")),
    )


class DatabricksAppError(Exception):
    """Raised when a Databricks OAuth interaction fails."""
