"""Tests for the in-process service identity (#6) in ``get_user_id``.

A same-process service (the scheduler's fire callback) authenticates by
presenting the per-boot secret from ``app.state.service_identity_token`` plus
an acting-user header. These pin the accept side: correct token → acts as the
named user on ANY auth mode; wrong/missing/malformed token → falls through to
the normal auth provider; unarmed app → headers are inert.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from omnigent.server.auth import SERVICE_ACTING_USER_HEADER, SERVICE_TOKEN_HEADER
from omnigent.server.routes._auth_helpers import get_user_id

_TOKEN = "boot-token-123"


class _CookieModeProvider:
    """Simulates a cookie/OIDC provider with no session: identity headers are
    ignored and an unauthenticated request resolves to None."""

    def get_user_id(self, request: Request) -> str | None:
        del request
        return None


def _app(*, armed: bool, provider: object | None) -> TestClient:
    app = FastAPI()
    if armed:
        app.state.service_identity_token = _TOKEN

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str | None]:
        return {"user": get_user_id(request, provider)}  # type: ignore[arg-type]

    return TestClient(app)


def _svc(token: str, acting: str = "alice@example.com") -> dict[str, str]:
    return {SERVICE_TOKEN_HEADER: token, SERVICE_ACTING_USER_HEADER: acting}


def test_service_identity_authenticates_on_cookie_mode() -> None:
    # The whole point of S1: a deployment whose provider ignores identity
    # headers (cookie/OIDC) still authenticates an in-process fire.
    client = _app(armed=True, provider=_CookieModeProvider())
    res = client.get("/whoami", headers=_svc(_TOKEN))
    assert res.json() == {"user": "alice@example.com"}


def test_wrong_or_partial_service_headers_fall_through() -> None:
    client = _app(armed=True, provider=_CookieModeProvider())
    # Wrong secret → normal auth (which here resolves to None).
    assert client.get("/whoami", headers=_svc("nope")).json() == {"user": None}
    # Token without an acting user → falls through.
    assert client.get("/whoami", headers={SERVICE_TOKEN_HEADER: _TOKEN}).json() == {"user": None}
    # No service headers at all → falls through.
    assert client.get("/whoami").json() == {"user": None}


def test_non_ascii_token_fails_cleanly() -> None:
    # Compared as bytes: a hostile non-ASCII header must fall through, not 500.
    # httpx refuses non-ASCII str header values, so send the raw latin-1 bytes
    # a hostile client would put on the wire (starlette decodes them latin-1).
    client = _app(armed=True, provider=None)
    res = client.get(
        "/whoami",
        headers={
            SERVICE_TOKEN_HEADER.encode(): "café".encode("latin-1"),
            SERVICE_ACTING_USER_HEADER.encode(): b"alice@example.com",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"user": None}


def test_unarmed_app_ignores_service_headers() -> None:
    # No secret on app.state (the feature isn't wired) → headers are inert.
    client = _app(armed=False, provider=None)
    assert client.get("/whoami", headers=_svc(_TOKEN)).json() == {"user": None}


def test_normal_auth_still_wins_without_service_headers() -> None:
    class _Provider:
        def get_user_id(self, request: Request) -> str | None:
            del request
            return "cookie-user@example.com"

    client = _app(armed=True, provider=_Provider())
    assert client.get("/whoami").json() == {"user": "cookie-user@example.com"}
