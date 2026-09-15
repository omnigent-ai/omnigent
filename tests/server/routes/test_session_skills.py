"""Session menu discovery uses the host and retains session access rules."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostSkillsFrame,
    HostSkillsResultFrame,
    decode_host_frame,
)
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.sessions.routes_agent import register_agent_routes
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
    permissions.grant("owner", conv.id, LEVEL_OWNER)
    permissions.grant("reader", conv.id, LEVEL_READ)
    conn = registry.register(
        "a828988dc0b441fb8d04dad3761773b9",
        AsyncMock(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="host"),
        owner="owner",
    )
    app = FastAPI()
    app.state.host_store = hosts
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
    return app, registry, conn, conv, agent, hosts


@pytest.mark.parametrize("user", ["owner", "reader"])
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_session_catalog_needs_no_runner_and_preserves_shared_read_access(
    skills_app, user: str, acknowledged: bool
) -> None:
    app, _, conn, conv, agent, _ = skills_app
    assert conv.runner_id is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.get(f"/v1/sessions/{conv.id}/skills", headers={"x-test-user": user})
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


async def test_unauthorized_session_does_not_send_discovery(skills_app) -> None:
    app, _, conn, conv, _, _ = skills_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(OmnigentError):
            await client.get(f"/v1/sessions/{conv.id}/skills", headers={"x-test-user": "stranger"})
    assert conn.outbound_queue.empty()
    assert not conn.pending_skills


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
