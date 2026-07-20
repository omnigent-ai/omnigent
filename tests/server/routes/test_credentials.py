"""Tests for the per-user credentials routes (``/v1/credentials``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from starlette.requests import HTTPConnection

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import AuthProvider
from omnigent.server.routes import credentials as credentials_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.credential_store import CredentialStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

_USER = "alice@example.com"


class _StubAuth(AuthProvider):
    """Auth provider pinning every request to one user (or none)."""

    def __init__(self, user_id: str | None) -> None:
        self._user_id = user_id

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return self._user_id


@pytest.fixture()
def cred_env(monkeypatch):
    monkeypatch.setenv("OMNIGENT_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("OMNIGENT_GITHUB_CREDENTIAL_CLIENT_ID", "cid123")
    monkeypatch.setenv("OMNIGENT_GITHUB_CREDENTIAL_CLIENT_SECRET", "csecret")


@pytest.fixture()
def credential_store(cred_env, db_uri: str) -> CredentialStore:
    return CredentialStore(db_uri)


@asynccontextmanager
async def _client(
    db_uri: str, tmp_path: Path, credential_store: CredentialStore, user: str | None
) -> AsyncIterator[httpx.AsyncClient]:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        credential_store=credential_store,
        auth_provider=_StubAuth(user),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture()
async def client(
    runtime_init: None, db_uri: str, tmp_path: Path, credential_store: CredentialStore
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(db_uri, tmp_path, credential_store, _USER) as c:
        yield c


@pytest_asyncio.fixture()
async def anon_client(
    runtime_init: None, db_uri: str, tmp_path: Path, credential_store: CredentialStore
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(db_uri, tmp_path, credential_store, None) as c:
        yield c


async def test_list_requires_user(anon_client: httpx.AsyncClient) -> None:
    resp = await anon_client.get("/v1/credentials")
    assert resp.status_code == 401


async def test_list_empty(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/credentials")
    assert resp.status_code == 200
    body = resp.json()
    assert body["credentials"] == []
    assert body["enabled"] is True


async def test_connect_returns_authorize_url_with_state(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/credentials/github/connect")
    assert resp.status_code == 200
    url = resp.json()["authorize_url"]
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=cid123" in url
    assert "state=" in url


async def test_connect_disabled_without_client_id(client: httpx.AsyncClient, monkeypatch) -> None:
    monkeypatch.delenv("OMNIGENT_GITHUB_CREDENTIAL_CLIENT_ID", raising=False)
    resp = await client.post("/v1/credentials/github/connect")
    assert resp.status_code == 409


async def test_callback_bad_state_stores_nothing(
    client: httpx.AsyncClient, credential_store: CredentialStore
) -> None:
    resp = await client.get(
        "/auth/github/credential-callback?code=abc&state=garbage",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=state_mismatch" in resp.headers["location"]
    assert credential_store.get(_USER, "github") is None


async def test_callback_happy_path_then_list_and_disconnect(
    client: httpx.AsyncClient, credential_store: CredentialStore, monkeypatch
) -> None:
    async def fake_exchange(code: str) -> dict:
        assert code == "goodcode"
        return {"access_token": "gho_live", "scope": "repo"}

    async def fake_user(token: str) -> dict:
        assert token == "gho_live"
        return {"login": "alice-gh"}

    monkeypatch.setattr(credentials_routes, "_exchange_code", fake_exchange)
    monkeypatch.setattr(credentials_routes, "_fetch_github_user", fake_user)

    state = credentials_routes._mint_state(_USER)
    resp = await client.get(
        f"/auth/github/credential-callback?code=goodcode&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "connected=github" in resp.headers["location"]

    listing = await client.get("/v1/credentials")
    [cred] = listing.json()["credentials"]
    assert cred == {
        "provider": "github",
        "login": "alice-gh",
        "scopes": "repo",
        "connected_at": cred["connected_at"],
    }
    assert "gho_live" not in listing.text  # token never on the API surface

    resp = await client.delete("/v1/credentials/github")
    assert resp.json() == {"ok": True}
    assert credential_store.get(_USER, "github") is None
