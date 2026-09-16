"""Unified discovery authorizes either a session editor or the host owner."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostSkillsFrame,
    HostSkillsResultFrame,
    decode_host_frame,
)
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.host.skills import HostSkillDiscovery
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.sessions.routes_agent import register_agent_routes
from omnigent.server.routes.skills import create_skills_router
from omnigent.spec.skill_sources import resolve_session_skills
from omnigent.spec.types import SkillSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import build_agent_bundle


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


@pytest.fixture
def skills_app(db_uri: str, tmp_path: Path):
    registry = HostRegistry()
    hosts = HostStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    artifacts.put("bundle", build_agent_bundle("menu-test"))
    agent = agents.create(
        "735f0aa173274531aaf7eb203ca4aa31", name="menu-test", bundle_location="bundle"
    )
    conv = conversations.create_conversation(
        agent_id=agent.id,
        host_id="a828988dc0b441fb8d04dad3761773b9",
        workspace="/actual/workspace",
        sub_agent_name="child",
    )
    permissions.ensure_user("owner")
    permissions.ensure_user("reader")
    permissions.ensure_user("editor")
    permissions.grant("owner", conv.id, LEVEL_OWNER)
    permissions.grant("reader", conv.id, LEVEL_READ)
    permissions.grant("editor", conv.id, LEVEL_EDIT)
    conn = registry.register(
        "a828988dc0b441fb8d04dad3761773b9",
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="host"),
        owner="owner",
    )
    app = FastAPI()
    app.state.host_store = hosts
    app.state.agent_cache = AgentCache(artifacts, tmp_path / "cache")
    app.state.agent_store = agents
    router = APIRouter()
    register_agent_routes(
        router,
        conversation_store=conversations,
        agent_store=agents,
        artifact_store=artifacts,
        host_registry=registry,
        auth_provider=_Auth(),
        permission_store=permissions,
    )
    app.include_router(router, prefix="/v1")
    app.include_router(
        create_skills_router(
            registry,
            hosts,
            conversations,
            agent_store=agents,
            agent_cache=app.state.agent_cache,
            auth_provider=_Auth(),
            permission_store=permissions,
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    return app, registry, conn, conv, agent, hosts


@pytest.fixture
def presession_app(skills_app):
    _, _, conn, _, _, hosts = skills_app
    hosts.upsert_on_connect(conn.host_id, "host", "owner")
    return skills_app


@pytest.mark.parametrize("user", ["owner", "editor"])
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_session_catalog_needs_no_runner_and_allows_shared_editors(
    skills_app, user: str, acknowledged: bool
) -> None:
    app, _, conn, conv, agent, _ = skills_app
    assert conv.runner_id is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.get("/v1/skills", params={"session_id": conv.id}, headers={"x-test-user": user})
        )
        frame = decode_host_frame(await asyncio.wait_for(conn.outbound_queue.get(), 2))
        assert isinstance(frame, HostSkillsFrame)
        assert (
            frame.session_id,
            frame.path,
            frame.agent_id,
            frame.agent_version,
            frame.sub_agent_name,
        ) == (conv.id, conv.workspace, agent.id, str(agent.version), "child")
        conn.pending_skills[frame.request_id].set_result(
            HostSkillsResultFrame(
                frame.request_id,
                "ok",
                skills=[{"name": "child-review", "description": "Review"}],
                session_id=conv.id if acknowledged else None,
            )
        )
        response = await task
    assert response.status_code == (200 if acknowledged else 502)
    if acknowledged:
        assert response.json()["skills"] == [{"name": "child-review", "description": "Review"}]
    else:
        assert "update the host" in response.json()["detail"]
    assert not conn.pending_skills


@pytest.mark.parametrize("user,status", [("reader", 403), ("stranger", 404), (None, 401)])
async def test_unauthorized_session_does_not_send_discovery(
    skills_app, user: str | None, status: int
) -> None:
    app, _, conn, conv, _, _ = skills_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/skills",
            params={"session_id": conv.id},
            headers={"x-test-user": user} if user else {},
        )
    assert response.status_code == status
    assert conn.outbound_queue.empty()
    assert not conn.pending_skills


@pytest.mark.parametrize(
    "overrides",
    [
        {"host_id": "other-host"},
        {"path": "/other"},
        {"harness": "codex-native"},
        {"agent_id": "other-agent"},
    ],
)
async def test_session_target_cannot_be_overridden(skills_app, overrides: dict[str, str]) -> None:
    app, _, conn, conv, _, _ = skills_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/skills",
            params={"session_id": conv.id, **overrides},
            headers={"x-test-user": "editor"},
        )
    assert response.status_code == 422
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("skill_filter", ["all", "none", ["allowed"]])
async def test_presession_catalog_matches_filtered_invocations(
    presession_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_filter: str | list[str]
) -> None:
    app, _, conn, _, agent, _ = presession_app
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for name in ("bundled", "hidden", "allowed", "denied"):
        directory = tmp_path / ".claude" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Host {name}\n---\nInstructions\n"
        )
    loaded = app.state.agent_cache.load(agent.id, agent.bundle_location)
    spec = replace(
        loaded.spec,
        skills_filter=skill_filter,
        skills=[
            SkillSpec(name="bundled", description="Bundled precedence", content="Instructions"),
            SkillSpec(
                name="hidden", description="Hidden", content="Instructions", user_invocable=False
            ),
        ],
    )
    monkeypatch.setattr(app.state.agent_cache, "load", lambda *a, **kw: SimpleNamespace(spec=spec))
    from omnigent.server.routes.builtin_agents import _to_agent_object as builtin_agent_object
    from omnigent.server.routes.sessions.routes_permissions import (
        _to_agent_object as session_agent_object,
    )

    for convert in (builtin_agent_object, session_agent_object):
        assert [s.name for s in convert(agent, app.state.agent_cache).skills] == ["bundled"]
    discovery = HostSkillDiscovery(
        lambda _: pytest.fail("Pre-session discovery needs no bundle fetch")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.get(
                "/v1/skills",
                params={
                    "host_id": conn.host_id,
                    "path": str(tmp_path),
                    "harness": "omnigent",
                    "agent_id": agent.id,
                },
                headers={"x-test-user": "owner"},
            )
        )
        frame = decode_host_frame(await asyncio.wait_for(conn.outbound_queue.get(), 2))
        assert isinstance(frame, HostSkillsFrame)
        assert frame.skills_filter == skill_filter
        assert frame.agent_version == str(agent.version)
        catalog = discovery.discover(frame, tmp_path)
        conn.pending_skills[frame.request_id].set_result(
            HostSkillsResultFrame(
                frame.request_id,
                "ok",
                skills=catalog,
                agent_id=frame.agent_id,
            )
        )
        response = await task
    assert response.status_code == 200
    expected = resolve_session_skills(spec, (tmp_path,), None)
    assert response.json()["skills"] == [
        {"name": s.name, "description": s.description} for s in expected
    ]
    assert "hidden" not in {s["name"] for s in response.json()["skills"]}


async def test_presession_discovery_rejects_private_agent(
    presession_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, conn, conv, agent, hosts = presession_app
    private = replace(agent, session_id=conv.id)
    monkeypatch.setattr(app.state.agent_store, "get", lambda _: private)
    other_host = "cafc32c79cdf4b55b786c1692e9e6c30"
    hosts.upsert_on_connect(other_host, "other", "stranger")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/skills",
            params={
                "host_id": other_host,
                "path": "/repo",
                "harness": "claude-sdk",
                "agent_id": private.id,
            },
            headers={"x-test-user": "stranger"},
        )
    assert response.status_code == 404
    assert conn.outbound_queue.empty()


async def test_presession_discovery_requires_host_to_apply_agent_filter(presession_app) -> None:
    app, _, conn, _, agent, _ = presession_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.get(
                "/v1/skills",
                params={
                    "host_id": conn.host_id,
                    "path": "/repo",
                    "harness": "claude-sdk",
                    "agent_id": agent.id,
                },
                headers={"x-test-user": "owner"},
            )
        )
        frame = decode_host_frame(await asyncio.wait_for(conn.outbound_queue.get(), 2))
        assert isinstance(frame, HostSkillsFrame)
        conn.pending_skills[frame.request_id].set_result(
            HostSkillsResultFrame(frame.request_id, "ok")
        )
        response = await task
    assert response.status_code == 502
    assert "update the host" in response.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"session_id": ""},
        {"host_id": "host"},
        {"host_id": "host", "path": "/repo"},
        {"harness": "claude-native", "path": "/repo"},
    ],
)
async def test_discovery_requires_one_complete_target(skills_app, params: dict[str, str]) -> None:
    app, _, conn, _, _, _ = skills_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/skills", params=params, headers={"x-test-user": "owner"})
    assert response.status_code == 422
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize(
    "token_host,expired,token,status",
    [
        ("a828988dc0b441fb8d04dad3761773b9", False, "valid", 200),
        ("661d72bbf63a42869a578d9de596577f", False, "valid", 401),
        ("a828988dc0b441fb8d04dad3761773b9", True, "valid", 401),
        ("a828988dc0b441fb8d04dad3761773b9", False, "invalid", 401),
    ],
)
async def test_managed_host_bundle_token_is_bound_to_session_host(
    skills_app, token_host: str, expired: bool, token: str, status: int
) -> None:
    app, _, _, conv, agent, hosts = skills_app
    hosts.register_managed_host(
        host_id=token_host,
        name="sandbox",
        user_id="owner",
        token="valid",
        provider="modal",
        sandbox_id="sandbox",
        token_expires_at=int(time.time()) + (-60 if expired else 3600),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/sessions/{conv.id}/agent/contents", headers={MANAGED_HOST_TOKEN_HEADER: token}
        )
    assert response.status_code == status
    if status == 200:
        assert response.headers["X-Agent-Id"] == agent.id
        assert response.headers["X-Agent-Version"] == str(agent.version)
        assert response.headers["Cache-Control"] == "no-store"
