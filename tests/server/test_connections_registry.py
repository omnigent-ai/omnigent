"""A git provider defined outside omnigent wires its connection into the server.

The fake ``gitlab`` descriptor is registered at run time and its connection facet
module is injected into ``sys.modules``, so nothing in omnigent names it.
"""

from __future__ import annotations

import ast
import logging
import os
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from omnigent.connections.databricks import DatabricksConnectionStore
from omnigent.connections.github import GithubConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.git_providers import (
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    register_provider,
    reset_for_tests,
)
from omnigent.git_providers.github import PROVIDER as GITHUB_PROVIDER
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import connections_registry
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.connections_registry import connection_providers
from omnigent.server.databricks_app import DatabricksConfig
from omnigent.server.git_providers import (
    ConnectionFacet,
    connection_facets,
    connections_from_env,
)
from omnigent.server.git_providers.github import CONNECTION as GITHUB_CONNECTION
from omnigent.server.git_providers.github import GitHubConnection
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.routes.connections_base import ConnectStart, create_connection_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.github_app_fixtures import make_config

_FACET_MODULE = "tests_server_fake_gitlab_connection"
# Declared but never created: the server must not import a credential facet.
_CREDENTIAL_MODULE = "tests_server_fake_gitlab_credential"
_HOST = "gitlab.example.test"
_CONFIG = {"instance": _HOST}
_OWNER = "alice@example.com"
_HOST_ID = "a828988dc0b441fb8d04dad3761773b9"
_HEADERS = {MANAGED_HOST_TOKEN_HEADER: "launch-tok"}
_LOGGER = "omnigent.server.git_providers"
# A token-shaped value that a broken facet puts in its exception text.
_LEAK = "glpat-do-not-log"


class _PlainCipher:
    """Keeps secrets as given; these tests only need a cipher that round-trips."""

    def encrypt(self, plaintext: str, *, context: Any) -> str:
        return plaintext

    def decrypt(self, ciphertext: str, *, context: Any) -> str | None:
        return ciphertext


@dataclass(frozen=True)
class FakeGitLab:
    """Claims ``gitlab.example.test``; its connection facet is the injected module."""

    facets: FacetModules = field(default_factory=lambda: FacetModules(connection=_FACET_MODULE))
    id: str = "gitlab"
    display_name: str = "GitLab"
    default_hosts: tuple[str, ...] = (_HOST,)

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host == _HOST

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        return None

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        return None


@dataclass
class _Link:
    token: str
    created_at: int = 0


class FakeGitLabStore:
    """In-memory per-user links with the ``get`` and ``delete`` the shared routes call."""

    def __init__(self) -> None:
        self.links: dict[str, _Link] = {}

    def get(self, user_id: str, *, with_tokens: bool = False) -> _Link | None:
        return self.links.get(user_id)

    def delete(self, user_id: str) -> bool:
        return self.links.pop(user_id, None) is not None


class _FakeGitLabHooks:
    """The GitLab half of the shared connect, status, and disconnect flow."""

    provider = "gitlab"

    def __init__(self, store: FakeGitLabStore) -> None:
        self.store = store

    def signing_key(self) -> bytes:
        return b"gitlab-state-signing-key"

    def status_fields(self, connection: _Link | None) -> dict[str, Any]:
        return {"instance": _HOST}

    def begin(self, request: Request, build_state: Any) -> ConnectStart | None:
        return ConnectStart(f"https://{_HOST}/oauth/authorize?state={build_state({})}")

    async def complete(self, user_id: str, code: str, claims: dict[str, Any]) -> None:
        self.store.links[user_id] = _Link(token=f"glpat-{code}")


class FakeGitLabConnection:
    """A connection facet that records the stores the server asks it to build."""

    repo_browser = False

    def __init__(self) -> None:
        self.config_error: Exception | None = None
        self.store_error: Exception | None = None
        self.stores: list[tuple[str, object]] = []

    def config_from_env(self) -> dict[str, str] | None:
        if self.config_error is not None:
            raise self.config_error
        return _CONFIG

    def make_store(self, db_uri: str, cipher: object) -> FakeGitLabStore:
        if self.store_error is not None:
            raise self.store_error
        self.stores.append((db_uri, cipher))
        return FakeGitLabStore()

    def make_client(self, config: object) -> dict[str, object]:
        return {"client_for": config}

    def make_router(
        self, config: object, store: FakeGitLabStore, *, auth_provider: Any, client: object
    ) -> APIRouter:
        return create_connection_router(_FakeGitLabHooks(store), auth_provider=auth_provider)

    async def resolve_credential(
        self, user_id: str, *, store: FakeGitLabStore, client: object
    ) -> dict[str, object] | None:
        link = store.get(user_id, with_tokens=True)
        if link is None:
            return None
        return {"username": "oauth2", "token": link.token, "expires_at": None, "hosts": [_HOST]}


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start and end each test with the built-in providers and no GitHub App env."""
    monkeypatch.delenv("OMNIGENT_GIT_PROVIDER_MODULES", raising=False)
    for name in list(os.environ):
        if name.startswith("OMNIGENT_GITHUB_APP_"):
            monkeypatch.delenv(name)
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def gitlab(monkeypatch: pytest.MonkeyPatch) -> FakeGitLabConnection:
    """Register the fake provider and inject its connection facet module."""
    facet = FakeGitLabConnection()
    module = types.ModuleType(_FACET_MODULE)
    module.CONNECTION = facet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _FACET_MODULE, module)
    register_provider(FakeGitLab())
    return facet


def _app(db_uri: str, tmp_path: Path, **kwargs: Any) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        **kwargs,
    )


def _register_managed_host(app: FastAPI) -> None:
    """Add a managed host for ``_OWNER`` that authenticates with ``_HEADERS``."""
    app.state.host_store.register_managed_host(
        host_id=_HOST_ID,
        name="managed-gitlab",
        user_id=_OWNER,
        token=_HEADERS[MANAGED_HOST_TOKEN_HEADER],
        provider="modal",
        sandbox_id="sb-gitlab",
        token_expires_at=now_epoch() + 3600,
    )


def test_the_fake_and_github_facets_satisfy_the_protocol(gitlab: FakeGitLabConnection) -> None:
    assert isinstance(gitlab, ConnectionFacet)
    assert isinstance(GITHUB_CONNECTION, ConnectionFacet)
    assert GITHUB_CONNECTION.repo_browser is True


def test_connection_facets_follow_registration_order(gitlab: FakeGitLabConnection) -> None:
    assert list(connection_facets()) == [("github", GITHUB_CONNECTION), ("gitlab", gitlab)]


def test_connection_providers_list_the_facets_then_databricks(
    gitlab: FakeGitLabConnection,
) -> None:
    providers = connection_providers()

    assert [provider.name for provider in providers] == ["github", "gitlab", "databricks"]
    fake = providers[1]
    assert (fake.client_factory, fake.router_factory, fake.credential_resolver) == (
        gitlab.make_client,
        gitlab.make_router,
        gitlab.resolve_credential,
    )


def test_a_configured_fake_is_enabled_and_served(
    gitlab: FakeGitLabConnection, db_uri: str, tmp_path: Path
) -> None:
    store = FakeGitLabStore()
    store.links[RESERVED_USER_LOCAL] = _Link(token="glpat-local")
    store.links[_OWNER] = _Link(token="glpat-owner")
    app = _app(db_uri, tmp_path, connections={"gitlab": (_CONFIG, store)})
    _register_managed_host(app)
    client = TestClient(app)

    info = client.get("/v1/info").json()
    status = client.get("/v1/connections/gitlab/status")
    credential = client.get(f"/v1/hosts/{_HOST_ID}/credentials/gitlab", headers=_HEADERS)

    assert info["enabled_connections"] == ["gitlab"]
    assert (app.state.gitlab_config, app.state.gitlab_store) == (_CONFIG, store)
    assert app.state.gitlab_client == {"client_for": _CONFIG}
    assert status.status_code == 200
    assert status.json() == {
        "enabled": True,
        "connected": True,
        "connected_at": 0,
        "instance": _HOST,
    }
    assert credential.status_code == 200
    assert credential.json() == {
        "connected": True,
        "owner": _OWNER,
        "username": "oauth2",
        "token": "glpat-owner",
        "expires_at": None,
        "hosts": [_HOST],
    }


def test_a_fake_without_a_store_stays_disabled(
    gitlab: FakeGitLabConnection, db_uri: str, tmp_path: Path
) -> None:
    # A config without a store (the credential store has no cipher) stays off.
    app = _app(db_uri, tmp_path, connections={"gitlab": (_CONFIG, None)})
    _register_managed_host(app)
    client = TestClient(app)

    info = client.get("/v1/info").json()
    status = client.get("/v1/connections/gitlab/status")
    credential = client.get(f"/v1/hosts/{_HOST_ID}/credentials/gitlab", headers=_HEADERS)

    assert info["enabled_connections"] == []
    assert (app.state.gitlab_config, app.state.gitlab_client) == (None, None)
    assert status.status_code == 404
    assert credential.status_code == 404
    assert credential.json() == {"detail": "unknown credential provider"}


def test_enabled_connections_keep_the_registry_order(
    gitlab: FakeGitLabConnection, db_uri: str, tmp_path: Path
) -> None:
    databricks = DatabricksConfig(
        "dbx-client", "dbx-key", "https://x/v1/connections/databricks/callback", "all-apis"
    )
    app = _app(
        db_uri,
        tmp_path,
        # The mapping order does not matter; the registry order does.
        connections={
            "gitlab": (_CONFIG, FakeGitLabStore()),
            "github": (make_config(), GithubConnectionStore(db_uri, _PlainCipher())),
        },
        databricks_config=databricks,
        databricks_store=DatabricksConnectionStore(db_uri, _PlainCipher()),
    )

    info = TestClient(app).get("/v1/info").json()

    assert info["enabled_connections"] == ["github", "gitlab", "databricks"]


def test_info_offers_the_github_connection_and_repo_picker(db_uri: str, tmp_path: Path) -> None:
    store = GithubConnectionStore(db_uri, _PlainCipher())
    app = _app(db_uri, tmp_path, connections={"github": (make_config(), store)})

    info = TestClient(app).get("/v1/info").json()
    github, azure_devops = info["git_providers"]

    assert info["enabled_connections"] == ["github"]
    assert github == {
        "id": "github",
        "display_name": GITHUB_PROVIDER.display_name,
        "capabilities": {
            "pull_requests": True,
            "connection": True,
            "repo_browser": True,
            # True once the GitHub descriptor declares its credential facet.
            "credential_broker": bool(GITHUB_PROVIDER.facets.credential),
        },
    }
    assert azure_devops["id"] == "azure_devops"
    assert azure_devops["capabilities"]["connection"] is False


@pytest.mark.parametrize("configured", [True, False])
def test_info_describes_a_fake_provider_from_its_descriptor(
    configured: bool, gitlab: FakeGitLabConnection, db_uri: str, tmp_path: Path
) -> None:
    descriptor = FakeGitLab(
        facets=FacetModules(connection=_FACET_MODULE, credential=_CREDENTIAL_MODULE)
    )
    register_provider(descriptor)
    store = FakeGitLabStore() if configured else None
    app = _app(db_uri, tmp_path, connections={"gitlab": (_CONFIG, store)})

    providers = TestClient(app).get("/v1/info").json()["git_providers"]

    assert [provider["id"] for provider in providers] == ["github", "azure_devops", "gitlab"]
    assert providers[-1] == {
        "id": "gitlab",
        "display_name": descriptor.display_name,
        "capabilities": {
            "pull_requests": False,
            "connection": configured,
            "repo_browser": False,
            "credential_broker": configured,
        },
    }
    assert _CREDENTIAL_MODULE not in sys.modules


def test_the_deprecated_github_arguments_still_enable_github(db_uri: str, tmp_path: Path) -> None:
    config = make_config()
    store = GithubConnectionStore(db_uri, _PlainCipher())

    with pytest.warns(DeprecationWarning, match=r"0\.19\.0"):
        app = _app(db_uri, tmp_path, github_config=config, github_store=store)
    client = TestClient(app)
    info = client.get("/v1/info").json()
    status = client.get("/v1/connections/github/status")

    assert info["enabled_connections"] == ["github"]
    assert app.state.github_config is config
    assert app.state.github_store is store
    assert status.status_code == 200
    assert status.json()["install_url"] == "https://github.com/apps/omni-app/installations/new"


def test_github_in_connections_and_in_the_deprecated_arguments_is_rejected(
    db_uri: str, tmp_path: Path
) -> None:
    config = make_config()
    store = GithubConnectionStore(db_uri, _PlainCipher())

    with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match="not both"):
        _app(db_uri, tmp_path, connections={"github": (config, store)}, github_config=config)


def test_the_github_credential_carries_expiry_and_hosts(db_uri: str, tmp_path: Path) -> None:
    store = GithubConnectionStore(db_uri, _PlainCipher())
    expires_at = now_epoch() + 3600
    store.upsert(
        _OWNER,
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_live", "ghr_live", expires_at, None, "repo"),
    )
    app = _app(db_uri, tmp_path, connections={"github": (make_config(), store)})
    _register_managed_host(app)

    resp = TestClient(app).get(f"/v1/hosts/{_HOST_ID}/credentials/github", headers=_HEADERS)

    assert resp.status_code == 200
    assert resp.json() == {
        "connected": True,
        "owner": _OWNER,
        "username": "x-access-token",
        "token": "ghu_live",
        "login": "octocat",
        "expires_at": expires_at,
        "hosts": ["github.com"],
    }


def test_connections_from_env_builds_each_store_with_one_cipher(
    gitlab: FakeGitLabConnection, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(GitHubConnection, "config_from_env", lambda self: make_config())
    cipher = _PlainCipher()
    calls: list[None] = []

    def cipher_factory() -> _PlainCipher:
        calls.append(None)
        return cipher

    connections = connections_from_env(db_uri, cipher_factory=cipher_factory)

    assert list(connections) == ["github", "gitlab"]
    github_config, github_store = connections["github"]
    assert github_config == make_config()
    assert isinstance(github_store, GithubConnectionStore)
    assert connections["gitlab"][0] == _CONFIG
    assert isinstance(connections["gitlab"][1], FakeGitLabStore)
    assert gitlab.stores == [(db_uri, cipher)]
    assert len(calls) == 1


def test_connections_from_env_builds_no_cipher_when_nothing_is_configured(
    db_uri: str,
) -> None:
    def cipher_factory() -> None:
        raise AssertionError("no provider is configured, so no cipher is needed")

    assert connections_from_env(db_uri, cipher_factory=cipher_factory) == {}


def test_connections_from_env_keeps_the_config_without_a_cipher(
    gitlab: FakeGitLabConnection, db_uri: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        connections = connections_from_env(db_uri, cipher_factory=lambda: None)

    assert connections == {"gitlab": (_CONFIG, None)}
    assert gitlab.stores == []
    assert "GitLab is configured but disabled" in caplog.text


@pytest.mark.parametrize("failing", ["config_error", "store_error"])
def test_a_misconfigured_facet_stops_startup(
    failing: str, gitlab: FakeGitLabConnection, db_uri: str
) -> None:
    # A misconfigured provider must fail startup, not end up silently disabled.
    setattr(gitlab, failing, RuntimeError("unreadable key file"))

    with pytest.raises(RuntimeError, match="unreadable key file"):
        connections_from_env(db_uri, cipher_factory=_PlainCipher)


@pytest.mark.parametrize(
    ("module_name", "source", "reason"),
    [
        ("tests_server_facet_raises", f"raise RuntimeError('token {_LEAK}')\n", "RuntimeError"),
        (
            "tests_server_facet_missing_dependency",
            "import tests_server_no_such_dependency\n",
            "ModuleNotFoundError",
        ),
        ("tests_server_facet_not_a_facet", "CONNECTION = object()\n", "ConnectionFacet"),
    ],
)
def test_a_facet_that_does_not_load_is_logged_and_skipped(
    module_name: str,
    source: str,
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    register_provider(FakeGitLab(facets=FacetModules(connection=module_name)))
    try:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            names = [provider.name for provider in connection_providers()]
    finally:
        sys.modules.pop(module_name, None)

    assert names == ["github", "databricks"]
    assert "gitlab" in caplog.text
    assert reason in caplog.text
    assert _LEAK not in caplog.text


def test_the_registry_names_no_git_provider() -> None:
    tree = ast.parse(Path(connections_registry.__file__).read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "github" not in literals
