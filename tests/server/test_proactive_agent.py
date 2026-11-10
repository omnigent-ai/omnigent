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

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.utils import builtin_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import proactive_agent
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.app import _ensure_default_agents, _ensure_extra_builtin_agents, create_app
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
from omnigent.server.seeded_agents import SEEDED_AGENTS_ENV
from omnigent.spec import load
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.helpers import FakeSandboxLauncher

_BUNDLE = Path(proactive_agent.__file__).parent.parent / "resources" / "proactive" / "bundle"


class _SeededAgents:
    """An agent store in which the built-in proactive agent exists."""

    def get(self, agent_id: str) -> object | None:
        return object() if agent_id == PROACTIVE_AGENT_ID else None


_SEEDED_AGENTS = _SeededAgents()


def test_task_id_is_fixed_per_owner() -> None:
    assert proactive_task_id("alice") == proactive_task_id("alice")
    assert proactive_task_id("alice") != proactive_task_id("bob")
    assert len(proactive_task_id(None)) == 32


@pytest.mark.parametrize(
    "value",
    ["Not/AZone", "America", "a" * 300, "../../etc/passwd", "", None],
    ids=["unknown", "directory", "overlong", "path", "empty", "absent"],
)
def test_request_timezone_falls_back_to_utc(value: str | None) -> None:
    assert request_timezone(value) == "UTC"


def test_request_timezone_keeps_a_valid_name() -> None:
    assert request_timezone("America/Los_Angeles") == "America/Los_Angeles"


def test_bundle_opts_into_the_inbox_tools_only() -> None:
    spec = load(_BUNDLE)

    assert spec.name == PROACTIVE_AGENT_NAME
    assert spec.tool_groups.user_inbox
    assert not (spec.tool_groups.scheduled_tasks or spec.tool_groups.browser)


def test_seeded_outside_the_seeded_agents_allowlist(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty allowlist trims every packaged agent; the proactive agent is still seeded.
    monkeypatch.setenv(SEEDED_AGENTS_ENV, "")
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)

    suppressed = _ensure_default_agents(
        agents, artifacts, AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    )

    # Not an OMNIGENT_SEEDED_AGENTS suppression; GET /v1/agents hides it separately.
    assert PROACTIVE_AGENT_NAME not in suppressed
    agent = agents.get(PROACTIVE_AGENT_ID)
    assert agent is not None and agent.name == PROACTIVE_AGENT_NAME


def test_an_operator_agent_with_the_name_is_left_alone(db_uri: str, tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)
    cache = AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    operator_id = uuid.uuid4().hex
    agents.create(operator_id, PROACTIVE_AGENT_NAME, f"{operator_id}/operator")

    _ensure_default_agents(agents, artifacts, cache)

    operator = agents.get(operator_id)
    assert operator is not None and operator.bundle_location == f"{operator_id}/operator"
    assert agents.get(PROACTIVE_AGENT_ID) is None


def test_an_extra_built_in_cannot_take_the_name(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / f"{PROACTIVE_AGENT_NAME}.yaml"
    extra.write_text("name: proactive\ninstructions: An operator's agent.\n")
    monkeypatch.setenv("OMNIGENT_BUILTIN_AGENT_DIRS", str(extra))
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)

    cache = AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")

    seeded = _ensure_extra_builtin_agents(agents, artifacts, cache)

    assert seeded == frozenset()
    assert agents.get_by_name(PROACTIVE_AGENT_NAME) is None


async def test_nothing_is_provisioned_without_the_built_in_agent(db_uri: str) -> None:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    provisioner = ProactiveProvisioner()

    task = await provisioner.provision(store, SqlAlchemyAgentStore(db_uri), None, "UTC", None)

    assert task is None
    assert store.list() == []
    assert not provisioner.needed(None)


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
    conversations = SqlAlchemyConversationStore(db_uri)
    cache = AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    app = create_app(
        agent_store=agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
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
    app.state.test_conversations = conversations
    return app


@pytest_asyncio.fixture()
async def client(db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(db_uri, tmp_path, enabled=True)
    app.state.seed_builtin_agents()
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

    await _create_session(client)
    hidden_id = proactive_task_id(None)
    assert store.get(hidden_id) is not None

    moved = await client.patch(
        f"/v1/scheduled-tasks/{task.id}", json={"agent_id": f"ag_{PROACTIVE_AGENT_ID}"}
    )
    hidden = await client.patch(f"/v1/scheduled-tasks/{hidden_id}", json={"agent_id": "e" * 32})

    assert moved.status_code == 400
    assert store.get(task.id).agent_id == "e" * 32
    assert hidden.status_code == 404
    assert store.get(hidden_id).agent_id == PROACTIVE_AGENT_ID


async def test_provisioning_arms_the_in_process_scheduler(db_uri: str) -> None:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    scheduler = ScheduledTaskScheduler(store, AsyncMock())
    await scheduler.start()
    provisioner = ProactiveProvisioner()
    try:
        task = await provisioner.provision(store, _SEEDED_AGENTS, None, "UTC", scheduler)

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
            await provisioner.provision(store, _SEEDED_AGENTS, None, "UTC", scheduler)
        assert provisioner.needed(None)

        scheduler.add = real_add  # type: ignore[method-assign]
        task = await provisioner.provision(store, _SEEDED_AGENTS, None, "UTC", scheduler)

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

    try:
        # Bounded well below the blocked lookup, so a create that waited on it fails here.
        resp = await asyncio.wait_for(
            client.post("/v1/sessions", json={"agent_id": "f" * 32}), timeout=3
        )
        assert not release.is_set()
    finally:
        release.set()

    # The create's own response is returned while provisioning still runs.
    assert resp.status_code == 404
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


@pytest.mark.parametrize("header", ["America", "a" * 300], ids=["directory", "overlong"])
async def test_a_bad_timezone_header_does_not_fail_the_create(
    client: httpx.AsyncClient, header: str
) -> None:
    store: Any = client.app.state.scheduled_task_store  # type: ignore[attr-defined]

    resp = await client.post(
        "/v1/sessions", json={"agent_id": "f" * 32}, headers={TIMEZONE_HEADER: header}
    )

    # The unknown agent's own 404, not a provisioning error.
    assert resp.status_code == 404
    task = store.get(proactive_task_id(None))
    assert task is not None and task.timezone == "UTC"


async def test_a_setup_failure_does_not_fail_the_create(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(value: str | None) -> str:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(proactive_agent, "request_timezone", broken)

    resp = await client.post("/v1/sessions", json={"agent_id": "f" * 32})

    assert resp.status_code == 404


async def test_a_reregistered_account_gets_a_new_task(db_uri: str) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    store = SqlAlchemyScheduledTaskStore(db_uri)
    old = accounts.create_user_with_password("alice", "test-password-hash")
    checked = ProactiveProvisioner()
    with account_authority_scope("alice", old.account_generation):
        first = await checked.provision(store, _SEEDED_AGENTS, "alice", "UTC", None)
        assert not checked.needed("alice")

    assert accounts.delete_user("alice") is True
    assert store.get(first.id).state == "deleted"
    new = accounts.create_user_with_password("alice", "replacement-password-hash")

    with account_authority_scope("alice", new.account_generation):
        # A fresh process meets the deleted row first.
        fresh = await ProactiveProvisioner().provision(store, _SEEDED_AGENTS, "alice", "UTC", None)
        # A process that provisioned the old registration provisions again.
        assert checked.needed("alice")
        again = await checked.provision(store, _SEEDED_AGENTS, "alice", "UTC", None)

    assert fresh.id != first.id
    assert again.id == fresh.id
    assert (fresh.state, fresh.account_generation) == ("active", new.account_generation)
    assert store.get(first.id).state == "deleted"


async def test_a_proactive_run_cannot_seed_an_automation(client: httpx.AsyncClient) -> None:
    app: Any = client.app  # type: ignore[attr-defined]
    app.state.seed_builtin_agents()
    run = app.state.test_conversations.create_conversation(agent_id=PROACTIVE_AGENT_ID)

    resp = await client.post(
        "/v1/scheduled-tasks",
        json={
            "name": "from a run",
            "prompt": "go",
            "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "agent_id": builtin_agent_id("debby"),
            "execution_target": "managed_sandbox",
            "source_session_id": run.id,
        },
    )

    assert resp.status_code == 400, resp.text
    assert "proactive" in resp.text
    assert app.state.scheduled_task_store.list() == []
