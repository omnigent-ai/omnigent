"""Client side of the Omnigent browserless-login flows.

A Slack user authorizes this bot to act as their own Omnigent identity
without any credential passing through Slack. The bot relays a login
link into the setup modal and polls in the background until the user
finishes in their browser. Two server auth modes are supported, detected
from the server itself (see :func:`probe_auth_mode`, mirroring the
``omnigent login`` CLI):

- **accounts** → OAuth 2.0 Device Authorization Grant (RFC 8628) against
  ``/oauth/*``. The user approves a consent page; the bot receives a
  scoped, rotating delegated token.
- **oidc** → the server's CLI-login ticket flow (``/auth/cli-login`` +
  ``/auth/cli-poll``). The user completes the real OIDC flow at the IdP;
  the bot receives the server's session JWT (no refresh — re-login on
  expiry), exactly as the ``omnigent`` CLI does.

Header/proxy mode is unsupported: identity is asserted by a trusted
upstream proxy, so the server mints no token and mounts no login
endpoint. :func:`start_login` raises an :class:`OAuthError` for it.

Both are surfaced through one :class:`PendingLogin` shape so the rest of
the bot (``auth_manager`` / ``setup``) is flow-agnostic.

See ``designs/DEVICE_AUTH.md`` for the device-grant design.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# Header carrying the optional device-grant client secret. Sent on the
# client-facing endpoints (authorize / token / revoke) so a server with
# OMNIGENT_DEVICE_CLIENT_SECRET set only accepts this authorized client.
_CLIENT_SECRET_HEADER = "X-Omnigent-Client-Secret"


def _secret_headers(client_secret: str | None) -> dict[str, str]:
    """Header dict carrying the client secret, or empty when unset."""
    return {_CLIENT_SECRET_HEADER: client_secret} if client_secret else {}


class AuthMode(enum.Enum):
    """The Omnigent server's auth posture, as probed from ``/v1/me``."""

    ACCOUNTS = "accounts"
    OIDC = "oidc"
    HEADER = "header"


class OAuthError(RuntimeError):
    """A login step failed in a way the user must resolve."""


class AuthorizationPendingError(OAuthError):
    """The user has not yet finished — keep polling."""


class AuthorizationDeniedError(OAuthError):
    """The user denied the request, or the grant was revoked."""


class AuthorizationExpiredError(OAuthError):
    """The login link expired before the user finished."""


class DeviceGrantUnavailableError(OAuthError):
    """The server has no device-grant endpoints mounted.

    Raised when ``/oauth/device/authorize`` responds as if the route does not
    exist (the server has ``OMNIGENT_DEVICE_GRANT_ENABLED`` off, so the request
    falls through to the SPA catch-all → 404/405). Distinct from a transient
    failure: retrying won't help — an operator must enable the device grant.
    """


@dataclass(frozen=True, slots=True)
class TokenResult:
    """A token obtained from a completed login.

    ``refresh_token`` is empty for OIDC session JWTs (the cli-ticket flow
    issues no refresh token — the bot re-logs-in on expiry).
    """

    access_token: str
    refresh_token: str
    expires_in: int


def _token_from_response(
    resp: httpx.Response, *, access_key: str, has_refresh: bool, default_expires: int
) -> TokenResult:
    """Parse a 200 token body, mapping a malformed one to ``OAuthError``.

    A 200 whose body is non-JSON, missing the token field, or carries a
    non-numeric ``expires_in`` would otherwise raise
    ``JSONDecodeError``/``KeyError``/``ValueError`` — none of which the
    login poller's caller catches, so the background task would die and
    leave the setup modal hung on "waiting for approval…". Normalise them
    into ``OAuthError`` so the caller reports a clean failure.
    """
    try:
        data = resp.json()
        access_token = str(data[access_key])
        refresh_token = str(data["refresh_token"]) if has_refresh else ""
        expires_in = int(data.get("expires_in", default_expires))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise OAuthError(f"Malformed token response: {exc}") from exc
    return TokenResult(
        access_token=access_token, refresh_token=refresh_token, expires_in=expires_in
    )


@dataclass(slots=True)
class PendingLogin:
    """A login in progress: a link to show the user + how to complete it.

    Flow-agnostic. ``verification_url`` goes in the modal; the caller then
    awaits :meth:`poll` in the background until it returns a
    :class:`TokenResult` or raises an :class:`OAuthError` subclass.
    :meth:`close` releases the underlying HTTP client.
    """

    verification_url: str
    # Short human-readable code to display, when the flow has one (device
    # grant). Empty for the OIDC ticket flow (the IdP page needs no code).
    user_code: str
    _poll: Callable[[], Awaitable[TokenResult]]
    _close: Callable[[], Awaitable[None]]

    async def poll(self) -> TokenResult:
        return await self._poll()

    async def close(self) -> None:
        await self._close()


async def probe_auth_mode(server_url: str, http_timeout: float = 10.0) -> AuthMode:
    """Detect the server's auth mode, mirroring ``omnigent login``.

    Unauthenticated ``GET /v1/me`` encodes the mode: ``200`` → header
    (a proxy injects identity), ``401`` with ``login_url == "/login"`` →
    accounts, ``401`` with ``login_url == "/auth/login"`` (or anything
    else) → oidc. A transport failure raises :class:`OAuthError`.

    :param server_url: Base URL of the Omnigent server.
    :returns: The detected :class:`AuthMode`.
    """
    async with httpx.AsyncClient(
        base_url=server_url.rstrip("/"), timeout=httpx.Timeout(http_timeout)
    ) as client:
        try:
            resp = await client.get("/v1/me")
        except httpx.HTTPError as exc:
            raise OAuthError(f"Could not reach {server_url}/v1/me: {exc}") from exc
    if resp.status_code == 200:
        return AuthMode.HEADER
    login_url: str | None = None
    if resp.status_code == 401:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            raw = body.get("login_url")
            login_url = raw if isinstance(raw, str) else None
    if login_url == "/login":
        return AuthMode.ACCOUNTS
    # "/auth/login" or unknown → OIDC (the ticket endpoint surfaces a clear
    # error if the server turns out not to support it).
    return AuthMode.OIDC


async def start_login(
    server_url: str, *, client_id: str, client_secret: str | None = None
) -> PendingLogin:
    """Begin the login flow matching the server's auth mode.

    Probes the mode, then starts the device grant (accounts) or the
    CLI-ticket flow (oidc). Returns a :class:`PendingLogin` the caller
    shows + polls. Raises :class:`OAuthError` if the flow can't be started.

    Header/proxy mode is **unsupported**: identity there is asserted by a
    trusted upstream proxy header, so the server mints no token and mounts
    no login endpoint (device grant or cli-ticket). A standalone bot can't
    obtain a per-user identity that way, so this raises rather than firing a
    device-grant request that the server would 404.

    :param client_id: RFC 8628 client id to present (device grant only;
        ignored by the OIDC ticket flow, which has no client identifier).
    :param client_secret: Optional device-grant client secret; sent on the
        device authorize/token calls when the server requires it. The OIDC
        ticket flow doesn't use it.
    """
    mode = await probe_auth_mode(server_url)
    if mode is AuthMode.OIDC:
        return await _start_cli_ticket_login(server_url)
    if mode is AuthMode.HEADER:
        raise OAuthError(
            "This server uses header/proxy authentication, which this bot "
            "can't log in to per user. Put the bot behind the same identity "
            "proxy, or run the server in accounts or OIDC mode."
        )
    return await _start_device_login(server_url, client_id=client_id, client_secret=client_secret)


# ── Device Authorization Grant (accounts mode) ───────────────────────


async def _start_device_login(
    server_url: str, *, client_id: str, client_secret: str | None = None
) -> PendingLogin:
    # The secret rides on the client's default headers so it's sent on both
    # the authorize call here and every token poll on the same client.
    client = httpx.AsyncClient(
        base_url=server_url.rstrip("/"),
        timeout=httpx.Timeout(30.0),
        headers=_secret_headers(client_secret),
    )
    try:
        resp = await client.post("/oauth/device/authorize", json={"client_id": client_id})
        # 404/405 here means the /oauth/* router isn't mounted (the server has
        # OMNIGENT_DEVICE_GRANT_ENABLED off), so the request fell through to the
        # SPA catch-all. That's not transient — surface it as its own error.
        if resp.status_code in (404, 405):
            await client.aclose()
            raise DeviceGrantUnavailableError(
                f"Device grant not enabled on {server_url} (HTTP {resp.status_code})."
            )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise OAuthError(f"Could not start device authorization: {exc}") from exc
    try:
        data = resp.json()
        device_code = str(data["device_code"])
        interval = max(int(data.get("interval", 5)), 1)
        expires_in = int(data.get("expires_in", 600))
        verification_url = str(data["verification_uri_complete"])
        user_code = str(data.get("user_code", ""))
    except (ValueError, KeyError, TypeError) as exc:
        # A 200 with a malformed body would otherwise leak the open client.
        await client.aclose()
        raise OAuthError(f"Malformed device authorization response: {exc}") from exc

    async def _poll() -> TokenResult:
        return await _poll_device(client, device_code, interval, expires_in)

    return PendingLogin(
        verification_url=verification_url,
        user_code=user_code,
        _poll=_poll,
        _close=client.aclose,
    )


async def _poll_device(
    client: httpx.AsyncClient, device_code: str, interval: int, expires_in: int
) -> TokenResult:
    deadline = asyncio.get_event_loop().time() + expires_in
    while True:
        if asyncio.get_event_loop().time() >= deadline:
            raise AuthorizationExpiredError("The login link expired.")
        await asyncio.sleep(interval)
        try:
            resp = await client.post(
                "/oauth/token",
                data={"grant_type": _DEVICE_GRANT_TYPE, "device_code": device_code},
            )
        except httpx.HTTPError:
            continue  # transient — keep polling until the deadline (mirrors the ticket flow)
        if resp.status_code == 200:
            return _token_from_response(
                resp, access_key="access_token", has_refresh=True, default_expires=3600
            )
        error = _error_code(resp)
        if error == "slow_down":
            interval += 1
            continue
        if error == "authorization_pending":
            continue
        if error == "access_denied":
            raise AuthorizationDeniedError("You denied the login request.")
        if error == "expired_token":
            raise AuthorizationExpiredError("The login link expired.")
        raise OAuthError(f"Token request failed: {error or resp.status_code}")


# ── OIDC CLI-login ticket flow ───────────────────────────────────────


async def _start_cli_ticket_login(server_url: str) -> PendingLogin:
    base = server_url.rstrip("/")
    client = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(30.0))
    try:
        resp = await client.post("/auth/cli-login")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise OAuthError(f"Could not start login: {exc}") from exc
    try:
        data = resp.json()
        ticket = str(data["ticket"])
        # login_url is a server-relative path (e.g. "/auth/login?ticket=…").
        login_url = str(data["login_url"])
    except (ValueError, KeyError, TypeError) as exc:
        # A 200 with a malformed body would otherwise leak the open client.
        await client.aclose()
        raise OAuthError(f"Malformed cli-login response: {exc}") from exc
    verification_url = login_url if login_url.startswith("http") else f"{base}{login_url}"

    async def _poll() -> TokenResult:
        return await _poll_cli_ticket(client, ticket)

    return PendingLogin(
        verification_url=verification_url,
        user_code="",
        _poll=_poll,
        _close=client.aclose,
    )


async def _poll_cli_ticket(
    client: httpx.AsyncClient, ticket: str, interval: int = 2, timeout_seconds: int = 300
) -> TokenResult:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while True:
        if asyncio.get_event_loop().time() >= deadline:
            raise AuthorizationExpiredError("The login link expired.")
        await asyncio.sleep(interval)
        try:
            resp = await client.get("/auth/cli-poll", params={"ticket": ticket})
        except httpx.HTTPError:
            continue  # transient — keep polling until the deadline
        if resp.status_code == 202:
            continue  # pending: browser flow not finished
        if resp.status_code == 200:
            # No refresh token in the ticket flow — the session JWT stands
            # alone until it expires, then the user re-logs-in.
            return _token_from_response(
                resp, access_key="token", has_refresh=False, default_expires=8 * 3600
            )
        # 410 (expired/unknown) or any other status → terminal.
        raise AuthorizationExpiredError("The login link expired or was rejected.")


class DeviceFlowClient:
    """Talks to a single Omnigent server's ``/oauth/*`` endpoints.

    Used for token refresh and revocation of device-grant tokens (the
    login start/poll now lives in :func:`start_login`). Sends the optional
    device-grant client secret (when configured) on every call, since the
    ``/oauth/token`` and ``/oauth/revoke`` endpoints may be secret-gated.
    """

    def __init__(
        self, base_url: str, timeout: float = 30.0, *, client_secret: str | None = None
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout),
            headers=_secret_headers(client_secret),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def refresh(self, refresh_token: str) -> TokenResult:
        """Exchange a refresh token for a fresh access + refresh pair."""
        response = await self._client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        if response.status_code != 200:
            raise OAuthError(f"Refresh failed: {_error_code(response) or response.status_code}")
        return _token_from_response(
            response, access_key="access_token", has_refresh=True, default_expires=3600
        )

    async def revoke(self, refresh_token: str) -> None:
        """Revoke the grant behind a refresh token. Best-effort."""
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post("/oauth/revoke", data={"refresh_token": refresh_token})


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            return error
    return None
