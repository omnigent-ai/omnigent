"""Externally authenticated apps can write resources without an OSS users table."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from starlette.requests import HTTPConnection

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlUser
from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import AccountAuthorityMiddleware, AuthProvider, UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore


class ExternalAuthProvider(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("X-External-User")


@pytest.fixture
def without_users(db_uri):
    engine = get_or_create_engine(db_uri)
    SqlUser.__table__.drop(engine)
    try:
        yield
    finally:
        SqlUser.__table__.create(engine)


async def test_custom_auth_project_routes_without_users(
    runtime_init, db_uri, tmp_path, monkeypatch, without_users
):
    # Embedders pass their provider directly, regardless of the environment default.
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=ExternalAuthProvider(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/v1/projects", json={"name": "Denied"})).status_code == 401
        client.headers["X-External-User"] = "alice"
        ids = []
        for name in ("B", "A"):
            response = await client.post("/v1/projects", json={"name": name})
            assert response.status_code == 200, response.text
            ids.append(response.json()["id"])
        response = await client.patch(f"/v1/projects/{ids[0]}", json={"name": "C"})
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "C"
        response = await client.put("/v1/projects/order", json={"ordered_project_ids": ids})
        assert response.status_code == 200, response.text
        assert response.json() == {"ordered_project_ids": ids, "sort_mode": "manual"}
        assert (await client.get("/v1/projects/order")).json() == response.json()
        assert [p["id"] for p in (await client.get("/v1/projects")).json()["data"]] == ids
        assert SqlAlchemyProjectStore(db_uri).get_order(user_id="alice") == ids

        client.headers["X-External-User"] = "bob"
        assert (await client.get("/v1/projects")).json()["data"] == []
        assert (await client.get("/v1/projects/order")).json()["ordered_project_ids"] is None
        assert (
            await client.patch(f"/v1/projects/{ids[0]}", json={"name": "Other owner"})
        ).status_code == 404
        assert (
            await client.put("/v1/projects/order", json={"ordered_project_ids": ids})
        ).status_code == 400


@pytest.mark.parametrize("source", ["custom", "header", "oidc", "none"])
def test_external_auth_lifespan_and_websocket_writes(db_uri, without_users, source):
    if source == "custom":
        provider = ExternalAuthProvider()
    elif source == "none":
        provider = None
    else:
        provider = UnifiedAuthProvider(source=source)
    hosts = HostStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task_id = uuid4().hex
    host_id = uuid4().hex

    async def background_write():
        task = await asyncio.to_thread(
            tasks.create,
            task_id,
            "Scheduled task",
            "hello",
            "FREQ=DAILY",
            "alice",
            uuid4().hex,
            "UTC",
        )
        assert task.account_generation is None
        # Durable jobs restore an identity even when the external generation is NULL.
        with account_authority_scope(task.user_id, task.account_generation):
            updated = await asyncio.to_thread(tasks.update, task_id, state="paused")
        assert updated.state == "paused"

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.create_task(background_write())
        yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(AccountAuthorityMiddleware, auth_provider=provider)

    @app.websocket("/host")
    async def register_host(websocket: WebSocket):
        await websocket.accept()
        host = await asyncio.to_thread(hosts.upsert_on_connect, host_id, "Laptop", "alice")
        await websocket.send_json({"user_id": host.user_id, "generation": host.account_generation})
        await websocket.close()

    with TestClient(app) as client:
        with client.websocket_connect("/host") as ws:
            assert ws.receive_json() == {"user_id": "alice", "generation": None}
    assert tasks.get(task_id).state == "paused"
    assert hosts.get_host(host_id).user_id == "alice"

    # App execution must not change the default for standalone store calls.
    with pytest.raises(DBAPIError):
        hosts.upsert_on_connect(uuid4().hex, "Other laptop", "alice")


async def test_account_mode_is_isolated_between_apps(db_uri, without_users):
    projects = SqlAlchemyProjectStore(db_uri)
    arrived = 0
    both_running = asyncio.Event()

    def build(provider):
        app = FastAPI()
        app.add_middleware(AccountAuthorityMiddleware, auth_provider=provider)

        @app.post("/write")
        async def write():
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both_running.set()
            await asyncio.wait_for(both_running.wait(), timeout=5)
            await asyncio.to_thread(projects.create, uuid4().hex, "Project", "alice")
            return {}

        return app

    async def request(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.post("/write")

    external, accounts = await asyncio.gather(
        request(build(ExternalAuthProvider())),
        request(build(UnifiedAuthProvider(source="accounts"))),
    )
    assert external.status_code == 200, external.text
    assert accounts.status_code == 500
