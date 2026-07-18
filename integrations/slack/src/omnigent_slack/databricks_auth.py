"""Databricks Apps web-auth: enroll a Slack user against a proxy-fronted server.

When the Omnigent server is deployed as a Databricks App it runs in header
mode: the Databricks Apps proxy authenticates every request and injects the
caller's identity, and the server mints no token of its own. A Socket-Mode
event carries no such proxy-authenticated request, so the bot's device-grant /
OIDC-ticket flows (see :mod:`omnigent_slack.oauth`) can't be driven — the probe
never reaches Omnigent past the proxy.

This module implements the alternative from
``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``: the bot is itself a Databricks App
with **user authorization** enabled, serving a small enrollment page. A Slack
user opening that page makes a browser request through the bot's *own* proxy,
which authenticates them and forwards their access token in
``x-forwarded-access-token``. The page then performs an **audience-scoped OAuth
token exchange** for the target Omnigent app: the exchanged token is scoped to
that one app and can't call other Databricks APIs, so storing it per user is
least-privilege — a stolen store leaks "can talk to Omnigent as this user", not
a workspace-wide credential.

Two pieces live here, both transport-agnostic (the aiohttp wiring is in
:mod:`omnigent_slack.webauth`):

- :func:`sign_state` / :func:`verify_state` — bind a browser enrollment session
  to the Slack ``(team, user)`` that requested it, so nobody can enroll another
  user's identity. Signed (HMAC-SHA256), single-use via a short TTL.
- :func:`exchange_token` — the RFC 8693 token exchange against the workspace
  ``/oidc/v1/token`` endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

import httpx

_logger = logging.getLogger(__name__)

_TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

# The forwarded-user header the Databricks Apps proxy injects on an
# authenticated browser request (lowercased; HTTP headers are case-insensitive).
FORWARDED_ACCESS_TOKEN_HEADER = "x-forwarded-access-token"
FORWARDED_EMAIL_HEADER = "x-forwarded-email"

# How long a signed enrollment ``state`` stays valid. Short: the user clicks the
# link and completes SSO within seconds, so a tight window bounds replay.
_DEFAULT_STATE_TTL_SECONDS = 600


class StateError(RuntimeError):
    """A ``state`` token was malformed, tampered with, or expired."""


class TokenExchangeError(RuntimeError):
    """The workspace rejected or failed the token exchange."""


@dataclass(frozen=True, slots=True)
class EnrollmentState:
    """The Slack identity a browser enrollment session is bound to."""

    team_id: str
    user_id: str
    issued_at: int


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()


def sign_state(team_id: str, user_id: str, secret: str, *, issued_at: int | None = None) -> str:
    """Return a signed, URL-safe ``state`` binding a browser session to Slack IDs.

    The payload carries the ``(team_id, user_id)`` and an issue time; the
    signature (HMAC-SHA256 over the payload) makes it unforgeable without the
    secret. :func:`verify_state` checks the signature and TTL. ``issued_at`` is
    injectable for tests; production stamps ``time.time()``.
    """
    issued = int(issued_at if issued_at is not None else time.time())
    payload = json.dumps(
        {"t": team_id, "u": user_id, "i": issued}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    signature = _sign(payload, secret)
    return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"


def verify_state(
    state: str,
    secret: str,
    *,
    ttl_seconds: int = _DEFAULT_STATE_TTL_SECONDS,
    now: int | None = None,
) -> EnrollmentState:
    """Validate a ``state`` from :func:`sign_state`, returning the bound identity.

    Raises :class:`StateError` if the token is malformed, the signature doesn't
    match (constant-time compare), or it is older than ``ttl_seconds``. ``now``
    is injectable for tests.
    """
    try:
        payload_b64, signature_b64 = state.split(".", 1)
        payload = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
    except (ValueError, TypeError) as exc:  # split / base64 decode failures
        raise StateError("Malformed enrollment token.") from exc

    expected = _sign(payload, secret)
    if not hmac.compare_digest(signature, expected):
        raise StateError("Enrollment token signature did not match.")

    try:
        data = json.loads(payload)
        team_id = str(data["t"])
        user_id = str(data["u"])
        issued_at = int(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise StateError("Malformed enrollment token payload.") from exc

    current = int(now if now is not None else time.time())
    if current - issued_at > ttl_seconds:
        raise StateError("Enrollment link expired. Start again from Slack.")
    if issued_at - current > ttl_seconds:
        # Clock skew / future-dated token — reject rather than trust it.
        raise StateError("Enrollment token is not yet valid.")

    return EnrollmentState(team_id=team_id, user_id=user_id, issued_at=issued_at)


@dataclass(frozen=True, slots=True)
class ExchangedToken:
    """An app-scoped token minted by the exchange, plus its lifetime."""

    access_token: str
    expires_in: int


async def exchange_token(
    *,
    workspace_host: str,
    subject_token: str,
    audience: str,
    scope: str = "all-apis",
    subject_token_type: str = _ACCESS_TOKEN_TYPE,
    http_timeout: float = 30.0,
) -> ExchangedToken:
    """Exchange a user's forwarded token for one scoped to the target app.

    RFC 8693 token exchange against ``{workspace_host}/oidc/v1/token`` with the
    target app's ``oauth2_app_client_id`` as ``audience``. Databricks scopes the
    result to that app — it can't call other Databricks APIs — which is what
    makes storing it per user least-privilege.

    :param workspace_host: Workspace base URL (scheme + host).
    :param subject_token: The user's forwarded ``x-forwarded-access-token``.
    :param audience: The target Omnigent app's ``oauth2_app_client_id``.
    :raises TokenExchangeError: On any non-200 or malformed response.
    """
    url = f"{workspace_host.rstrip('/')}/oidc/v1/token"
    form = {
        "grant_type": _TOKEN_EXCHANGE_GRANT_TYPE,
        "subject_token": subject_token,
        "subject_token_type": subject_token_type,
        "requested_token_type": _ACCESS_TOKEN_TYPE,
        "scope": scope,
        "audience": audience,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(http_timeout)) as client:
            resp = await client.post(url, data=form)
    except httpx.HTTPError as exc:
        raise TokenExchangeError(f"Could not reach the token endpoint: {exc}") from exc

    if resp.status_code != 200:
        # Don't surface the raw body to the user — it can carry internal detail.
        _logger.warning(
            "Databricks token exchange failed status=%s body=%r",
            resp.status_code,
            resp.text[:500],
        )
        raise TokenExchangeError(f"Token exchange failed (HTTP {resp.status_code}).")

    try:
        data = resp.json()
        access_token = str(data["access_token"])
        expires_in = int(data.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenExchangeError(f"Malformed token-exchange response: {exc}") from exc

    return ExchangedToken(access_token=access_token, expires_in=expires_in)
