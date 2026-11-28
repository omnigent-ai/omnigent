"""Tests for the owner-only host session-defaults routes."""

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
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
ALICE_HOST = "a1ed1ab71de2311e20488a989e61701c"
BOB_HOST = "b1b5efd7dfc33b5a6241f1866ffb00e6"
AGENT_ID = "187b7cb7ac30abf4debfaa578d052ec6"
UNKNOWN_AGENT_ID = "287b7cb7ac30abf4debfaa578d052ec6"
EMPTY = {"agent_id": None, "workspace": None, "model": None, "effort": None}


def _route(host_id: str = ALICE_HOST) -> str:
    return f"/v1/hosts/{host_id}/session-defaults"


def _as_user(user: str = ALICE) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


@pytest.fixture()
def host_store(db_uri: str) -> HostStore:
    store = HostStore(db_uri)
    store.upsert_on_connect(ALICE_HOST, "alice-laptop", ALICE)
    store.upsert_on_connect(BOB_HOST, "bob-laptop", BOB)
    return store


@pytest_asyncio.fixture()
async def multi_user_client(
    runtime_init: None, db_uri: str, tmp_path: Path, host_store: HostStore
) -> AsyncIterator[httpx.AsyncClient]:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(AGENT_ID, "host-default-agent", f"{AGENT_ID}/bundle")
    app: FastAPI = create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        host_store=host_store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_owner_sets_updates_and_clears_defaults(
    multi_user_client: httpx.AsyncClient,
) -> None:
    initial = await multi_user_client.get(_route(), headers=_as_user())
    assert initial.status_code == 200, initial.text
    assert initial.json() == EMPTY

    defaults = {"agent_id": AGENT_ID, "workspace": "/work/app", "model": "opus", "effort": "high"}
    saved = await multi_user_client.put(_route(), json=defaults, headers=_as_user())
    assert saved.status_code == 200, saved.text
    assert saved.json() == defaults
    assert (await multi_user_client.get(_route(), headers=_as_user())).json() == defaults

    # PUT replaces the whole object: omitted fields are cleared.
    updated = await multi_user_client.put(
        _route(), json={"workspace": "C:\\work\\app"}, headers=_as_user()
    )
    assert updated.json() == {**EMPTY, "workspace": "C:\\work\\app"}
    assert (await multi_user_client.get(_route(), headers=_as_user())).json() == updated.json()

    cleared = await multi_user_client.put(_route(), json={}, headers=_as_user())
    assert cleared.json() == EMPTY
    assert (await multi_user_client.get(_route(), headers=_as_user())).json() == EMPTY


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"workspace": "work/app"}, id="relative-workspace"),
        pytest.param({"workspace": ""}, id="empty-workspace"),
        pytest.param({"model": "opus"}, id="model-without-agent"),
        pytest.param({"agent_id": AGENT_ID, "effort": "extreme"}, id="unknown-effort"),
        pytest.param({"agent_id": AGENT_ID, "model": "--dangerous"}, id="flag-shaped-model"),
        pytest.param({"harness_args": ["--yolo"]}, id="unknown-field"),
    ],
)
async def test_rejects_invalid_defaults_without_changing_saved_ones(
    multi_user_client: httpx.AsyncClient, body: dict[str, object]
) -> None:
    await multi_user_client.put(_route(), json={"workspace": "/keep"}, headers=_as_user())

    response = await multi_user_client.put(_route(), json=body, headers=_as_user())

    assert response.status_code == 422, response.text
    assert (await multi_user_client.get(_route(), headers=_as_user())).json()["workspace"] == (
        "/keep"
    )


async def test_rejects_an_agent_the_owner_cannot_start(
    multi_user_client: httpx.AsyncClient,
) -> None:
    response = await multi_user_client.put(
        _route(), json={"agent_id": UNKNOWN_AGENT_ID}, headers=_as_user()
    )

    assert response.status_code == 404, response.text
    assert (await multi_user_client.get(_route(), headers=_as_user())).json() == EMPTY


async def test_defaults_are_private_to_the_host_owner(
    multi_user_client: httpx.AsyncClient,
) -> None:
    await multi_user_client.put(_route(), json={"workspace": "/alice"}, headers=_as_user())

    assert (await multi_user_client.get(_route(), headers=_as_user(BOB))).status_code == 403
    write = await multi_user_client.put(
        _route(), json={"workspace": "/bob"}, headers=_as_user(BOB)
    )
    assert write.status_code == 403
    unknown = await multi_user_client.get(
        _route("c1b5efd7dfc33b5a6241f1866ffb00e6"), headers=_as_user()
    )
    assert unknown.status_code == 404
    assert (await multi_user_client.get(_route())).status_code == 401
    assert (await multi_user_client.get(_route(), headers=_as_user())).json()["workspace"] == (
        "/alice"
    )


async def test_managed_sandbox_hosts_take_no_defaults(
    multi_user_client: httpx.AsyncClient, host_store: HostStore
) -> None:
    managed = host_store.register_managed_host(
        host_id="d1b5efd7dfc33b5a6241f1866ffb00e6",
        name="sandbox-host",
        user_id=ALICE,
        token="launch-token",
        provider="modal",
        sandbox_id="sb-test",
        token_expires_at=2**31 - 1,
    )

    response = await multi_user_client.put(
        _route(managed.host_id), json={"workspace": "/work"}, headers=_as_user()
    )

    assert response.status_code == 400, response.text
    assert "managed sandboxes" in response.text


async def test_info_advertises_capability(multi_user_client: httpx.AsyncClient) -> None:
    info = await multi_user_client.get("/v1/info", headers=_as_user())

    assert info.json()["host_session_defaults"] is True
