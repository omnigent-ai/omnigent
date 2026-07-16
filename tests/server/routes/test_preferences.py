"""Tests for the per-user sidebar preference routes (``/v1/preferences``).

The router is only mounted when ``create_app`` receives a ``permission_store``
(it needs an identity to key rows by), so these tests build their own app with
one, and drive it through header auth so two identities can be exercised
independently.

Covers the whole surface: the empty default, PUT round-tripping through GET,
per-user isolation, the key allow-list, the value-size cap, and the
unauthenticated 401 that sends the web app back to its localStorage cache.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_ALICE = "alice@prefs.test"
_BOB = "bob@prefs.test"


@pytest.fixture()
def prefs_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """A real ``create_app`` with a permission store and header auth."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        # local_single_user=False so a request with no identity header resolves
        # to None (401), not the "local" fallback — exercises the unauth path.
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


def _client(app: FastAPI, email: str | None = None) -> httpx.AsyncClient:
    """An in-process async client, optionally carrying a header identity."""
    headers = {"X-Forwarded-Email": email} if email else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    )


@pytest_asyncio.fixture()
async def alice(prefs_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client authenticated as ``_ALICE``."""
    async with _client(prefs_app, _ALICE) as c:
        yield c


@pytest_asyncio.fixture()
async def bob(prefs_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client authenticated as ``_BOB``."""
    async with _client(prefs_app, _BOB) as c:
        yield c


@pytest.mark.asyncio
async def test_get_returns_empty_preferences_by_default(alice: httpx.AsyncClient) -> None:
    """A user who has never written a preference gets an empty mapping."""
    res = await alice.get("/v1/preferences")
    assert res.status_code == 200
    assert res.json() == {"object": "preferences", "preferences": {}}


@pytest.mark.asyncio
async def test_put_then_get_round_trips(alice: httpx.AsyncClient) -> None:
    """A pinned-ids write is readable back on the next GET — the whole point:
    the pins survive a fresh browser with an empty localStorage bucket."""
    res = await alice.put(
        "/v1/preferences/pinned_conversation_ids",
        json={"value": ["abc", "def"]},
    )
    assert res.status_code == 200
    assert res.json() == {
        "object": "preference",
        "key": "pinned_conversation_ids",
        "value": ["abc", "def"],
    }

    res = await alice.get("/v1/preferences")
    assert res.status_code == 200
    assert res.json()["preferences"] == {"pinned_conversation_ids": ["abc", "def"]}


@pytest.mark.asyncio
async def test_put_upserts(alice: httpx.AsyncClient) -> None:
    """Re-pinning overwrites the stored list rather than appending a row."""
    await alice.put("/v1/preferences/pinned_conversation_ids", json={"value": ["abc"]})
    await alice.put("/v1/preferences/pinned_conversation_ids", json={"value": ["def"]})
    res = await alice.get("/v1/preferences")
    assert res.json()["preferences"] == {"pinned_conversation_ids": ["def"]}


@pytest.mark.asyncio
async def test_all_sidebar_keys_are_accepted(alice: httpx.AsyncClient) -> None:
    """Pins, collapsed sections, and expanded projects all persist."""
    await alice.put("/v1/preferences/pinned_conversation_ids", json={"value": ["abc"]})
    await alice.put("/v1/preferences/collapsed_sidebar_sections", json={"value": ["Chats"]})
    await alice.put("/v1/preferences/expanded_project_sections", json={"value": ["omnigent"]})
    res = await alice.get("/v1/preferences")
    assert res.json()["preferences"] == {
        "pinned_conversation_ids": ["abc"],
        "collapsed_sidebar_sections": ["Chats"],
        "expanded_project_sections": ["omnigent"],
    }


@pytest.mark.asyncio
async def test_preferences_are_isolated_per_user(
    alice: httpx.AsyncClient, bob: httpx.AsyncClient
) -> None:
    """Two identities never see each other's pins."""
    await alice.put("/v1/preferences/pinned_conversation_ids", json={"value": ["alice-pin"]})
    await bob.put("/v1/preferences/pinned_conversation_ids", json={"value": ["bob-pin"]})

    assert (await alice.get("/v1/preferences")).json()["preferences"] == {
        "pinned_conversation_ids": ["alice-pin"]
    }
    assert (await bob.get("/v1/preferences")).json()["preferences"] == {
        "pinned_conversation_ids": ["bob-pin"]
    }


@pytest.mark.asyncio
async def test_unknown_key_is_rejected(alice: httpx.AsyncClient) -> None:
    """The endpoint is an allow-list, not an open per-user blob store."""
    res = await alice.put("/v1/preferences/arbitrary_key", json={"value": ["x"]})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_oversize_value_is_rejected(alice: httpx.AsyncClient) -> None:
    """A value beyond the cap is refused so the column can't be stuffed."""
    res = await alice.put(
        "/v1/preferences/pinned_conversation_ids",
        # ~100 KB serialized — comfortably past the cap.
        json={"value": ["x" * 100 for _ in range(1000)]},
    )
    assert res.status_code == 400
    # Nothing was persisted by the rejected write.
    assert (await alice.get("/v1/preferences")).json()["preferences"] == {}


@pytest.mark.asyncio
async def test_unauthenticated_requests_are_rejected(prefs_app: FastAPI) -> None:
    """Without an identity there is no row to key on — the web app falls back
    to its localStorage cache on 401 rather than sharing one global blob."""
    async with _client(prefs_app) as anon:
        assert (await anon.get("/v1/preferences")).status_code == 401
        res = await anon.put("/v1/preferences/pinned_conversation_ids", json={"value": ["x"]})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_router_absent_without_permission_store(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """Single-user deploys with no permission store have no identity to key
    rows by, so the endpoint isn't mounted and the client stays local-only."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
    )
    async with _client(app) as anon:
        assert (await anon.get("/v1/preferences")).status_code == 404
