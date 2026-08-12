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


@pytest.mark.asyncio
async def test_list_pulls_projects_fields_and_stops_on_short_page() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/repos/caffeinelabs/app/pulls"
        # All states so merged/closed PRs surface too.
        assert request.url.params.get("state") == "all"
        return httpx.Response(
            200,
            json=[
                {
                    "number": 7,
                    "title": "feat: thing",
                    "html_url": "https://github.com/caffeinelabs/app/pull/7",
                    "head": {"ref": "feat-thing"},
                    "user": {"login": "octocat"},
                    "draft": False,
                    "state": "closed",
                    "merged_at": "2026-07-29T02:00:00Z",
                    "created_at": "2026-07-29T00:00:00Z",
                    "body": "does the thing\n\n[Open in Omnigent](https://omni.example/c/conv_1)",
                    "extra": "ignored",
                },
                {"no_number": "skipped"},
            ],
        )

    pulls = await _client(handler).list_pulls("ghu_x", "caffeinelabs/app")
    assert calls == ["/repos/caffeinelabs/app/pulls"]
    # A merged PR: state closed + merged True, and it is still returned.
    assert pulls == [
        {
            "number": 7,
            "title": "feat: thing",
            "html_url": "https://github.com/caffeinelabs/app/pull/7",
            "head_ref": "feat-thing",
            "draft": False,
            "state": "closed",
            "merged": True,
            "author_login": "octocat",
            "created_at": "2026-07-29T00:00:00Z",
            "body": "does the thing\n\n[Open in Omnigent](https://omni.example/c/conv_1)",
        }
    ]


@pytest.mark.asyncio
async def test_list_pulls_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    with pytest.raises(GitHubAppError):
        await _client(handler).list_pulls("ghu_bad", "caffeinelabs/app")


@pytest.mark.asyncio
async def test_search_pulls_maps_search_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/issues"
        assert request.url.params.get("q") == "sess123 in:body type:pr author:octocat"
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    {
                        "number": 9,
                        "title": "via MCP",
                        "html_url": "https://github.com/caffeinelabs/other/pull/9",
                        "state": "closed",
                        "draft": False,
                        "user": {"login": "octocat"},
                        "created_at": "2026-07-29T03:00:00Z",
                        "body": "…/c/sess123",
                        "repository_url": "https://api.github.com/repos/caffeinelabs/other",
                        "pull_request": {"merged_at": "2026-07-29T04:00:00Z"},
                    },
                    {"no_number": "skipped"},
                ],
            },
        )

    pulls = await _client(handler).search_pulls("ghu_x", "sess123 in:body type:pr author:octocat")
    assert pulls == [
        {
            "number": 9,
            "title": "via MCP",
            "html_url": "https://github.com/caffeinelabs/other/pull/9",
            "head_ref": None,
            "draft": False,
            "state": "closed",
            "merged": True,  # from pull_request.merged_at
            "author_login": "octocat",
            "created_at": "2026-07-29T03:00:00Z",
            "body": "…/c/sess123",
            "repo": "caffeinelabs/other",  # parsed from repository_url
        }
    ]


@pytest.mark.asyncio
async def test_search_pulls_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Validation Failed"})

    with pytest.raises(GitHubAppError):
        await _client(handler).search_pulls("ghu_bad", "sess in:body type:pr")


@pytest.mark.asyncio
async def test_list_pull_commit_messages_extracts_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/caffeinelabs/app/pulls/7/commits"
        return httpx.Response(
            200,
            json=[
                {"commit": {"message": "feat: x\n\nOmnigent-Session: conv_abc"}},
                {"commit": {"message": "fix: y"}},
                {"no_commit": True},
            ],
        )

    msgs = await _client(handler).list_pull_commit_messages("ghu_x", "caffeinelabs/app", 7)
    assert msgs == ["feat: x\n\nOmnigent-Session: conv_abc", "fix: y"]
    assert any("Omnigent-Session: conv_abc" in m for m in msgs)


@pytest.mark.asyncio
async def test_list_pull_commit_messages_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with pytest.raises(GitHubAppError):
        await _client(handler).list_pull_commit_messages("ghu_bad", "caffeinelabs/app", 9)
