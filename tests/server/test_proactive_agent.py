"""Tests for the built-in proactive agent: settings, hidden tasks, and provisioning."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import proactive_agent
from omnigent.server.app import _ensure_default_agents, create_app
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.proactive_agent import (
    PROACTIVE_AGENT_ID,
    PROACTIVE_AGENT_NAME,
    TIMEZONE_HEADER,
    FileProactiveAgentSettings,
    ProactiveAgentState,
    proactive_task_id,
    request_timezone,
)
from omnigent.spec import load
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.helpers import FakeSandboxLauncher

_BUNDLE = Path(proactive_agent.__file__).parent.parent / "resources" / "proactive"


def test_settings_default_off_and_round_trip(tmp_path: Path) -> None:
    settings = FileProactiveAgentSettings(tmp_path / "proactive_agents")
    assert settings.read() == ProactiveAgentState()

    settings.write(ProactiveAgentState(enabled=True, agents={"other": False}))

    state = settings.read()
    assert state.agent_enabled(PROACTIVE_AGENT_NAME)
    assert not state.agent_enabled("other")
    invalid = tmp_path / "invalid"
    invalid.write_text("not json")
    assert FileProactiveAgentSettings(invalid).read() == ProactiveAgentState()


def test_task_id_is_fixed_per_owner() -> None:
    assert proactive_task_id("alice") == proactive_task_id("alice")
    assert proactive_task_id("alice") != proactive_task_id("bob")
    assert len(proactive_task_id(None)) == 32


def test_request_timezone_falls_back_to_utc() -> None:
    assert request_timezone("America/Los_Angeles") == "America/Los_Angeles"
    assert request_timezone("Not/AZone") == "UTC"
    assert request_timezone(None) == "UTC"


def test_bundle_opts_into_the_inbox_tools_only() -> None:
    spec = load(_BUNDLE)

    assert spec.name == PROACTIVE_AGENT_NAME
    assert spec.tool_groups.user_inbox
    assert not (spec.tool_groups.scheduled_tasks or spec.tool_groups.browser)


def test_seeded_and_always_hidden(db_uri: str, tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)

    hidden = _ensure_default_agents(
        agents, artifacts, AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    )

    assert PROACTIVE_AGENT_NAME in hidden
    agent = agents.get(PROACTIVE_AGENT_ID)
    assert agent is not None and agent.name == PROACTIVE_AGENT_NAME


def _sandbox() -> ManagedSandboxDeployment:
    launcher = FakeSandboxLauncher()
    return ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            provider="company",
            server_url="https://test.example",
            token_ttl_s=100,
            launcher_factory=lambda: launcher,
        )
    )


def _app(db_uri: str, tmp_path: Path, *, enabled: bool, sandbox: bool = True) -> FastAPI:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    settings = FileProactiveAgentSettings(tmp_path / "proactive_agents")
    settings.write(ProactiveAgentState(enabled=enabled))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        sandbox_config=_sandbox() if sandbox else None,
        proactive_agent_settings=settings,
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "proactive_agent"}),
    )


@pytest_asyncio.fixture()
async def client(db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(db_uri, tmp_path, enabled=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


async def _create_session(client: httpx.AsyncClient, **headers: str) -> None:
    # The agent does not exist, so the create fails; provisioning runs regardless.
    await client.post("/v1/sessions", json={"agent_id": "f" * 32}, headers=headers)


async def test_session_create_provisions_one_hidden_task(client: httpx.AsyncClient) -> None:
    store: Any = client.app.state.scheduled_task_store  # type: ignore[attr-defined]

    await _create_session(client, **{TIMEZONE_HEADER: "Asia/Seoul"})
    await _create_session(client)

    task = store.get(proactive_task_id(None))
    assert task is not None
    assert (task.agent_id, task.execution_target, task.timezone) == (
        PROACTIVE_AGENT_ID,
        "managed_sandbox",
        "Asia/Seoul",
    )
    assert [t.id for t in store.list()] == [task.id]
    listed = (await client.get("/v1/scheduled-tasks")).json()["scheduled_tasks"]
    assert listed == []
    assert (await client.get(f"/v1/scheduled-tasks/{task.id}")).status_code == 404
    assert (await client.post(f"/v1/scheduled-tasks/{task.id}/run")).status_code == 404


async def test_users_cannot_schedule_the_proactive_agent(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/scheduled-tasks",
        json={
            "name": "copy",
            "prompt": "go",
            "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "agent_id": PROACTIVE_AGENT_ID,
            "execution_target": "managed_sandbox",
        },
    )

    assert resp.status_code == 400


@pytest.mark.parametrize(
    ("enabled", "sandbox"), [(False, True), (True, False)], ids=["turned-off", "no-sandbox"]
)
async def test_no_task_when_the_agent_cannot_run(
    db_uri: str, tmp_path: Path, enabled: bool, sandbox: bool
) -> None:
    app = _app(db_uri, tmp_path, enabled=enabled, sandbox=sandbox)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await _create_session(c)

    assert app.state.scheduled_task_store.get(proactive_task_id(None)) is None


async def test_provisioning_failure_does_not_fail_the_create(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store: Any = client.app.state.scheduled_task_store  # type: ignore[attr-defined]

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "create", broken)

    resp = await client.post("/v1/sessions", json={"agent_id": "f" * 32})

    assert resp.status_code == 404
    monkeypatch.undo()
    await _create_session(client)
    assert store.get(proactive_task_id(None)) is not None


async def test_admin_settings_route(client: httpx.AsyncClient) -> None:
    state = (await client.get("/v1/admin/proactive-agents")).json()
    assert (state["enabled"], state["editable"]) == (True, True)
    assert [agent["name"] for agent in state["agents"]] == [PROACTIVE_AGENT_NAME]

    updated = await client.put(
        "/v1/admin/proactive-agents", json={"agents": {PROACTIVE_AGENT_NAME: False}}
    )

    assert updated.status_code == 200
    assert updated.json()["agents"][0]["enabled"] is False
    assert (
        await client.put("/v1/admin/proactive-agents", json={"agents": {"nope": True}})
    ).status_code == 400


def test_admin_route_is_off_without_the_feature(db_uri: str, tmp_path: Path) -> None:
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=LocalArtifactStore(str(tmp_path / "a")),
        agent_cache=SimpleNamespace(),  # type: ignore[arg-type]
    )

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/v1/admin/proactive-agents" not in paths
