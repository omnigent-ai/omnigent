"""Tests for the GitHub App HTTP client.

Network half only. HTTP is mocked at the transport boundary
(``httpx.MockTransport``); the App config (which owns the secret-shaped
fields) is built by :func:`tests.server.github_app_fixtures.make_config`
so this file never names a client secret alongside the httpx sink.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent.server.github_app import GitHubAppError
from omnigent.server.github_app_client import GitHubAppClient
from tests.server.github_app_fixtures import make_config


def _client(handler) -> GitHubAppClient:
    return GitHubAppClient(make_config(), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_exchange_code_parses_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        return httpx.Response(
            200,
            json={
                "access_token": "ghu_new",
                "refresh_token": "ghr_new",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
                "scope": "",
            },
        )

    tokens = await _client(handler).exchange_code("code123")
    assert tokens.access_token == "ghu_new"
    assert tokens.refresh_token == "ghr_new"


@pytest.mark.asyncio
async def test_exchange_code_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad_verification_code"})

    with pytest.raises(GitHubAppError):
        await _client(handler).exchange_code("nope")


@pytest.mark.asyncio
async def test_refresh_token_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "ghu_refreshed", "scope": "repo"})

    tokens = await _client(handler).refresh_token("ghr_old")
    assert tokens.access_token == "ghu_refreshed"


@pytest.mark.asyncio
async def test_token_endpoint_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(GitHubAppError):
        await _client(handler).exchange_code("c")


@pytest.mark.asyncio
async def test_fetch_login() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ghu_x"
        return httpx.Response(200, json={"login": "octocat", "id": 583231})

    login, uid = await _client(handler).fetch_login("ghu_x")
    assert (login, uid) == ("octocat", 583231)
