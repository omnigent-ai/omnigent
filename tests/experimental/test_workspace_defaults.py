"""Owner-scoped default branch metadata and soft generic fallback."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.connections.github import GithubConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.experimental.workspace_profiles import defaults
from omnigent.experimental.workspace_profiles.defaults import GitHubDefaultBranchResolver
from omnigent.server import github_identity
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.github_app_client import GitHubAppClient

_REPO = "https://github.com/acme/application.git"
_OTHER = "https://github.com/acme/library.git"
_OWNER = "owner@example.invalid"


def _configured(
    transport: httpx.MockTransport,
) -> tuple[GitHubDefaultBranchResolver, MagicMock, MagicMock]:
    store = MagicMock(spec=GithubConnectionStore)
    store.get.return_value = SimpleNamespace(
        access_token="test-owner-token", refresh_token=None, token_expires_at=None
    )
    client = MagicMock(spec=GitHubAppClient)
    resolver = GitHubDefaultBranchResolver(transport=transport)
    resolver.configure(store, client)
    return resolver, store, client


def test_metadata_uses_owner_connection_and_exact_canonical_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def metadata(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://api.github.com/repos/acme/application"
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-owner-token"
        return httpx.Response(200, json={"default_branch": "release/current"})

    resolver, store, client = _configured(httpx.MockTransport(metadata))
    identity = AsyncMock(wraps=github_identity.resolve_access_token)
    monkeypatch.setattr(github_identity, "resolve_access_token", identity)

    assert resolver(_OWNER, ["git@github.com:Acme/Application.git", _REPO]) == {
        _REPO: "release/current"
    }
    identity.assert_awaited_once_with(_OWNER, store=store, client=client)
    store.get.assert_called_once_with(_OWNER, with_tokens=True)
    assert len(requests) == 1


def test_existing_resolver_refreshes_expired_owner_token_before_metadata() -> None:
    def metadata(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer refreshed-test-token"
        return httpx.Response(200, json={"default_branch": "trunk"})

    resolver, store, client = _configured(httpx.MockTransport(metadata))
    store.get.return_value = SimpleNamespace(
        access_token="expired-test-token",
        refresh_token="test-refresh-token",
        token_expires_at=now_epoch() - 10,
    )
    tokens = GitHubTokenSet(
        "refreshed-test-token", "new-refresh", now_epoch() + 3600, None, "repo"
    )
    client.refresh_token.return_value = tokens

    assert resolver(_OWNER, [_REPO]) == {_REPO: "trunk"}
    client.refresh_token.assert_awaited_once_with("test-refresh-token")
    store.update_tokens.assert_called_once_with(_OWNER, tokens)


def test_unconfigured_or_unlinked_owner_never_queries_metadata() -> None:
    metadata = MagicMock(side_effect=AssertionError("An unlinked owner must not query GitHub"))
    transport = httpx.MockTransport(metadata)
    resolver = GitHubDefaultBranchResolver(transport=transport)
    assert resolver(_OWNER, [_REPO]) == {}

    resolver, store, client = _configured(transport)
    store.get.return_value = None
    assert resolver(_OWNER, [_REPO]) == {}
    resolver.configure(store, None)
    assert resolver(_OWNER, [_REPO]) == {}
    resolver.configure(None, client)
    assert resolver(_OWNER, [_REPO]) == {}
    metadata.assert_not_called()


def test_connection_failures_are_unresolved_without_logging_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metadata = MagicMock(side_effect=AssertionError("No metadata request expected"))
    resolver, store, _client = _configured(httpx.MockTransport(metadata))
    store.get.side_effect = RuntimeError("sensitive-test-error")

    assert resolver(_OWNER, [_REPO]) == {}
    assert "sensitive-test-error" not in caplog.text
    metadata.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 401, 403, 404, 429, 500])
def test_rejected_or_redirected_metadata_is_unresolved(status: int) -> None:
    requests = []

    def metadata(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://github.example.invalid/redirect"},
            json={"default_branch": "main"},
        )

    resolver, _store, _client = _configured(httpx.MockTransport(metadata))
    assert resolver(_OWNER, [_REPO]) == {}
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        None,
        {"default_branch": None},
        {"default_branch": 42},
        {"default_branch": ""},
        {"default_branch": "feature..main"},
        {"default_branch": "branch\nmain"},
        {"default_branch": "-main"},
    ],
)
def test_invalid_metadata_never_guesses_main(payload: object) -> None:
    resolver, _store, _client = _configured(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )
    assert resolver(_OWNER, [_REPO]) == {}


@pytest.mark.parametrize("failure", ["network", "timeout", "json"])
def test_lookup_failure_keeps_other_repository_metadata(failure: str) -> None:
    def metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("application"):
            if failure == "network":
                raise httpx.ConnectError("test network error")
            if failure == "timeout":
                raise httpx.ReadTimeout("test timeout")
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, json={"default_branch": "develop"})

    resolver, _store, _client = _configured(httpx.MockTransport(metadata))
    assert resolver(_OWNER, [_REPO, _OTHER]) == {_OTHER: "develop"}


def test_metadata_timeout_is_bounded_and_keeps_previous_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("library"):
            await asyncio.sleep(10)
        return httpx.Response(200, json={"default_branch": "trunk"})

    monkeypatch.setattr(defaults, "_REQUEST_TIMEOUT_S", 0.01)
    resolver, _store, _client = _configured(httpx.MockTransport(metadata))
    assert resolver(_OWNER, [_REPO, _OTHER]) == {_REPO: "trunk"}


def test_metadata_deadline_preserves_completed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("library"):
            await asyncio.sleep(10)
        return httpx.Response(200, json={"default_branch": "trunk"})

    monkeypatch.setattr(defaults, "_METADATA_TIMEOUT_S", 0.1)
    monkeypatch.setattr(
        github_identity, "resolve_access_token", AsyncMock(return_value="test-owner-token")
    )
    resolver, _store, _client = _configured(httpx.MockTransport(metadata))
    assert resolver(_OWNER, [_REPO, _OTHER]) == {_REPO: "trunk"}


def test_metadata_deadline_starts_after_existing_credential_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve_token(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return "test-owner-token"

    monkeypatch.setattr(defaults, "_METADATA_TIMEOUT_S", 0.01)
    monkeypatch.setattr(github_identity, "resolve_access_token", resolve_token)
    resolver, _store, _client = _configured(
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"default_branch": "trunk"}))
    )
    assert resolver(_OWNER, [_REPO]) == {_REPO: "trunk"}


def test_metadata_is_not_cached_across_owners_or_calls() -> None:
    requests = []

    def metadata(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["Authorization"]
        requests.append(authorization)
        if authorization == "Bearer first-owner-token":
            return httpx.Response(200, json={"default_branch": "production"})
        return httpx.Response(404)

    resolver, store, _client = _configured(httpx.MockTransport(metadata))
    store.get.side_effect = lambda owner, **_kwargs: SimpleNamespace(
        access_token=f"{owner}-token", refresh_token=None, token_expires_at=None
    )

    assert resolver("first-owner", [_REPO]) == {_REPO: "production"}
    assert resolver("second-owner", [_REPO]) == {}
    assert resolver("first-owner", [_REPO]) == {_REPO: "production"}
    assert requests == [
        "Bearer first-owner-token",
        "Bearer second-owner-token",
        "Bearer first-owner-token",
    ]


def test_unsupported_repository_urls_do_not_query_github() -> None:
    metadata = MagicMock(side_effect=AssertionError("No metadata request expected"))
    resolver, store, _client = _configured(httpx.MockTransport(metadata))

    assert (
        resolver(
            _OWNER,
            [
                "https://gitlab.com/acme/application",
                "https://token@github.com/acme/application",
                "https://github.com/acme/application?token=placeholder",
            ],
        )
        == {}
    )
    assert resolver("", [_REPO]) == {}
    assert resolver(_OWNER, []) == {}
    store.get.assert_not_called()
    metadata.assert_not_called()


def test_metadata_http_ignores_ambient_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:proxy-password@127.0.0.1:1")
    original_client = httpx.AsyncClient
    clients = []

    def http_client(**kwargs):
        clients.append(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr(defaults.httpx, "AsyncClient", http_client)
    resolver, _store, _client = _configured(
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"default_branch": "trunk"}))
    )

    assert resolver(_OWNER, [_REPO]) == {_REPO: "trunk"}
    assert len(clients) == 1
    assert clients[0]["trust_env"] is False
    assert clients[0]["follow_redirects"] is False
    assert 0 < clients[0]["timeout"] <= 5
