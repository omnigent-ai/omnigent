"""The client command surface must renew an idle-expired login token.

After ``omnigent login``, the short-lived access token can lapse while the
CLI sits idle with no turn. The next command builds its auth through
``omnigent.chat._remote_headers`` / ``_DatabricksTokenAuth``; those paths
must renew from the stored (still-valid) refresh grant instead of going out
with the expired bearer, 401-ing, and demanding a needless re-login — the
same renewal an unattended runner already performs via
``omnigent.runner._entry``.
"""

from __future__ import annotations

import time

import httpx
import pytest

_SERVER_URL = "http://localhost:6767"


@pytest.fixture()
def expired_login(tmp_path, monkeypatch):
    """Store an idle-expired token entry with a valid refresh grant.

    Redirects the token file to a temp dir, clears any ambient static
    bearer (which would short-circuit the stored-token branch), and stubs
    the ``/oauth/token`` exchange to succeed — so a renewed bearer proves
    the client path actually attempted the refresh.

    :param tmp_path: Pytest temp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The temp directory holding ``auth_tokens.json``.
    """
    from omnigent import cli_auth

    monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    cli_auth.store_token(
        _SERVER_URL,
        token="stale",
        user_id="a@x",
        expires_at=time.time() - 10,
        refresh_token="refresh-1",
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **_kw: httpx.Response(
            200,
            json={"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 3600},
            request=httpx.Request("POST", url),
        ),
    )
    return tmp_path


def test_remote_headers_renew_idle_expired_token(expired_login) -> None:
    """Ad-hoc command headers (``omnigent usage`` et al.) carry a renewed
    bearer after an idle expiry, not the stale one and not nothing."""
    from omnigent.chat import _remote_headers

    headers = _remote_headers(server_url=_SERVER_URL, host_id=None)
    assert headers.get("Authorization") == "Bearer fresh"


def test_auth_flow_renews_idle_expired_token(expired_login) -> None:
    """The long-lived httpx Auth renews per request after an idle expiry."""
    from omnigent.chat import _DatabricksTokenAuth

    auth = _DatabricksTokenAuth(server_url=_SERVER_URL, session_id=None)
    flow = auth.auth_flow(httpx.Request("GET", f"{_SERVER_URL}/v1/usage"))
    request = next(flow)
    assert request.headers.get("Authorization") == "Bearer fresh"


def test_server_auth_treats_expired_login_as_credential(expired_login) -> None:
    """An expired-but-refreshable login still yields an Auth instance —
    otherwise the client is built with no auth and 401s before
    ``auth_flow`` ever gets the chance to renew."""
    from omnigent.chat import _server_auth

    auth = _server_auth(_SERVER_URL, session_id=None)
    assert auth is not None
