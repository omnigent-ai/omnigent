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


@pytest.mark.asyncio
async def test_list_repos_projects_fields_and_stops_on_short_page() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.path == "/user/repos"
        return httpx.Response(
            200,
            json=[
                {
                    "full_name": "caffeinelabs/app",
                    "clone_url": "https://github.com/caffeinelabs/app.git",
                    "default_branch": "main",
                    "private": True,
                    "pushed_at": "2026-07-28T00:00:00Z",
                    "stargazers_count": 3,
                },
                {"description": "no full_name — skipped"},
            ],
        )

    repos, truncated = await _client(handler).list_repos("ghu_x")
    # Short page (< per_page) → only one request, no over-fetch, not truncated.
    assert len(calls) == 1
    assert truncated is False
    # Only the projected keys survive; the entry missing full_name is dropped.
    assert repos == [
        {
            "full_name": "caffeinelabs/app",
            "clone_url": "https://github.com/caffeinelabs/app.git",
            "default_branch": "main",
            "private": True,
            "pushed_at": "2026-07-28T00:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_list_repos_flags_truncation_at_page_cap() -> None:
    # Every page comes back full → the page cap is hit and truncated=True so the
    # UI can say the list is partial instead of silently dropping repos.
    def handler(request: httpx.Request) -> httpx.Response:
        page = [
            {"full_name": f"o/r{i}", "clone_url": None, "default_branch": "main"}
            for i in range(100)  # a full per_page page
        ]
        return httpx.Response(200, json=page)

    repos, truncated = await _client(handler).list_repos("ghu_x")
    assert truncated is True
    assert len(repos) == 300  # 3 full pages (the cap)


@pytest.mark.asyncio
async def test_list_repos_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(GitHubAppError):
        await _client(handler).list_repos("ghu_bad")


@pytest.mark.asyncio
async def test_list_branches_returns_names_and_stops_on_short_page() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/repos/caffeinelabs/app/branches"
        return httpx.Response(
            200,
            json=[
                {"name": "main", "protected": True},
                {"name": "dev"},
                {"no_name": "skipped"},
            ],
        )

    branches = await _client(handler).list_branches("ghu_x", "caffeinelabs/app")
    # Short page (< per_page) → single request, entries without a name dropped.
    assert calls == ["/repos/caffeinelabs/app/branches"]
    assert branches == ["main", "dev"]


@pytest.mark.asyncio
async def test_list_branches_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubAppError):
        await _client(handler).list_branches("ghu_bad", "caffeinelabs/nope")
