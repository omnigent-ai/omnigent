"""Focused tests for admin publishing of instance-wide template agents."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from omnigent.db.utils import builtin_agent_id, generate_agent_id
from omnigent.errors import OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.admin_list import AdminList
from omnigent.server.auth import AuthProvider
from omnigent.server.routes import builtin_agents as agent_routes
from omnigent.server.routes.builtin_agents import create_builtin_agents_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio


class _HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-user")


class _Permissions:
    def is_admin(self, user_id: str) -> bool:
        return user_id == "admin"


@dataclass
class _PublishingStores:
    agents: SqlAlchemyAgentStore
    artifacts: LocalArtifactStore
    conversations: SqlAlchemyConversationStore
    cache: AgentCache
    artifact_root: Path


@pytest.fixture()
def publishing_stores(db_uri: str, tmp_path: Path) -> _PublishingStores:
    artifact_root = tmp_path / "artifacts"
    artifacts = LocalArtifactStore(str(artifact_root))
    return _PublishingStores(
        agents=SqlAlchemyAgentStore(db_uri),
        artifacts=artifacts,
        conversations=SqlAlchemyConversationStore(db_uri),
        cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        artifact_root=artifact_root,
    )


def _build_publishing_app(
    stores: _PublishingStores,
    *,
    permission_store: object | None,
    admin_list: AdminList,
) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_builtin_agents_router(
            stores.agents,
            stores.cache,
            artifact_store=stores.artifacts,
            conversation_store=stores.conversations,
            auth_provider=_HeaderAuth(),
            permission_store=permission_store,  # type: ignore[arg-type]
            admin_list=admin_list,
        ),
        prefix="/v1",
    )
    return app


@pytest.fixture()
def publishing_app(
    publishing_stores: _PublishingStores,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setattr(agent_routes, "local_single_user_enabled", lambda: False)
    return _build_publishing_app(
        publishing_stores,
        permission_store=_Permissions(),
        admin_list=AdminList(publishing_stores.artifact_root.parent / "admins"),
    )


@pytest_asyncio.fixture()
async def publishing_client(publishing_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=publishing_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _upload(bundle: bytes) -> dict[str, tuple[str, bytes, str]]:
    return {"bundle": ("agent.tar.gz", bundle, "application/gzip")}


async def test_remote_without_permission_store_requires_file_admin(
    publishing_stores: _PublishingStores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_routes, "local_single_user_enabled", lambda: False)
    admins_path = publishing_stores.artifact_root.parent / "admins"
    app = _build_publishing_app(
        publishing_stores,
        permission_store=None,
        admin_list=AdminList(admins_path),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.post(
            "/v1/agents",
            files=_upload(build_agent_bundle("unauthenticated-agent")),
        )
        assert unauthenticated.status_code == 401

        denied = await client.post(
            "/v1/agents",
            files=_upload(build_agent_bundle("denied-agent")),
            headers={"x-user": "member"},
        )
        assert denied.status_code == 403

        admins_path.write_text("file-admin\n")
        accepted = await client.post(
            "/v1/agents",
            files=_upload(build_agent_bundle("file-admin-agent")),
            headers={"x-user": "file-admin"},
        )
        assert accepted.status_code == 200, accepted.text


async def test_changed_put_schedules_runner_resets_in_background(
    publishing_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await publishing_client.post(
        "/v1/agents",
        files=_upload(build_agent_bundle("background-agent", description="v1")),
        headers={"x-user": "admin"},
    )
    agent_id = created.json()["id"]
    queued: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def _record_task(
        self: BackgroundTasks,
        func: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        del self
        queued.append((func, args, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", _record_task)
    updated = await publishing_client.put(
        f"/v1/agents/{agent_id}",
        files=_upload(build_agent_bundle("background-agent", description="v2")),
        headers={"x-user": "admin"},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert len(queued) == 1
    assert queued[0][1] == (agent_id,)


async def test_create_requires_admin_and_is_create_only(
    publishing_client: httpx.AsyncClient,
    publishing_stores: _PublishingStores,
) -> None:
    bundle = build_agent_bundle("shared-reviewer", description="v1")

    forbidden = await publishing_client.post(
        "/v1/agents", files=_upload(bundle), headers={"x-user": "member"}
    )
    assert forbidden.status_code == 403
    assert publishing_stores.agents.get_by_name("shared-reviewer") is None

    created = await publishing_client.post(
        "/v1/agents", files=_upload(bundle), headers={"x-user": "admin"}
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "shared-reviewer"
    assert body["version"] == 1
    assert body["builtin"] is False
    assert body["id"] != builtin_agent_id("shared-reviewer")
    stored = publishing_stores.agents.get(body["id"])
    assert stored is not None
    assert publishing_stores.artifacts.exists(stored.bundle_location)
    assert (
        publishing_stores.cache.load(
            stored.id, stored.bundle_location, expand_env=True
        ).spec.description
        == "v1"
    )

    forbidden_update = await publishing_client.put(
        f"/v1/agents/{stored.id}",
        files=_upload(build_agent_bundle("shared-reviewer", description="v2")),
        headers={"x-user": "member"},
    )
    assert forbidden_update.status_code == 403
    assert publishing_stores.agents.get(stored.id) == stored

    duplicate = await publishing_client.post(
        "/v1/agents", files=_upload(bundle), headers={"x-user": "admin"}
    )
    assert duplicate.status_code == 409
    assert publishing_stores.agents.get_by_name("shared-reviewer") == stored


async def test_invalid_create_and_update_leave_durable_state_unchanged(
    publishing_client: httpx.AsyncClient,
    publishing_stores: _PublishingStores,
) -> None:
    before = list(publishing_stores.artifact_root.rglob("*"))
    invalid_create = await publishing_client.post(
        "/v1/agents", files=_upload(b"not a tarball"), headers={"x-user": "admin"}
    )
    assert invalid_create.status_code == 400
    assert publishing_stores.agents.list(limit=100).data == []
    assert list(publishing_stores.artifact_root.rglob("*")) == before

    bundle = build_agent_bundle("safe-agent", description="v1")
    created = await publishing_client.post(
        "/v1/agents", files=_upload(bundle), headers={"x-user": "admin"}
    )
    agent_id = created.json()["id"]
    original = publishing_stores.agents.get(agent_id)
    assert original is not None

    invalid_update = await publishing_client.put(
        f"/v1/agents/{agent_id}",
        files=_upload(b"still not a tarball"),
        headers={"x-user": "admin"},
    )
    assert invalid_update.status_code == 400
    assert publishing_stores.agents.get(agent_id) == original
    assert publishing_stores.artifacts.get(original.bundle_location) == bundle


async def test_update_protects_seeded_session_scoped_and_name(
    publishing_client: httpx.AsyncClient,
    publishing_stores: _PublishingStores,
) -> None:
    seeded_name = "seeded-agent"
    seeded_id = builtin_agent_id(seeded_name)
    seeded_bundle = build_agent_bundle(seeded_name)
    seeded_location = f"{seeded_id}/seeded"
    publishing_stores.artifacts.put(seeded_location, seeded_bundle)
    publishing_stores.agents.create(seeded_id, seeded_name, seeded_location)

    seeded = await publishing_client.put(
        f"/v1/agents/{seeded_id}",
        files=_upload(build_agent_bundle(seeded_name, description="changed")),
        headers={"x-user": "admin"},
    )
    assert seeded.status_code == 403
    assert publishing_stores.agents.get(seeded_id).bundle_location == seeded_location  # type: ignore[union-attr]

    session_agent_id = generate_agent_id()
    session_bundle = build_agent_bundle("session-only")
    session_location = f"{session_agent_id}/session"
    publishing_stores.artifacts.put(session_location, session_bundle)
    publishing_stores.conversations.create_session_with_agent(
        agent_id=session_agent_id,
        agent_name="session-only",
        agent_bundle_location=session_location,
        agent_description=None,
    )
    session_scoped = await publishing_client.put(
        f"/v1/agents/{session_agent_id}",
        files=_upload(session_bundle),
        headers={"x-user": "admin"},
    )
    assert session_scoped.status_code == 400

    created = await publishing_client.post(
        "/v1/agents",
        files=_upload(build_agent_bundle("immutable-name")),
        headers={"x-user": "admin"},
    )
    agent_id = created.json()["id"]
    renamed = await publishing_client.put(
        f"/v1/agents/{agent_id}",
        files=_upload(build_agent_bundle("new-name")),
        headers={"x-user": "admin"},
    )
    assert renamed.status_code == 400
    assert publishing_stores.agents.get(agent_id).version == 1  # type: ignore[union-attr]


async def test_update_is_idempotent_refreshes_cache_and_resets_bound_sessions(
    publishing_client: httpx.AsyncClient,
    publishing_stores: _PublishingStores,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_bundle = build_agent_bundle("live-agent", description="v1")
    created = await publishing_client.post(
        "/v1/agents", files=_upload(original_bundle), headers={"x-user": "admin"}
    )
    agent_id = created.json()["id"]
    active_sessions = [
        publishing_stores.conversations.create_conversation(agent_id=agent_id)
        for _ in range(agent_routes._RUNNER_RESET_PAGE_SIZE + 2)
    ]
    archived = publishing_stores.conversations.create_conversation(agent_id=agent_id)
    publishing_stores.conversations.update_conversation(archived.id, archived=True)
    reset_calls: list[str] = []
    failed_session = active_sessions[0].id

    async def _record_reset(session_id: str, reset_agent_id: str, runner_router: object) -> None:
        del reset_agent_id, runner_router
        reset_calls.append(session_id)
        if session_id == failed_session:
            raise RuntimeError("unexpected runner failure")

    list_calls: list[dict[str, object]] = []
    original_list = SqlAlchemyConversationStore.list_conversations

    def _record_list(
        store: SqlAlchemyConversationStore,
        *args: object,
        **kwargs: object,
    ) -> object:
        list_calls.append(dict(kwargs))
        return original_list(store, *args, **kwargs)

    monkeypatch.setattr(agent_routes, "reset_runner_session_agent_cache", _record_reset)
    monkeypatch.setattr(SqlAlchemyConversationStore, "list_conversations", _record_list)

    identical = await publishing_client.put(
        f"/v1/agents/{agent_id}",
        files=_upload(original_bundle),
        headers={"x-user": "admin"},
    )
    assert identical.status_code == 200
    assert identical.json()["version"] == 1
    assert reset_calls == []

    changed_bundle = build_agent_bundle("live-agent", description="v2")
    changed = await publishing_client.put(
        f"/v1/agents/{agent_id}",
        files=_upload(changed_bundle),
        headers={"x-user": "admin"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["id"] == agent_id
    assert changed.json()["version"] == 2
    stored = publishing_stores.agents.get(agent_id)
    assert stored is not None
    assert publishing_stores.artifacts.get(stored.bundle_location) == changed_bundle
    assert (
        publishing_stores.cache.load(
            agent_id, stored.bundle_location, expand_env=True
        ).spec.description
        == "v2"
    )
    assert set(reset_calls) == {session.id for session in active_sessions}
    assert archived.id not in reset_calls
    assert failed_session in caplog.text
    assert len(list_calls) == 2
    assert all(call["limit"] == agent_routes._RUNNER_RESET_PAGE_SIZE for call in list_calls)
    assert all(call["include_archived"] is False for call in list_calls)
