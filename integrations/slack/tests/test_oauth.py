from __future__ import annotations

import httpx
import pytest
import respx
from omnigent_slack.oauth import (
    AuthMode,
    AuthorizationExpiredError,
    DeviceFlowClient,
    DeviceGrantUnavailableError,
    OAuthError,
    probe_auth_mode,
    start_login,
)

_BASE = "http://omnigent.test"
_ME = _BASE + "/v1/me"
_TOKEN = _BASE + "/oauth/token"


# ── Auth-mode probe (mirrors the CLI's /v1/me logic) ─────────────────


@respx.mock
async def test_probe_header_mode() -> None:
    respx.get(_ME).mock(return_value=httpx.Response(200, json={"user_id": "u", "is_admin": False}))
    assert await probe_auth_mode(_BASE) is AuthMode.HEADER


@respx.mock
async def test_probe_accounts_mode() -> None:
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/login"}))
    assert await probe_auth_mode(_BASE) is AuthMode.ACCOUNTS


@respx.mock
async def test_probe_oidc_mode() -> None:
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/auth/login"}))
    assert await probe_auth_mode(_BASE) is AuthMode.OIDC


@respx.mock
async def test_probe_unknown_401_defaults_to_oidc() -> None:
    # A 401 with no login_url (or a non-JSON body) falls through to OIDC,
    # matching the CLI (the ticket endpoint surfaces a clear error).
    respx.get(_ME).mock(return_value=httpx.Response(401, text="nope"))
    assert await probe_auth_mode(_BASE) is AuthMode.OIDC


# ── start_login: device grant (accounts/header) ──────────────────────


@respx.mock
async def test_start_login_device_grant() -> None:
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/login"}))
    authorize = respx.post(_BASE + "/oauth/device/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-2345",
                "verification_uri": _BASE + "/oauth/device",
                "verification_uri_complete": _BASE + "/oauth/device?user_code=ABCD-2345",
                "expires_in": 600,
                "interval": 5,
            },
        )
    )
    pending = await start_login(_BASE, client_id="slack")
    try:
        assert "user_code=ABCD-2345" in pending.verification_url
        assert pending.user_code == "ABCD-2345"
        # client_id is forwarded to the authorize call.
        import json as _json

        assert _json.loads(authorize.calls.last.request.content)["client_id"] == "slack"
    finally:
        await pending.close()


@respx.mock
async def test_start_login_header_mode_unsupported() -> None:
    """Header/proxy mode has no per-user login — start_login must reject it
    with a clear error, not fire a device-grant request the server 404s."""
    me = respx.get(_ME).mock(return_value=httpx.Response(200, json={"user_id": None}))
    authorize = respx.post(_BASE + "/oauth/device/authorize")
    with pytest.raises(OAuthError, match="header/proxy"):
        await start_login(_BASE, client_id="slack")
    assert me.called
    assert not authorize.called  # never attempts the device grant


@pytest.mark.parametrize("status", [404, 405])
@respx.mock
async def test_start_login_device_grant_not_enabled(status: int) -> None:
    """When /oauth/device/authorize isn't mounted (device grant disabled), the
    request falls through to the SPA catch-all (404/405) — start_login must
    raise DeviceGrantUnavailableError, not a generic transient OAuthError."""
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/login"}))
    respx.post(_BASE + "/oauth/device/authorize").mock(return_value=httpx.Response(status))
    with pytest.raises(DeviceGrantUnavailableError):
        await start_login(_BASE, client_id="slack")


@respx.mock
async def test_device_poll_pending_then_success() -> None:
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/login"}))
    respx.post(_BASE + "/oauth/device/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-2345",
                "verification_uri": _BASE + "/oauth/device",
                "verification_uri_complete": _BASE + "/oauth/device?user_code=ABCD-2345",
                "expires_in": 600,
                "interval": 0,  # no real sleep in the test
            },
        )
    )
    respx.post(_TOKEN).mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            ),
        ]
    )
    pending = await start_login(_BASE, client_id="slack")
    try:
        result = await pending.poll()
    finally:
        await pending.close()
    assert result.access_token == "at"
    assert result.refresh_token == "rt"


@respx.mock
async def test_device_poll_denied() -> None:
    from omnigent_slack.oauth import AuthorizationDeniedError

    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/login"}))
    respx.post(_BASE + "/oauth/device/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-2345",
                "verification_uri": _BASE + "/oauth/device",
                "verification_uri_complete": _BASE + "/oauth/device?user_code=ABCD-2345",
                "expires_in": 600,
                "interval": 0,
            },
        )
    )
    respx.post(_TOKEN).mock(return_value=httpx.Response(400, json={"error": "access_denied"}))
    pending = await start_login(_BASE, client_id="slack")
    try:
        with pytest.raises(AuthorizationDeniedError):
            await pending.poll()
    finally:
        await pending.close()


# ── start_login: OIDC CLI-ticket flow ────────────────────────────────


@respx.mock
async def test_start_login_oidc_ticket() -> None:
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/auth/login"}))
    respx.post(_BASE + "/auth/cli-login").mock(
        return_value=httpx.Response(
            200, json={"ticket": "T1", "login_url": "/auth/login?ticket=T1"}
        )
    )
    pending = await start_login(_BASE, client_id="slack")
    try:
        # Verification URL is the server-qualified login_url; no user code.
        assert pending.verification_url == _BASE + "/auth/login?ticket=T1"
        assert pending.user_code == ""
    finally:
        await pending.close()


@respx.mock
async def test_oidc_poll_pending_then_session_jwt() -> None:
    respx.get(_BASE + "/auth/cli-poll").mock(
        side_effect=[
            httpx.Response(202, json={"status": "pending"}),
            httpx.Response(200, json={"token": "sess-jwt", "user_id": "a@x", "expires_in": 28800}),
        ]
    )
    from omnigent_slack import oauth as _oauth

    client = httpx.AsyncClient(base_url=_BASE)
    try:
        result = await _oauth._poll_cli_ticket(client, "T1", interval=0)
    finally:
        await client.aclose()
    assert result.access_token == "sess-jwt"
    assert result.refresh_token == ""  # OIDC session JWT has no refresh token
    assert result.expires_in == 28800


@respx.mock
async def test_oidc_poll_malformed_200_raises_oauth_error() -> None:
    """A 200 missing the token field must raise OAuthError, not KeyError.

    An unhandled KeyError/ValueError would escape the background login
    task and strand the setup modal on "waiting for approval…" forever.
    """
    respx.get(_BASE + "/auth/cli-poll").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    from omnigent_slack import oauth as _oauth

    client = httpx.AsyncClient(base_url=_BASE)
    try:
        with pytest.raises(OAuthError):
            await _oauth._poll_cli_ticket(client, "T1", interval=0)
    finally:
        await client.aclose()


@respx.mock
async def test_device_poll_malformed_200_raises_oauth_error() -> None:
    respx.post(_TOKEN).mock(return_value=httpx.Response(200, text="not json"))
    from omnigent_slack import oauth as _oauth

    client = httpx.AsyncClient(base_url=_BASE)
    try:
        with pytest.raises(OAuthError):
            await _oauth._poll_device(client, "dc", interval=0, expires_in=600)
    finally:
        await client.aclose()


@respx.mock
async def test_oidc_poll_expired_ticket() -> None:
    respx.get(_BASE + "/auth/cli-poll").mock(return_value=httpx.Response(410, json={"error": "x"}))
    from omnigent_slack import oauth as _oauth

    client = httpx.AsyncClient(base_url=_BASE)
    try:
        with pytest.raises(AuthorizationExpiredError):
            await _oauth._poll_cli_ticket(client, "T1", interval=0)
    finally:
        await client.aclose()


# ── Refresh (device-grant tokens only) ───────────────────────────────


@respx.mock
async def test_refresh_rotates() -> None:
    respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}
        )
    )
    client = DeviceFlowClient(_BASE)
    try:
        pair = await client.refresh("rt1")
    finally:
        await client.aclose()
    assert pair.access_token == "at2"
    assert pair.refresh_token == "rt2"


# ── Device-grant client secret (X-Omnigent-Client-Secret header) ─────

_HEADER = "X-Omnigent-Client-Secret"


@respx.mock
async def test_client_secret_sent_on_device_authorize_and_poll() -> None:
    """When a client secret is configured, it rides the authorize AND the
    token-poll calls (same httpx client) — and only those."""
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/login"}))
    authorize = respx.post(_BASE + "/oauth/device/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-2345",
                "verification_uri": _BASE + "/oauth/device",
                "verification_uri_complete": _BASE + "/oauth/device?user_code=ABCD-2345",
                "expires_in": 600,
                "interval": 0,
            },
        )
    )
    token = respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )
    )
    pending = await start_login(_BASE, client_id="slack", client_secret="s3cr3t")
    try:
        assert authorize.calls.last.request.headers.get(_HEADER) == "s3cr3t"
        result = await pending.poll()
        assert result.access_token == "at"
        assert token.calls.last.request.headers.get(_HEADER) == "s3cr3t"
    finally:
        await pending.close()


@respx.mock
async def test_no_client_secret_sends_no_header() -> None:
    respx.get(_ME).mock(return_value=httpx.Response(401, json={"login_url": "/login"}))
    authorize = respx.post(_BASE + "/oauth/device/authorize").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-2345",
                "verification_uri": _BASE + "/oauth/device",
                "verification_uri_complete": _BASE + "/oauth/device?user_code=ABCD-2345",
                "expires_in": 600,
                "interval": 5,
            },
        )
    )
    pending = await start_login(_BASE, client_id="slack")
    try:
        assert _HEADER not in authorize.calls.last.request.headers
    finally:
        await pending.close()


@respx.mock
async def test_client_secret_sent_on_refresh_and_revoke() -> None:
    refresh = respx.post(_TOKEN).mock(
        return_value=httpx.Response(
            200, json={"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}
        )
    )
    revoke = respx.post(_BASE + "/oauth/revoke").mock(return_value=httpx.Response(200))
    client = DeviceFlowClient(_BASE, client_secret="s3cr3t")
    try:
        await client.refresh("rt1")
        await client.revoke("rt1")
    finally:
        await client.aclose()
    assert refresh.calls.last.request.headers.get(_HEADER) == "s3cr3t"
    assert revoke.calls.last.request.headers.get(_HEADER) == "s3cr3t"
