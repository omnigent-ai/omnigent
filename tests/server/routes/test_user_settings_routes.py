"""Tests for the caller-scoped settings routes (``/v1/settings``)."""

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
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ROUTE = "/v1/settings/session-title-instructions"
ALICE = "alice@example.com"
BOB = "bob@example.com"


def _as_user(user: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


@pytest.fixture()
def multi_user_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=SqlAlchemyPermissionStore(db_uri),
    )


@pytest_asyncio.fixture()
async def multi_user_client(multi_user_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=multi_user_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_single_user_round_trip(client: httpx.AsyncClient) -> None:
    initial = await client.get(ROUTE)
    assert initial.status_code == 200
    assert initial.json() == {"instructions": None, "max_chars": 4000}

    saved = await client.put(ROUTE, json={"instructions": "  Start with an emoji.\n"})
    assert saved.status_code == 200
    assert saved.json() == {"instructions": "Start with an emoji.", "max_chars": 4000}
    assert (await client.get(ROUTE)).json()["instructions"] == "Start with an emoji."

    cleared = await client.put(ROUTE, json={"instructions": "   "})
    assert cleared.json()["instructions"] is None
    assert (await client.get(ROUTE)).json()["instructions"] is None


async def test_rejects_instructions_over_limit(client: httpx.AsyncClient) -> None:
    await client.put(ROUTE, json={"instructions": "Keep me."})

    response = await client.put(ROUTE, json={"instructions": "x" * 4001})

    assert response.status_code == 400
    assert "4000 characters" in response.text
    assert (await client.get(ROUTE)).json()["instructions"] == "Keep me."


async def test_accepts_limit_after_stripping(client: httpx.AsyncClient) -> None:
    response = await client.put(ROUTE, json={"instructions": "  " + "x" * 4000 + "  "})

    assert response.status_code == 200
    assert response.json()["instructions"] == "x" * 4000


@pytest.mark.parametrize(
    "body",
    [{}, {"instructions": 7}, {"instructions": "ok", "extra": True}],
)
async def test_rejects_malformed_body(client: httpx.AsyncClient, body: dict[str, object]) -> None:
    assert (await client.put(ROUTE, json=body)).status_code == 422


async def test_info_advertises_capability(client: httpx.AsyncClient) -> None:
    info = await client.get("/v1/info")

    assert info.json()["session_title_instructions"] is True


async def test_settings_are_private_to_each_user(multi_user_client: httpx.AsyncClient) -> None:
    await multi_user_client.put(
        ROUTE, json={"instructions": "Alice rule."}, headers=_as_user(ALICE)
    )

    alice = await multi_user_client.get(ROUTE, headers=_as_user(ALICE))
    bob = await multi_user_client.get(ROUTE, headers=_as_user(BOB))

    assert alice.json()["instructions"] == "Alice rule."
    assert bob.json()["instructions"] is None


async def test_requires_authenticated_user(multi_user_client: httpx.AsyncClient) -> None:
    assert (await multi_user_client.get(ROUTE)).status_code == 401
    assert (await multi_user_client.put(ROUTE, json={"instructions": "x"})).status_code == 401
