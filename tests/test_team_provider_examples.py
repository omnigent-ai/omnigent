"""Provider contracts exercised through real HTTP adapters with recorded responses."""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "examples/team-providers/src")
    )
    monkeypatch.setenv("OMNIGENT_OKTA_ORG_URL", "https://example.okta.com/")
    monkeypatch.setenv("OMNIGENT_OKTA_API_TOKEN", "test-only-okta-token")
    monkeypatch.setenv("OMNIGENT_GOOGLE_GROUPS_ACCESS_TOKEN", "test-only-google-token")
    return importlib.import_module("omnigent_team_providers")


async def test_okta_maps_authenticated_login_to_stable_group_ids(providers):
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path == b"/api/v1/users/alice%2Bdev%40example.com/groups"
        assert request.url.host == "example.okta.com"
        assert request.headers["authorization"] == "SSWS test-only-okta-token"
        return httpx.Response(200, json=[{"id": "00g-engineering", "profile": {"name": "Eng"}}])

    provider = providers.OktaTeamProvider(transport=httpx.MockTransport(respond))
    teams = await provider.list_teams("alice+dev@example.com")
    assert [(team.id, team.name) for team in teams] == [("00g-engineering", "Eng")]


async def test_google_uses_fixed_endpoint_and_reads_every_page(providers):
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "admin.googleapis.com"
        assert request.url.path == "/admin/directory/v1/groups"
        assert request.url.params["userKey"] == "alice+dev@example.com"
        assert request.headers["authorization"] == "Bearer test-only-google-token"
        seen.append(dict(request.url.params))
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "groups": [{"id": "123", "name": "Engineering"}],
                    "nextPageToken": "opaque/../?token#",
                },
            )
        assert request.url.params["pageToken"] == "opaque/../?token#"
        return httpx.Response(200, json={"groups": [{"id": "456", "email": "ops@example.com"}]})

    provider = providers.GoogleTeamProvider(transport=httpx.MockTransport(respond))
    teams = await provider.list_teams("alice+dev@example.com")
    assert len(seen) == 2
    assert [(team.id, team.name) for team in teams] == [
        ("123", "Engineering"),
        ("456", "ops@example.com"),
    ]


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.okta.com",
        "https:///example.okta.com",
        "https://user@example.okta.com",
        "https://user:password@example.okta.com",
        "https://example.okta.com/path",
        "https://example.okta.com?query=1",
        "https://example.okta.com#fragment",
    ],
)
def test_okta_requires_an_https_origin(providers, monkeypatch, origin):
    monkeypatch.setenv("OMNIGENT_OKTA_ORG_URL", origin)
    with pytest.raises(ValueError, match="HTTPS origin"):
        providers.OktaTeamProvider()


@pytest.mark.parametrize("provider_name", ["OktaTeamProvider", "GoogleTeamProvider"])
@pytest.mark.parametrize("status", [302, 401, 429, 503])
async def test_provider_errors_never_become_empty_membership(providers, provider_name, status):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example/groups"})

    provider = getattr(providers, provider_name)(transport=httpx.MockTransport(respond))
    with pytest.raises(httpx.HTTPStatusError):
        await provider.list_teams("alice@example.com")
    assert len(requests) == 1


async def test_google_does_not_return_a_partial_list(providers):
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"groups": [{"id": str(calls), "name": "Eng"}], "nextPageToken": "again"},
        )

    provider = providers.GoogleTeamProvider(transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError, match="pagination limit"):
        await provider.list_teams("alice@example.com")
    assert calls == 25


@pytest.mark.parametrize(
    ("provider_name", "payload"),
    [
        ("OktaTeamProvider", {}),
        ("OktaTeamProvider", [{"id": 123, "profile": {"name": "Eng"}}]),
        ("GoogleTeamProvider", {"groups": {}}),
        ("GoogleTeamProvider", {"nextPageToken": False}),
    ],
)
async def test_malformed_responses_fail(providers, provider_name, payload):
    provider = getattr(providers, provider_name)(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    with pytest.raises(ValueError):
        await provider.list_teams("alice@example.com")


async def test_unexpected_okta_pagination_is_an_error(providers):
    provider = providers.OktaTeamProvider(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=[], headers={"Link": '<https://other.example/groups>; rel="next"'}
            )
        )
    )
    with pytest.raises(ValueError, match="Unexpected pagination"):
        await provider.list_teams("alice@example.com")
