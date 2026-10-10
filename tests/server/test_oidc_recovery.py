"""Regression coverage for OIDC provider-error recovery."""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.admin_list import AdminList
from omnigent.server.app import BasePathMiddleware
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig, derive_code_challenge
from omnigent.server.routes.auth import create_auth_router

_SECRET = b"oidc-recovery-test-only-secret-32"
_ISSUER = "https://idp.example.test"
_LOGGER = "omnigent.server.routes.auth"


class _OidcLogin:
    """A router client with controllable provider claims and signed state."""

    def __init__(self, directory: Path, forms: list[dict[str, str]], claims: dict[str, object]):
        self.admins = AdminList(directory / "admins")
        self.forms = forms
        self.claims = claims
        self.clients: list[TestClient] = []
        self.build_client()

    def build_client(self, *, secure: bool = False, base_path: str = "", invites: bool = False):
        self.prefix = f"{base_path}/auth"
        origin = "https://omni.example.test" if secure else "http://localhost:8000"
        self.config = OIDCConfig(
            issuer=_ISSUER,
            client_id="test-client",
            client_secret="test-client-secret",
            redirect_uri=f"{origin}{self.prefix}/callback",
            cookie_secret=_SECRET,
            scopes="openid email profile",
            session_ttl_hours=8,
            logout_redirect_uri=None,
            allowed_domains=None,
            provider_type="oidc",
            authorization_endpoint=f"{_ISSUER}/authorize",
            token_endpoint=f"{_ISSUER}/token",
            jwks_uri=f"{_ISSUER}/jwks",
            userinfo_endpoint=None,
            allow_invites=invites,
        )
        self.app = FastAPI()
        self.app.state.base_path = base_path
        self.app.include_router(
            create_auth_router(
                UnifiedAuthProvider(source="oidc", oidc_config=self.config),
                permission_store=None,
                admin_list=self.admins,
                account_store=MagicMock() if invites else None,
            ),
            prefix="/auth",
        )
        if base_path:
            self.app.add_middleware(BasePathMiddleware, base_path=base_path)
        self.client = TestClient(self.app, base_url=origin, follow_redirects=False)
        self.clients.append(self.client)
        self.cookie = "__Host-ap_auth_state" if secure else "ap_auth_state"

    def login(self, **params: str) -> tuple[httpx.Response, dict]:
        response = self.client.get(f"{self.prefix}/login", params=params)
        assert response.status_code == 302
        return (response, self.state())

    def state(self) -> dict:
        return jwt.decode(self.client.cookies.get(self.cookie), _SECRET, algorithms=["HS256"])

    def replace_state(self, payload: dict, secret: bytes = _SECRET) -> None:
        cookie = next(cookie for cookie in self.client.cookies.jar if cookie.name == self.cookie)
        self.client.cookies.clear()
        self.client.cookies.set(
            self.cookie,
            jwt.encode(payload, secret, algorithm="HS256"),
            domain=cookie.domain,
            path=cookie.path,
        )

    def set_cookie_condition(self, kind: str) -> None:
        """Set the cookie side of a callback-validation case."""
        if kind == "missing_cookie":
            self.client.cookies.clear()
        elif kind == "signature":
            self.replace_state(self.state(), b"different-test-only-signing-key")
        elif kind == "expired":
            self.replace_state({**self.state(), "exp": int(time.time()) - 30})

    def expired(self, state: str, **extra: str) -> httpx.Response:
        return self.client.get(
            f"{self.prefix}/callback",
            params={
                "state": state,
                "error": "temporarily_unavailable",
                "error_description": "authentication_expired",
                **extra,
            },
        )

    def finish(self, state: str) -> httpx.Response:
        return self.client.get(
            f"{self.prefix}/callback", params={"state": state, "code": "test-code"}
        )

    def recovery_link(self, response: httpx.Response) -> str:
        return html.unescape(re.search('href="([^"]+)"', response.text).group(1))

    def assert_manual_restart(self, response: httpx.Response) -> None:
        assert response.status_code == 400
        assert "text/html" in response.headers["content-type"]
        assert self.recovery_link(response) == f"{self.prefix}/login"
        assert "location" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert self.forms == []


@pytest.fixture
def oidc_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_OidcLogin]:
    """Stub only the IdP exchange/JWKS; verify actual JWT signatures locally."""
    forms: list[dict[str, str]] = []
    claims: dict[str, object] = {}
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        "omnigent.server.routes.auth.resolve_allowed_domains_path", lambda: tmp_path / "domains"
    )

    async def token_response(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        assert url == f"{_ISSUER}/token"
        forms.append(kwargs["data"])
        token = jwt.encode(
            {
                "iss": _ISSUER,
                "aud": "test-client",
                "sub": "test-subject",
                "email": "User@example.test",
                "email_verified": True,
                "exp": int(time.time()) + 300,
                **claims,
            },
            key,
            algorithm="RS256",
        )
        return httpx.Response(200, json={"id_token": token})

    monkeypatch.setattr(httpx.AsyncClient, "post", token_response)
    monkeypatch.setattr(
        jwt.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, token: SimpleNamespace(key=key.public_key()),
    )
    flow = _OidcLogin(tmp_path, forms, claims)
    try:
        yield flow
    finally:
        for client in reversed(flow.clients):
            client.close()


def test_expired_flow_rotates_state_and_pkce_once(oidc_login: _OidcLogin) -> None:
    original_response, original = oidc_login.login(return_to="/sessions/test")
    response = oidc_login.expired(original["state"])
    assert response.status_code == 302
    retried = oidc_login.state()
    assert retried["state"] != original["state"]
    assert retried["code_verifier"] != original["code_verifier"]
    assert retried["oidc_retry"]
    assert retried["return_to"] == "/sessions/test"
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == [retried["state"]]
    assert query["code_challenge"] == [derive_code_challenge(retried["code_verifier"])]
    for name in ("client_id", "redirect_uri", "scope", "response_type", "code_challenge_method"):
        assert query[name] == parse_qs(urlsplit(original_response.headers["location"]).query)[name]
    assert oidc_login.forms == []
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None


def test_repeated_expiry_stops_and_clears_cookie(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login()
    oidc_login.expired(original["state"])
    retried = oidc_login.state()
    response = oidc_login.expired(retried["state"], oidc_retry="0")
    assert response.status_code == 400
    assert "Your sign-in session expired" in response.text
    assert "location" not in response.headers
    assert urlsplit(oidc_login.recovery_link(response)).path == "/auth/login"
    assert oidc_login.client.cookies.get(oidc_login.cookie) is None
    assert oidc_login.forms == []


@pytest.mark.parametrize(
    ("error", "description"),
    (
        ("access_denied", "declined"),
        ("temporarily_unavailable", "another_problem"),
        ("server_error", "authentication_expired"),
        ("<script>secret-value</script>", "<script>private-detail</script>"),
    ),
)
def test_provider_errors_do_not_retry_or_reflect_details(
    oidc_login: _OidcLogin, error: str, description: str, caplog: pytest.LogCaptureFixture
) -> None:
    _, original = oidc_login.login()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        response = oidc_login.client.get(
            f"{oidc_login.prefix}/callback",
            params={"state": original["state"], "error": error, "error_description": description},
        )
    assert response.status_code == 400
    assert "Sign-in could not be completed" in response.text
    assert "private-detail" not in response.text + str(
        [record.message for record in caplog.records if record.name == _LOGGER]
    )
    assert "secret-value" not in response.text + str(
        [record.message for record in caplog.records if record.name == _LOGGER]
    )
    assert original["state"] not in str(
        [record.message for record in caplog.records if record.name == _LOGGER]
    )
    records = [record for record in caplog.records if record.name == _LOGGER]
    assert len(records) == 1
    assert records[0].levelno == (logging.INFO if error == "access_denied" else logging.WARNING)
    assert "location" not in response.headers
    assert oidc_login.client.cookies.get(oidc_login.cookie) is None
    assert oidc_login.forms == []


def test_missing_state_offers_restart_without_changing_active_cookie(
    oidc_login: _OidcLogin,
) -> None:
    oidc_login.login(return_to="/sessions/active", ticket="active-ticket")
    cookie = oidc_login.client.cookies.get(oidc_login.cookie)
    response = oidc_login.client.get(
        f"{oidc_login.prefix}/callback",
        params={
            "error": "<script>private-error</script>",
            "error_description": "private-description",
            "code": "private-code",
            "return_to": "https://attacker.example/private-path",
            "ticket": "private-ticket",
            "invite": "private-invite",
            "reauth": "1",
        },
    )
    oidc_login.assert_manual_restart(response)
    assert "private-" not in response.text
    assert "active-ticket" not in response.text
    assert "set-cookie" not in response.headers
    assert oidc_login.client.cookies.get(oidc_login.cookie) == cookie


def test_missing_cookie_offers_manual_restart(oidc_login: _OidcLogin) -> None:
    response = oidc_login.expired("private-state", code="private-code", ticket="private-ticket")
    oidc_login.assert_manual_restart(response)
    assert "private-" not in response.text
    assert "set-cookie" not in response.headers
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None


@pytest.mark.parametrize("kind", ("signature", "expired", "mismatch"))
def test_invalid_expired_or_mismatched_state_cannot_retry(
    oidc_login: _OidcLogin, kind: str
) -> None:
    _, original = oidc_login.login()
    oidc_login.set_cookie_condition(kind)
    response = oidc_login.expired(
        "another-state" if kind == "mismatch" else original["state"],
        code="private-code",
        return_to="https://attacker.example/private-path",
        ticket="private-ticket",
        invite="private-invite",
        reauth="1",
    )
    oidc_login.assert_manual_restart(response)
    assert "private-" not in response.text
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None
    if kind == "mismatch":
        assert "set-cookie" not in response.headers
        assert oidc_login.state() == original
    else:
        assert oidc_login.client.cookies.get(oidc_login.cookie) is None


def test_old_tab_error_preserves_second_tab_pending_login(oidc_login: _OidcLogin) -> None:
    _, first = oidc_login.login(return_to="/sessions/first", ticket="first-ticket")
    _, second = oidc_login.login(return_to="/sessions/second")
    cookie = oidc_login.client.cookies.get(oidc_login.cookie)
    response = oidc_login.expired(first["state"])
    oidc_login.assert_manual_restart(response)
    assert "set-cookie" not in response.headers
    assert "/sessions/" not in response.text
    assert "first-ticket" not in response.text
    assert oidc_login.client.cookies.get(oidc_login.cookie) == cookie
    success = oidc_login.finish(second["state"])
    assert success.status_code == 302
    assert success.headers["location"] == "/sessions/second"
    assert oidc_login.forms[0]["code_verifier"] == second["code_verifier"]


@pytest.mark.parametrize(
    ("kind", "message"),
    (
        ("missing_state", "Missing code or state parameter"),
        ("missing_cookie", "Missing auth state cookie"),
        ("signature", "Invalid or expired auth state"),
        ("expired", "Invalid or expired auth state"),
        ("mismatch", "State mismatch (possible CSRF)"),
    ),
)
def test_invalid_code_callbacks_keep_json_rejection(
    oidc_login: _OidcLogin, kind: str, message: str
) -> None:
    _, original = oidc_login.login()
    oidc_login.set_cookie_condition(kind)
    params = {"code": "test-code"}
    if kind != "missing_state":
        params["state"] = "another-state" if kind == "mismatch" else original["state"]
    response = oidc_login.client.get(f"{oidc_login.prefix}/callback", params=params)
    assert response.status_code == 400
    assert response.json() == {"error": message}
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None
    assert oidc_login.forms == []


def test_old_tab_error_preserves_second_tab_completed_session(oidc_login: _OidcLogin) -> None:
    _, first = oidc_login.login()
    _, second = oidc_login.login()
    assert oidc_login.finish(second["state"]).status_code == 302
    session = oidc_login.client.cookies.get(oidc_login.config.session_cookie_name)
    oidc_login.forms.clear()
    response = oidc_login.expired(first["state"])
    oidc_login.assert_manual_restart(response)
    assert "set-cookie" not in response.headers
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) == session


def test_expired_cookie_restart_starts_fresh_without_old_context(oidc_login: _OidcLogin) -> None:
    oidc_login.build_client(invites=True)
    _, original = oidc_login.login(
        return_to="/sessions/old", ticket="old-ticket", invite="old-invite", reauth="1"
    )
    oidc_login.replace_state({**original, "exp": int(time.time()) - 30})
    response = oidc_login.expired(original["state"])
    oidc_login.assert_manual_restart(response)
    assert oidc_login.client.cookies.get(oidc_login.cookie) is None
    restart = oidc_login.client.get(oidc_login.recovery_link(response))
    assert restart.status_code == 302
    fresh = oidc_login.state()
    assert fresh["state"] != original["state"]
    assert fresh["code_verifier"] != original["code_verifier"]
    assert fresh["return_to"] == "/"
    for key in ("ticket", "invite", "reauth_at", "oidc_retry"):
        assert key not in fresh
    assert oidc_login.finish(fresh["state"]).status_code == 302


def test_unverified_restart_respects_base_path(oidc_login: _OidcLogin) -> None:
    oidc_login.build_client(base_path="/omnigent")
    oidc_login.assert_manual_restart(oidc_login.expired("private-state"))


def test_invalid_https_cookie_is_cleared_with_matching_attributes(oidc_login: _OidcLogin) -> None:
    oidc_login.build_client(secure=True)
    _, original = oidc_login.login()
    oidc_login.replace_state({**original, "exp": int(time.time()) - 30})
    response = oidc_login.expired(original["state"])
    oidc_login.assert_manual_restart(response)
    header = response.headers["set-cookie"]
    for attribute in (
        "__Host-ap_auth_state=",
        "Max-Age=0",
        "Path=/",
        "Secure",
        "HttpOnly",
        "SameSite=lax",
    ):
        assert attribute in header
    assert oidc_login.client.cookies.get(oidc_login.cookie) is None


def test_retry_marker_query_parameter_is_ignored(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login(oidc_retry="1")
    assert "oidc_retry" not in original
    assert oidc_login.expired(original["state"]).status_code == 302


def test_old_callback_cannot_use_rotated_cookie(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login()
    oidc_login.expired(original["state"])
    assert oidc_login.expired(original["state"]).status_code == 400
    assert oidc_login.finish(original["state"]).status_code == 400
    assert oidc_login.forms == []


def test_error_with_code_does_not_exchange_or_mint_session(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login()
    assert oidc_login.expired(original["state"], code="test-code").status_code == 302
    assert oidc_login.forms == []
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None


def test_missing_code_without_error_remains_invalid(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login()
    response = oidc_login.client.get(
        f"{oidc_login.prefix}/callback", params={"state": original["state"]}
    )
    assert response.json()["error"] == "Missing code or state parameter"


def test_success_after_retry_uses_fresh_verifier_and_real_jwt(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login(return_to="/sessions/test")
    retry = oidc_login.expired(original["state"])
    assert retry.status_code == 302
    retried = oidc_login.state()
    assert retried["state"] != original["state"]
    response = oidc_login.finish(retried["state"])
    assert response.status_code == 302
    assert response.headers["location"] == "/sessions/test"
    assert oidc_login.forms[0]["code_verifier"] == retried["code_verifier"]
    session = jwt.decode(
        oidc_login.client.cookies.get(oidc_login.config.session_cookie_name),
        _SECRET,
        algorithms=["HS256"],
    )
    assert session["sub"] == "user@example.test"
    assert oidc_login.client.cookies.get(oidc_login.cookie) is None


@pytest.mark.parametrize(
    "redirect", ("http://127.0.0.1:53682/callback", "ai.omnigent.ios:/oauth/callback")
)
@pytest.mark.parametrize("error", ("access_denied", "temporarily_unavailable", ""))
@pytest.mark.parametrize("code", (None, "test-code"))
def test_native_errors_return_immediately_without_issuing_credentials(
    oidc_login: _OidcLogin, redirect: str, error: str, code: str | None
) -> None:
    _, original = oidc_login.login(
        native_redirect_uri=redirect,
        native_state="native-state",
        code_challenge=derive_code_challenge("v" * 64),
        code_challenge_method="S256",
    )
    params = {
        "state": original["state"],
        "error": error,
        "error_description": "authentication_expired",
    }
    if code is not None:
        params["code"] = code
    response = oidc_login.client.get(f"{oidc_login.prefix}/callback", params=params)
    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert target._replace(query="").geturl() == redirect
    query = parse_qs(target.query)
    assert query["state"] == ["native-state"]
    assert query["error"] == ["access_denied"]
    assert "code" not in query
    assert oidc_login.forms == []
    assert oidc_login.client.cookies.get(oidc_login.cookie) is None
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None


def test_empty_error_with_code_does_not_exchange_or_mint_session(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login()
    response = oidc_login.client.get(
        f"{oidc_login.prefix}/callback",
        params={"state": original["state"], "error": "", "code": "test-code"},
    )
    assert response.status_code == 400
    assert oidc_login.forms == []
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None


def test_cli_ticket_survives_retry_and_is_single_use(oidc_login: _OidcLogin) -> None:
    ticket = oidc_login.client.post(f"{oidc_login.prefix}/cli-login").json()["ticket"]
    _, original = oidc_login.login(ticket=ticket)
    retry = oidc_login.expired(original["state"])
    assert retry.status_code == 302
    retried = oidc_login.state()
    assert retried["state"] != original["state"]
    assert retried["ticket"] == ticket
    assert (
        oidc_login.client.get(
            f"{oidc_login.prefix}/cli-poll", params={"ticket": ticket}
        ).status_code
        == 202
    )
    response = oidc_login.finish(retried["state"])
    assert response.status_code == 200
    assert "Login successful" in response.text
    poll = oidc_login.client.get(f"{oidc_login.prefix}/cli-poll", params={"ticket": ticket})
    assert poll.status_code == 200
    assert poll.json()["user_id"] == "user@example.test"
    assert (
        oidc_login.client.get(
            f"{oidc_login.prefix}/cli-poll", params={"ticket": ticket}
        ).status_code
        == 410
    )


def test_manual_recovery_preserves_cli_ticket_and_destination(oidc_login: _OidcLogin) -> None:
    ticket = oidc_login.client.post(f"{oidc_login.prefix}/cli-login").json()["ticket"]
    _, original = oidc_login.login(ticket=ticket, return_to="/sessions/test")
    oidc_login.expired(original["state"])
    response = oidc_login.expired(oidc_login.state()["state"])
    link = oidc_login.recovery_link(response)
    assert parse_qs(urlsplit(link).query)["ticket"] == [ticket]
    assert oidc_login.client.get(link).status_code == 302
    fresh = oidc_login.state()
    assert "oidc_retry" not in fresh
    assert fresh["return_to"] == "/sessions/test"
    assert oidc_login.finish(fresh["state"]).status_code == 200


def test_reauth_requirement_survives_retry(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login(reauth="1")
    oidc_login.replace_state({**original, "reauth_at": int(time.time()) - 60})
    response = oidc_login.expired(original["state"])
    retried = oidc_login.state()
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["0"]
    assert retried["reauth_at"] > int(time.time()) - 60
    oidc_login.claims["auth_time"] = int(time.time()) - 60
    assert oidc_login.finish(retried["state"]).status_code == 403
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None


def test_https_retry_preserves_host_cookie_attributes(oidc_login: _OidcLogin) -> None:
    oidc_login.build_client(secure=True)
    _, original = oidc_login.login()
    response = oidc_login.expired(original["state"])
    assert response.status_code == 302
    cookie = response.headers["set-cookie"]
    for part in ("__Host-ap_auth_state=", "HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert part in cookie
    response = oidc_login.expired(oidc_login.state()["state"])
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_manual_recovery_keeps_forced_reauthentication(oidc_login: _OidcLogin) -> None:
    _, original = oidc_login.login(reauth="1")
    oidc_login.expired(original["state"])
    response = oidc_login.expired(oidc_login.state()["state"])
    link = oidc_login.recovery_link(response)
    assert parse_qs(urlsplit(link).query)["reauth"] == ["1"]
    response = oidc_login.client.get(link)
    assert parse_qs(urlsplit(response.headers["location"]).query)["prompt"] == ["login"]
    oidc_login.claims["auth_time"] = int(time.time())
    assert oidc_login.finish(oidc_login.state()["state"]).status_code == 302


def test_expired_cli_ticket_is_not_revived_by_manual_recovery(
    oidc_login: _OidcLogin, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = oidc_login.client.post(f"{oidc_login.prefix}/cli-login").json()["ticket"]
    _, original = oidc_login.login(ticket=ticket)
    oidc_login.expired(original["state"])
    response = oidc_login.expired(oidc_login.state()["state"])
    link = oidc_login.recovery_link(response)
    expired_at = time.time() + 301
    with monkeypatch.context() as clock:
        clock.setattr("omnigent.server.routes.auth.time.time", lambda: expired_at)
        response = oidc_login.client.get(
            f"{oidc_login.prefix}/cli-poll", params={"ticket": ticket}
        )
    assert response.status_code == 410
    assert oidc_login.client.get(link).status_code == 302
    assert oidc_login.state()["ticket"] == ticket
    assert oidc_login.finish(oidc_login.state()["state"]).status_code == 302
    assert (
        oidc_login.client.get(
            f"{oidc_login.prefix}/cli-poll", params={"ticket": ticket}
        ).status_code
        == 410
    )


def test_subpath_retry_and_recovery_stay_under_mount(oidc_login: _OidcLogin) -> None:
    oidc_login.build_client(base_path="/proxy/42")
    _, original = oidc_login.login()
    oidc_login.expired(original["state"])
    retried = oidc_login.state()
    assert retried["return_to"] == "/proxy/42/"
    response = oidc_login.expired(retried["state"])
    assert urlsplit(oidc_login.recovery_link(response)).path == "/proxy/42/auth/login"


def test_invite_survives_retry_without_entering_idp_url(oidc_login: _OidcLogin) -> None:
    oidc_login.build_client(invites=True)
    _, original = oidc_login.login(invite="test-only-invite")
    response = oidc_login.expired(original["state"])
    assert oidc_login.state()["invite"] == "test-only-invite"
    assert "test-only-invite" not in response.headers["location"]
    response = oidc_login.expired(oidc_login.state()["state"])
    assert parse_qs(urlsplit(oidc_login.recovery_link(response)).query)["invite"] == [
        "test-only-invite"
    ]


@pytest.mark.parametrize(
    "destination", ("https://evil.example", "//evil.example", "/\\evil.example", '/<script>"')
)
def test_unsafe_destinations_are_sanitized_on_retry_and_recovery(
    oidc_login: _OidcLogin, destination: str
) -> None:
    _, original = oidc_login.login()
    oidc_login.replace_state({**original, "return_to": destination})
    oidc_login.expired(original["state"])
    retried = oidc_login.state()
    assert not retried["return_to"].startswith(("https:", "//", "/\\"))
    response = oidc_login.expired(retried["state"])
    assert urlsplit(oidc_login.recovery_link(response)).netloc == ""
    assert "<script>" not in response.text


def test_auth_redirects_and_failure_pages_are_not_cached(oidc_login: _OidcLogin) -> None:
    response, original = oidc_login.login()
    responses = [
        response,
        oidc_login.expired(original["state"]),
        oidc_login.expired(oidc_login.state()["state"]),
    ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "redirect", ["http://127.0.0.1:53682/callback", "ai.omnigent.ios:/oauth/callback"]
)
@pytest.mark.parametrize(
    ("kind", "category"),
    [
        ("missing_state", "missing_state"),
        ("missing_cookie", "missing_cookie"),
        ("signature", "invalid_or_expired_cookie"),
        ("expired", "invalid_or_expired_cookie"),
        ("mismatch", "state_mismatch"),
    ],
)
def test_unverified_native_error_guides_app_retry_without_leaking_context(
    oidc_login: _OidcLogin,
    caplog: pytest.LogCaptureFixture,
    redirect: str,
    kind: str,
    category: str,
) -> None:
    _, original = oidc_login.login(
        native_redirect_uri=redirect,
        native_state="private-native-state",
        code_challenge=derive_code_challenge("v" * 64),
        code_challenge_method="S256",
    )
    oidc_login.set_cookie_condition(kind)
    params = {
        "error": "access_denied",
        "error_description": "private-description",
        "native_redirect_uri": redirect,
        "native_state": "private-native-state",
        "code": "private-code",
    }
    if kind != "missing_state":
        params["state"] = "private-mismatch" if kind == "mismatch" else original["state"]
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        response = oidc_login.client.get(f"{oidc_login.prefix}/callback", params=params)
    oidc_login.assert_manual_restart(response)
    assert "If you started from the Omnigent app, return to it and try again." in response.text
    records = [record for record in caplog.records if record.name == _LOGGER]
    assert [(record.levelno, record.message) for record in records] == [
        (logging.INFO, f"OIDC provider callback could not be verified: {category}")
    ]
    assert "private-" not in response.text + str([record.message for record in records])
    assert redirect not in response.text
    assert original["state"] not in str([record.message for record in records])
    assert oidc_login.client.cookies.get(oidc_login.config.session_cookie_name) is None
    if kind in ("missing_state", "mismatch"):
        assert "set-cookie" not in response.headers
        assert oidc_login.state() == original
    else:
        assert oidc_login.client.cookies.get(oidc_login.cookie) is None
