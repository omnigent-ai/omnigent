"""Databricks U2M OAuth client (authorization code + PKCE).

The bot registers a **custom OAuth app** in the Databricks workspace and drives
its user-to-machine (U2M) authorization-code flow, with PKCE (RFC 7636) and
``offline_access``, so each Slack user signs in once and the bot receives a
**durable, refreshable** access token it forwards to the Omnigent server.

This replaces the earlier forwarded-``x-forwarded-access-token`` pass-through,
whose ~1h token had no refresh (users re-enrolled hourly — see the "Weaknesses"
section of ``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``).

Endpoints (workspace-level):

- ``GET  {host}/oidc/v1/authorize`` — the browser lands here; the user signs in.
- ``POST {host}/oidc/v1/token``     — code→token exchange and refresh rotation.

The token/refresh calls authenticate the client with HTTP Basic
(``client_id:client_secret``); PKCE binds the code to the verifier the bot
generated, so an intercepted code is useless without it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

# Databricks issues the user's email in the id_token (openid scope) and via the
# SCIM "me" endpoint; we read the former and fall back to the latter.
_SCIM_ME_PATH = "/api/2.0/preview/scim/v2/Me"


class DatabricksOAuthError(RuntimeError):
    """A Databricks OAuth step failed (authorize/exchange/refresh)."""


class DatabricksAuthExpiredError(DatabricksOAuthError):
    """The grant is permanently dead — the token/refresh_token was rejected.

    Distinct from a transient failure (network blip, 5xx): only this warrants
    dropping the stored token and re-prompting sign-in. A transient
    :class:`DatabricksOAuthError` must NOT discard a still-valid refresh grant.
    The ``grant_expired`` marker lets the AuthManager rotator distinguish the two
    without importing this module.
    """

    grant_expired = True


# ── PKCE helpers (RFC 7636) ──────────────────────────────────────────


def generate_code_verifier() -> str:
    """Generate a PKCE code verifier.

    ``token_urlsafe(64)`` yields ~86 URL-safe chars, well within RFC 7636's
    43–128 range, so no truncation is needed.
    """
    return secrets.token_urlsafe(64)


def derive_code_challenge(code_verifier: str) -> str:
    """Derive the S256 code challenge (base64url SHA-256, unpadded)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class DatabricksTokens:
    """A token set from a completed code exchange or refresh.

    ``email`` is the authenticated user's identity, read from the id_token (or
    resolved via SCIM); the callback matches it against the requesting Slack
    user to close the confused-deputy. Empty on a refresh (no fresh id_token
    is needed there — identity was fixed at enrollment).
    """

    access_token: str
    refresh_token: str
    expires_in: int
    email: str


class DatabricksOAuthClient:
    """U2M OAuth client for one workspace's custom OAuth app.

    :param workspace_host: Workspace base URL (``https://<ws>.databricks.com``).
    :param client_id: Custom OAuth app client id (public).
    :param client_secret: Custom OAuth app client secret.
    :param scopes: Space-separated scopes (already normalized to include
        ``openid`` + ``offline_access`` by the caller).
    :param redirect_uri: The registered redirect URI (the bot's
        ``/auth/callback``); required to build the authorize URL and to exchange
        a code, but not to refresh.
    """

    def __init__(
        self,
        workspace_host: str,
        *,
        client_id: str,
        client_secret: str,
        scopes: str,
        redirect_uri: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._host = workspace_host.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._redirect_uri = redirect_uri
        self._timeout = timeout

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        """Build the ``/oidc/v1/authorize`` URL the browser is sent to.

        :param state: Opaque signed value round-tripped back to the callback
            (binds the redirect to the Slack user + PKCE verifier).
        :param code_challenge: S256 challenge from :func:`derive_code_challenge`.
        """
        if not self._redirect_uri:
            raise DatabricksOAuthError("No redirect URI configured for the OAuth flow.")
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": self._scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self._host}/oidc/v1/authorize?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> DatabricksTokens:
        """Exchange an authorization code for tokens (PKCE code_verifier)."""
        if not self._redirect_uri:
            raise DatabricksOAuthError("No redirect URI configured for the OAuth flow.")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "code_verifier": code_verifier,
        }
        return await self._token_request(data, want_email=True)

    async def refresh(self, refresh_token: str) -> DatabricksTokens:
        """Exchange a refresh token for a fresh access + refresh pair."""
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        return await self._token_request(data, want_email=False)

    async def revoke(self, token: str) -> None:
        """Best-effort revoke a token at ``/oidc/v1/revoke``.

        The custom OAuth app may not expose revocation; a failure is swallowed —
        logout still deletes the local copy. Revoking the refresh token (when the
        endpoint is present) means logout actually cuts off the grant, unlike the
        old refreshless flow where the ~1h token stayed live until it expired.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            with contextlib.suppress(httpx.HTTPError):
                await client.post(
                    f"{self._host}/oidc/v1/revoke",
                    data={"token": token},
                    auth=(self._client_id, self._client_secret),
                )

    async def _token_request(self, data: dict[str, str], *, want_email: bool) -> DatabricksTokens:
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            try:
                resp = await client.post(
                    f"{self._host}/oidc/v1/token",
                    data=data,
                    auth=(self._client_id, self._client_secret),
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                # Transport failure — transient. Do NOT treat as a dead grant.
                raise DatabricksOAuthError(f"Token request failed: {exc}") from exc
            if resp.status_code != 200:
                detail = _error_detail(resp)
                # A 4xx with an OAuth error code means the grant is permanently
                # rejected (invalid/expired refresh token, revoked). A 5xx or
                # other status is transient — must not discard a valid grant.
                if 400 <= resp.status_code < 500 and _is_invalid_grant(detail):
                    raise DatabricksAuthExpiredError(
                        f"Grant rejected (HTTP {resp.status_code}): {detail}"
                    )
                raise DatabricksOAuthError(
                    f"Token request rejected (HTTP {resp.status_code}): {detail}"
                )
            try:
                body = resp.json()
                access_token = str(body["access_token"])
                refresh_token = str(body.get("refresh_token", ""))
                expires_in = int(body.get("expires_in", 3600))
                id_token = body.get("id_token")
            except (ValueError, KeyError, TypeError) as exc:
                raise DatabricksOAuthError(f"Malformed token response: {exc}") from exc
            email = ""
            if want_email:
                email = _email_from_id_token(id_token) or await self._email_via_scim(
                    client, access_token
                )
        return DatabricksTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            email=email,
        )

    async def _email_via_scim(self, client: httpx.AsyncClient, access_token: str) -> str:
        """Resolve the user's email from SCIM Me when no id_token email is present."""
        try:
            resp = await client.get(
                f"{self._host}{_SCIM_ME_PATH}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError:
            return ""
        if resp.status_code != 200:
            return ""
        try:
            body = resp.json()
        except ValueError:
            return ""
        return _email_from_scim(body)


# OAuth token-endpoint error code (RFC 6749 §5.2) that means the USER's grant is
# permanently rejected — the refresh token is invalid/expired/revoked, so a fresh
# sign-in is required and retrying won't help. Deliberately ONLY ``invalid_grant``:
# ``invalid_client`` / ``unauthorized_client`` describe the bot's shared OAuth app
# (e.g. a rotated/misconfigured client secret), not any user's grant. Treating
# those as terminal would delete EVERY enrolled user's refresh token on one
# recoverable bot-side misconfig — a mass logout the transient/terminal split
# exists to prevent — and the deletion wouldn't even help (re-enrollment also
# fails until the secret is fixed). So they stay transient: keep the token, retry.
_TERMINAL_GRANT_ERRORS = frozenset({"invalid_grant"})


def _error_detail(resp: httpx.Response) -> str:
    """A short, non-secret error string from a failed token response."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        error = body.get("error") or body.get("error_description")
        if isinstance(error, str):
            return error
    return "unknown error"


def _is_invalid_grant(detail: str) -> bool:
    """Whether an error detail names a terminal (grant-dead) OAuth error."""
    return detail in _TERMINAL_GRANT_ERRORS


def _email_from_id_token(id_token: object) -> str:
    """Read the ``email`` claim from an id_token JWT without verifying it.

    The token is delivered over TLS directly from the workspace token endpoint
    (not via the browser), so its integrity is already assured by transport —
    we only need the claim, not a re-verification. Returns "" if absent/malformed.

    LOAD-BEARING: this skips JWKS signature verification purely because the
    transport is HTTPS. ``Settings._check_databricks_config`` enforces https on
    the workspace host (and every dependent URL) to hold that up — if that
    enforcement is ever relaxed, add real JWKS verification here first.
    """
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return ""
    import json

    payload_b64 = id_token.split(".")[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, TypeError):
        return ""
    email = payload.get("email") if isinstance(payload, dict) else None
    return str(email) if isinstance(email, str) and email else ""


def _email_from_scim(body: object) -> str:
    """Pull the primary (or first) email from a SCIM Me response."""
    if not isinstance(body, dict):
        return ""
    emails = body.get("emails")
    if not isinstance(emails, list):
        return ""
    primary = ""
    first = ""
    for entry in emails:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if not isinstance(value, str) or not value:
            continue
        first = first or value
        if entry.get("primary") is True:
            primary = value
            break
    return primary or first
