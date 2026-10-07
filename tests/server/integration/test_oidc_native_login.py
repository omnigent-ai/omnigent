"""Integration tests for the native (RFC 8252 loopback) OIDC sign-in.

Drives the real ``/auth/login`` → ``/auth/callback`` → ``/auth/native-token``
routes on a FastAPI app with OIDC auth enabled. Only the external IdP
(token and email endpoints) is mocked.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
import pytest

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig, derive_code_challenge
from omnigent.server.routes import auth as auth_routes
from omnigent.server.routes.auth import create_auth_router

pytestmark = pytest.mark.asyncio

_TEST_SECRET = b"n" * 32
_REDIRECT = "http://127.0.0.1:53682/callback"
_VERIFIER = "v" * 64
_NATIVE_STATE = "desktop-state-123"


def _config(allowed_domains: frozenset[str] | None = None) -> OIDCConfig:
    """Build a GitHub-flavoured OIDC config whose IdP calls are mocked."""
    return OIDCConfig(
        issuer="https://github.com",
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
        scopes="read:user user:email",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=allowed_domains,
        provider_type="github",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        jwks_uri=None,
        userinfo_endpoint="https://api.github.com/user",
        allow_invites=False,
    )


def _transport(
    *,
    allowed_domains: frozenset[str] | None = None,
    device_grant_store: object | None = None,
) -> httpx.ASGITransport:
    """Mount only the OIDC auth router on a bare FastAPI app."""
    from fastapi import FastAPI

    config = _config(allowed_domains)
    router = create_auth_router(
        auth_provider=UnifiedAuthProvider(source="oidc", oidc_config=config),
        permission_store=None,
        # A fresh directory, so no admin list on the host can leak in.
        admin_list=AdminList(Path(tempfile.mkdtemp()) / "no-admins.txt"),
        device_grant_store=device_grant_store,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router, prefix="/auth")
    return httpx.ASGITransport(app=app)


def _idp_client(token_status: int = 200, email: str = "alice@example.com") -> AsyncMock:
    """Mock ``httpx.AsyncClient`` for GitHub's token and email endpoints."""
    token_resp = MagicMock(status_code=token_status, text="{}")
    token_resp.json.return_value = {"access_token": "gho_test", "token_type": "bearer"}
    emails_resp = MagicMock(status_code=200)
    emails_resp.json.return_value = [{"email": email, "primary": True, "verified": True}]
    client = AsyncMock()
    client.post = AsyncMock(return_value=token_resp)
    client.get = AsyncMock(return_value=emails_resp)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _native_params(**overrides: str) -> dict[str, str]:
    params = {
        "native_redirect_uri": _REDIRECT,
        "native_state": _NATIVE_STATE,
        "code_challenge": derive_code_challenge(_VERIFIER),
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return params


async def _start(client: httpx.AsyncClient) -> tuple[str, str]:
    """Begin a native sign-in; return the IdP ``state`` and the state cookie."""
    resp = await client.get("/auth/login", params=_native_params())
    assert resp.status_code == 302
    idp_state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    return idp_state, resp.cookies["ap_auth_state"]


async def _callback(
    client: httpx.AsyncClient,
    idp_state: str,
    state_cookie: str,
    idp: AsyncMock | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    client.cookies.clear()
    client.cookies.set("ap_auth_state", state_cookie)
    with patch("omnigent.server.routes.auth.httpx.AsyncClient", return_value=idp or _idp_client()):
        return await client.get(
            "/auth/callback",
            params=params if params is not None else {"code": "idp-code", "state": idp_state},
        )


def _loopback_query(resp: httpx.Response) -> dict[str, str]:
    """Assert ``resp`` redirects to the loopback and return its query."""
    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == _REDIRECT
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def _exchange(client: httpx.AsyncClient, **form: str) -> httpx.Response:
    return await client.post("/auth/native-token", data=form)


def _client(transport: httpx.ASGITransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)


async def test_native_sign_in_delivers_code_and_exchanges_for_session() -> None:
    """The full loopback flow ends with a session token, and the browser gets none."""
    grant_store = MagicMock()
    with patch.object(auth_routes, "issue_login_grant", return_value="refresh-abc") as issue:
        async with _client(_transport(device_grant_store=grant_store)) as client:
            idp_state, state_cookie = await _start(client)
            callback = await _callback(client, idp_state, state_cookie)
            query = _loopback_query(callback)
            assert query["state"] == _NATIVE_STATE
            assert "ap_session" not in callback.cookies

            resp = await _exchange(
                client, code=query["code"], code_verifier=_VERIFIER, redirect_uri=_REDIRECT
            )

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert jwt.decode(body["token"], _TEST_SECRET, algorithms=["HS256"])["sub"] == (
        "alice@example.com"
    )
    assert body["user_id"] == "alice@example.com"
    assert body["expires_in"] == 8 * 3600
    assert body["refresh_token"] == "refresh-abc"
    issue.assert_called_once()
    assert issue.call_args.kwargs["user_id"] == "alice@example.com"


async def test_native_token_omits_refresh_without_grant_store() -> None:
    async with _client(_transport()) as client:
        idp_state, state_cookie = await _start(client)
        query = _loopback_query(await _callback(client, idp_state, state_cookie))
        resp = await _exchange(
            client, code=query["code"], code_verifier=_VERIFIER, redirect_uri=_REDIRECT
        )

    assert resp.status_code == 200
    assert "refresh_token" not in resp.json()


async def test_native_code_is_single_use() -> None:
    async with _client(_transport()) as client:
        idp_state, state_cookie = await _start(client)
        query = _loopback_query(await _callback(client, idp_state, state_cookie))
        form = {"code": query["code"], "code_verifier": _VERIFIER, "redirect_uri": _REDIRECT}
        first = await _exchange(client, **form)
        second = await _exchange(client, **form)

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json() == {"error": "invalid_grant"}


@pytest.mark.parametrize(
    "override",
    [
        {"code_verifier": "w" * 64},
        {"code_verifier": "short"},
        {"redirect_uri": "http://127.0.0.1:53683/callback"},
    ],
    ids=["wrong-verifier", "malformed-verifier", "other-redirect"],
)
async def test_native_exchange_rejects_mismatch_and_consumes_code(
    override: dict[str, str],
) -> None:
    """A failed attempt burns the code, so a stolen code can't be retried."""
    async with _client(_transport()) as client:
        idp_state, state_cookie = await _start(client)
        query = _loopback_query(await _callback(client, idp_state, state_cookie))
        good = {"code": query["code"], "code_verifier": _VERIFIER, "redirect_uri": _REDIRECT}
        bad = await _exchange(client, **{**good, **override})
        retry = await _exchange(client, **good)

    assert bad.status_code == 400
    assert bad.json() == {"error": "invalid_grant"}
    assert retry.status_code == 400


async def test_native_code_expires() -> None:
    async with _client(_transport()) as client:
        idp_state, state_cookie = await _start(client)
        query = _loopback_query(await _callback(client, idp_state, state_cookie))
        with patch.object(auth_routes, "_NATIVE_CODE_TTL_SECONDS", -1):
            resp = await _exchange(
                client, code=query["code"], code_verifier=_VERIFIER, redirect_uri=_REDIRECT
            )

    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_grant"}


async def test_native_exchange_requires_fields_and_still_burns_the_code() -> None:
    async with _client(_transport()) as client:
        idp_state, state_cookie = await _start(client)
        query = _loopback_query(await _callback(client, idp_state, state_cookie))
        incomplete = await _exchange(client, code=query["code"])
        retry = await _exchange(
            client, code=query["code"], code_verifier=_VERIFIER, redirect_uri=_REDIRECT
        )
        no_code = await _exchange(client, code_verifier=_VERIFIER, redirect_uri=_REDIRECT)

    assert incomplete.status_code == 400
    assert incomplete.json() == {"error": "invalid_request"}
    assert retry.json() == {"error": "invalid_grant"}
    assert no_code.json() == {"error": "invalid_request"}


@pytest.mark.parametrize(
    "override",
    [
        {"native_redirect_uri": "http://localhost:53682/callback"},
        {"native_redirect_uri": "https://127.0.0.1:53682/callback"},
        {"native_redirect_uri": "http://0.0.0.0:53682/callback"},
        {"native_redirect_uri": "http://evil.example/callback"},
        {"native_redirect_uri": "http://127.0.0.1/callback"},
        {"native_redirect_uri": "http://user@127.0.0.1:53682/callback"},
        {"native_redirect_uri": "http://127.0.0.1:53682/callback?x=1"},
        {"native_redirect_uri": "http://127.0.0.1:53682/callback#x"},
        {"native_redirect_uri": "http://127.0.0.1.evil.example:53682/callback"},
        {"code_challenge_method": "plain"},
        {"code_challenge": "too-short"},
        {"native_state": ""},
        {"native_state": "has space"},
    ],
)
async def test_login_rejects_invalid_native_parameters(override: dict[str, str]) -> None:
    async with _client(_transport()) as client:
        resp = await client.get("/auth/login", params=_native_params(**override))

    assert resp.status_code == 400
    assert resp.json() == {"error": "Invalid native sign-in parameters"}


async def test_login_rejects_incomplete_native_parameters() -> None:
    async with _client(_transport()) as client:
        resp = await client.get("/auth/login", params={"native_redirect_uri": _REDIRECT})

    assert resp.status_code == 400


async def test_login_accepts_ipv6_loopback() -> None:
    redirect = "http://[::1]:53682/callback"
    async with _client(_transport()) as client:
        resp = await client.get("/auth/login", params=_native_params(native_redirect_uri=redirect))

    assert resp.status_code == 302
    state = jwt.decode(resp.cookies["ap_auth_state"], _TEST_SECRET, algorithms=["HS256"])
    assert state["native"]["redirect_uri"] == redirect


async def test_login_rejects_native_with_ticket() -> None:
    async with _client(_transport()) as client:
        resp = await client.get("/auth/login", params={**_native_params(), "ticket": "t"})

    assert resp.status_code == 400


async def test_native_callback_reports_admission_denial_to_loopback() -> None:
    async with _client(_transport(allowed_domains=frozenset({"corp.example"}))) as client:
        idp_state, state_cookie = await _start(client)
        query = _loopback_query(await _callback(client, idp_state, state_cookie))

    assert query["error"] == "access_denied"
    assert (
        query["error_description"] == "Email domain 'example.com' is not permitted on this server"
    )
    assert query["state"] == _NATIVE_STATE
    assert "code" not in query


async def test_native_callback_reports_idp_error_to_loopback() -> None:
    async with _client(_transport()) as client:
        idp_state, state_cookie = await _start(client)
        resp = await _callback(
            client,
            idp_state,
            state_cookie,
            params={"error": "access_denied", "state": idp_state},
        )

    query = _loopback_query(resp)
    assert query["error"] == "access_denied"
    assert query["state"] == _NATIVE_STATE


async def test_native_callback_reports_token_exchange_failure_to_loopback() -> None:
    async with _client(_transport()) as client:
        idp_state, state_cookie = await _start(client)
        query = _loopback_query(
            await _callback(client, idp_state, state_cookie, idp=_idp_client(token_status=401))
        )

    assert query["error"] == "server_error"
    assert query["state"] == _NATIVE_STATE


async def test_callback_without_valid_state_never_redirects_to_loopback() -> None:
    """A forged or missing state cookie can't steer the browser to a loopback."""
    async with _client(_transport()) as client:
        idp_state, _ = await _start(client)
        forged = jwt.encode(
            {"state": idp_state, "native": {"redirect_uri": _REDIRECT}},
            b"x" * 32,
            algorithm="HS256",
        )
        resp = await _callback(
            client,
            idp_state,
            forged,
            params={"error": "access_denied", "state": idp_state},
        )

    assert resp.status_code == 400
    assert resp.json() == {"error": "Missing code or state parameter"}


async def test_browser_login_is_unchanged_by_native_support() -> None:
    """A plain browser login still lands on the app with a session cookie."""
    async with _client(_transport()) as client:
        resp = await client.get("/auth/login", params={"return_to": "/c/1"})
        idp_state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
        callback = await _callback(client, idp_state, resp.cookies["ap_auth_state"])

    assert callback.status_code == 302
    assert callback.headers["location"] == "/c/1"
    assert "ap_session" in callback.cookies


async def test_redirect_query_round_trips() -> None:
    """The loopback query carries exactly the delivered params plus ``state``."""
    native = {"redirect_uri": _REDIRECT, "state": "s", "code_challenge": "c"}
    resp = auth_routes._native_redirect(native, {"code": "a b&c"})
    assert resp.headers["location"] == f"{_REDIRECT}?{urlencode({'code': 'a b&c', 'state': 's'})}"
