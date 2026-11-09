"""Tests for the built-in proactive agent: settings, hidden tasks, and provisioning."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import proactive_agent
from omnigent.server.app import _ensure_default_agents, create_app
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.proactive_agent import (
    PROACTIVE_AGENT_ID,
    PROACTIVE_AGENT_NAME,
    TIMEZONE_HEADER,
    ProactiveProvisioner,
    proactive_task_id,
    request_timezone,
)
from omnigent.server.scheduled.scheduler import ScheduledTaskScheduler
from omnigent.spec import load
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.helpers import FakeSandboxLauncher

_BUNDLE = Path(proactive_agent.__file__).parent.parent / "resources" / "proactive" / "bundle"


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


def test_seeded_outside_the_seeded_agents_allowlist(db_uri: str, tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)

    suppressed = _ensure_default_agents(
        agents, artifacts, AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    )

    # Not an OMNIGENT_SEEDED_AGENTS suppression; GET /v1/agents hides it separately.
    assert PROACTIVE_AGENT_NAME not in suppressed
    agent = agents.get(PROACTIVE_AGENT_ID)
    assert agent is not None and agent.name == PROACTIVE_AGENT_NAME


async def test_agent_list_never_shows_the_proactive_agent(client: httpx.AsyncClient) -> None:
    app: Any = client.app  # type: ignore[attr-defined]
    app.state.seed_builtin_agents()

    listed = (await client.get("/v1/agents", params={"limit": 1000})).json()

    assert PROACTIVE_AGENT_ID not in {agent["id"] for agent in listed["data"]}
    assert PROACTIVE_AGENT_NAME not in listed["suppressed_agent_names"]


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
    agents = SqlAlchemyAgentStore(db_uri)
    cache = AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    app = create_app(
        agent_store=agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=cache,
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        sandbox_config=_sandbox() if sandbox else None,
        inbox_item_store=SqlAlchemyInboxItemStore(db_uri),
        # A callable, like a deployment's per-request gate.
        proactive_agent=lambda: enabled,
    )
    # The lifespan seeds built-in agents; these tests skip it.
    app.state.seed_builtin_agents = lambda: _ensure_default_agents(agents, artifacts, cache)
    return app


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


@pytest.mark.parametrize(
    "agent_id",
    [
        PROACTIVE_AGENT_ID,
        str(uuid.UUID(PROACTIVE_AGENT_ID)),
        f"ag_{PROACTIVE_AGENT_ID}",
    ],
    ids=["canonical", "dashed", "legacy-prefix"],
)
async def test_users_cannot_schedule_the_proactive_agent(
    client: httpx.AsyncClient, agent_id: str
) -> None:
    app: Any = client.app  # type: ignore[attr-defined]
    app.state.seed_builtin_agents()
    resp = await client.post(
        "/v1/scheduled-tasks",
        json={
            "name": "copy",
            "prompt": "go",
            "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "agent_id": agent_id,
            "execution_target": "managed_sandbox",
        },
    )

    assert resp.status_code == 400
    assert app.state.scheduled_task_store.list() == []


async def test_users_cannot_move_a_task_onto_the_proactive_agent(
    client: httpx.AsyncClient,
) -> None:
    store: Any = client.app.state.scheduled_task_store  # type: ignore[attr-defined]
    task = store.create(
        uuid.uuid4().hex, "mine", "go", "FREQ=DAILY;BYHOUR=9", None, "e" * 32, "UTC"
    )

    moved = await client.patch(
        f"/v1/scheduled-tasks/{task.id}", json={"agent_id": f"ag_{PROACTIVE_AGENT_ID}"}
    )
    hidden = await client.patch(
        f"/v1/scheduled-tasks/{proactive_task_id(None)}", json={"agent_id": "e" * 32}
    )

    assert moved.status_code == 400
    assert store.get(task.id).agent_id == "e" * 32
    assert hidden.status_code == 404


async def test_provisioning_arms_the_in_process_scheduler(db_uri: str) -> None:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    scheduler = ScheduledTaskScheduler(store, AsyncMock())
    await scheduler.start()
    provisioner = ProactiveProvisioner()
    try:
        task = await provisioner.provision(store, None, "UTC", scheduler)

        assert scheduler.next_run_at(task.id) is not None
        assert not provisioner.needed(None)
    finally:
        scheduler.stop()


async def test_a_failed_registration_is_retried(db_uri: str) -> None:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    scheduler = ScheduledTaskScheduler(store, AsyncMock())
    await scheduler.start()
    provisioner = ProactiveProvisioner()
    real_add = scheduler.add
    scheduler.add = Mock(side_effect=RuntimeError("timer unavailable"))  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await provisioner.provision(store, None, "UTC", scheduler)
        assert provisioner.needed(None)

        scheduler.add = real_add  # type: ignore[method-assign]
        task = await provisioner.provision(store, None, "UTC", scheduler)

        assert scheduler.next_run_at(task.id) is not None
        assert [t.id for t in store.list()] == [task.id]
    finally:
        scheduler.stop()


async def test_slow_provisioning_does_not_hold_the_create(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store: Any = client.app.state.scheduled_task_store  # type: ignore[attr-defined]
    release = threading.Event()
    real_get = store.get

    def slow_get(task_id: str) -> Any:
        release.wait(timeout=10)
        return real_get(task_id)

    monkeypatch.setattr(proactive_agent, "_PROVISION_TIMEOUT_S", 0.05)
    monkeypatch.setattr(store, "get", slow_get)

    resp = await client.post("/v1/sessions", json={"agent_id": "f" * 32})

    # The create's own response is returned while provisioning still runs.
    assert resp.status_code == 404
    release.set()
    for _ in range(100):
        if real_get(proactive_task_id(None)) is not None:
            break
        await asyncio.sleep(0.02)
    assert real_get(proactive_task_id(None)) is not None


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
