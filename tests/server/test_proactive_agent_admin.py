"""Tests for the proactive agent's admin routes: prompt override and Run now."""

from __future__ import annotations

import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import yaml

from omnigent.server.proactive_agent import PROACTIVE_AGENT_ID, proactive_task_id
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.proactive_agent_settings_store import ProactiveAgentSettingsStore
from tests.server.test_proactive_agent import _app, _create_session


@pytest_asyncio.fixture()
async def client(db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(db_uri, tmp_path, enabled=True)
    app.state.seed_builtin_agents()
    app.state.scheduled_task_run_now = AsyncMock(return_value=True)
    # The same directory _app stores bundles in.
    app.state.test_artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


def _bundled_prompt(client: httpx.AsyncClient) -> str:
    app: Any = client.app  # type: ignore[attr-defined]
    agent = app.state.agent_store.get(PROACTIVE_AGENT_ID)
    bundle = app.state.test_artifacts.get(agent.bundle_location)
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        member = next(m for m in archive.getmembers() if m.name.endswith("config.yaml"))
        config = archive.extractfile(member)
        assert config is not None
        return str(yaml.safe_load(config.read())["prompt"])


async def test_an_admin_overrides_and_restores_the_prompt(client: httpx.AsyncClient) -> None:
    app: Any = client.app  # type: ignore[attr-defined]
    before = (await client.get("/v1/proactive-agent/prompt")).json()
    assert before["overridden"] is False
    assert before["prompt"] == before["default_prompt"] == _bundled_prompt(client)
    original = app.state.agent_store.get(PROACTIVE_AGENT_ID)

    set_ = await client.put("/v1/proactive-agent/prompt", json={"prompt": "Post one item."})

    assert set_.status_code == 200, set_.text
    assert (set_.json()["prompt"], set_.json()["overridden"]) == ("Post one item.", True)
    assert _bundled_prompt(client) == "Post one item."
    changed = app.state.agent_store.get(PROACTIVE_AGENT_ID)
    # A new bundle version, so Inbox items record which prompt produced them.
    assert changed.bundle_location != original.bundle_location

    reset = await client.put("/v1/proactive-agent/prompt", json={"prompt": None})

    assert reset.json()["overridden"] is False
    assert _bundled_prompt(client) == before["default_prompt"]
    assert app.state.agent_store.get(PROACTIVE_AGENT_ID).bundle_location == (
        original.bundle_location
    )


async def test_the_override_survives_a_restart_reseed(client: httpx.AsyncClient) -> None:
    app: Any = client.app  # type: ignore[attr-defined]
    await client.put("/v1/proactive-agent/prompt", json={"prompt": "Post one item."})

    # Startup seeds the bundled prompt, then reapplies the override.
    app.state.seed_builtin_agents()
    app.state.reseed_proactive_agent(only_if_overridden=True)

    assert _bundled_prompt(client) == "Post one item."


@pytest.mark.parametrize(
    "prompt",
    ["é" * 9000, "\u0001" * 16_000],
    ids=["utf-8-bytes", "json-escapes"],
)
async def test_an_overlong_prompt_is_rejected(client: httpx.AsyncClient, prompt: str) -> None:
    resp = await client.put("/v1/proactive-agent/prompt", json={"prompt": prompt})

    assert resp.status_code == 400, resp.text
    assert (await client.get("/v1/proactive-agent/prompt")).json()["overridden"] is False


async def test_run_now_starts_named_users_tasks_and_reports_the_rest(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    app: Any = client.app  # type: ignore[attr-defined]
    await _create_session(client)

    resp = await client.post(
        "/v1/proactive-agent/runs", json={"users": ["local", "nobody@example.com"]}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] == [
        {"user_id": "local", "task_id": proactive_task_id(None), "run_id": None}
    ]
    assert resp.json()["skipped"] == [{"user_id": "nobody@example.com", "reason": "no_task"}]
    app.state.scheduled_task_run_now.assert_awaited_once()

    ProactiveAgentSettingsStore(db_uri).set_user_enabled("local", False)
    off = await client.post("/v1/proactive-agent/runs", json={"users": ["local"]})
    assert off.json()["skipped"] == [{"user_id": "local", "reason": "turned_off"}]


async def test_run_now_for_everyone(client: httpx.AsyncClient) -> None:
    app: Any = client.app  # type: ignore[attr-defined]
    await _create_session(client)
    app.state.scheduled_task_run_now.return_value = False

    resp = await client.post("/v1/proactive-agent/runs", json={"users": ["*"]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "started": [],
        "skipped": [{"user_id": "local", "reason": "already_running"}],
        "truncated": False,
    }
    mixed = await client.post("/v1/proactive-agent/runs", json={"users": ["*", "local"]})
    assert mixed.status_code == 400


async def test_run_now_for_everyone_stops_scanning_after_the_page_cap() -> None:
    from types import SimpleNamespace

    from omnigent.server.routes import proactive_agent_admin as admin

    pages = {"read": 0}

    class EndlessPausedTasks:
        def list_for_agent(self, agent_id: str, *, after: str | None, limit: int) -> tuple:
            pages["read"] += 1
            return [SimpleNamespace(state="paused")] * limit, f"page-{pages['read']}"

    tasks, truncated = await admin._all_tasks(EndlessPausedTasks())

    assert (tasks, truncated) == ([], True)
    assert pages["read"] == admin._RUN_ALL_MAX_PAGES


async def test_admin_routes_are_hidden_where_the_agent_is_not_offered(
    db_uri: str, tmp_path: Path
) -> None:
    app = _app(db_uri, tmp_path, enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/proactive-agent/prompt")).status_code == 404
        runs = await client.post("/v1/proactive-agent/runs", json={"users": ["*"]})
        assert runs.status_code == 404


async def test_admin_routes_require_an_admin(db_uri: str, tmp_path: Path) -> None:
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("admin@example.com")
    permissions.set_admin("admin@example.com", True)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        agent_cache=None,  # type: ignore[arg-type]
        inbox_item_store=SqlAlchemyInboxItemStore(db_uri),
        proactive_agent=True,
        proactive_agent_settings_store=ProactiveAgentSettingsStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=permissions,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        user = {"X-Forwarded-Email": "alice@example.com"}
        admin = {"X-Forwarded-Email": "admin@example.com"}
        assert (await client.get("/v1/proactive-agent/prompt", headers=user)).status_code == 403
        runs = await client.post("/v1/proactive-agent/runs", json={"users": ["*"]}, headers=user)
        assert runs.status_code == 403
        assert (await client.get("/v1/proactive-agent/prompt", headers=admin)).status_code == 200


@pytest.mark.parametrize("limit", [0, 501])
def test_list_for_agent_bounds_its_page(db_uri: str, limit: int) -> None:
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    with pytest.raises(ValueError):
        SqlAlchemyScheduledTaskStore(db_uri).list_for_agent(PROACTIVE_AGENT_ID, limit=limit)


def test_list_for_agent_pages_one_agents_live_tasks(db_uri: str) -> None:
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    store = SqlAlchemyScheduledTaskStore(db_uri)
    ids = [f"{n:032x}" for n in range(1, 5)]
    for n, task_id in enumerate(ids):
        store.create(
            task_id, f"job {n}", "hi", "FREQ=DAILY", f"user{n}", PROACTIVE_AGENT_ID, "UTC"
        )
    store.create("f" * 32, "other", "hi", "FREQ=DAILY", "user9", "e" * 32, "UTC")
    store.delete(ids[1])

    seen, cursor, pages = [], None, 0
    while True:
        page, cursor = store.list_for_agent(PROACTIVE_AGENT_ID, after=cursor, limit=2)
        seen.extend(task.id for task in page)
        pages += 1
        if cursor is None:
            break

    assert seen == [ids[0], ids[2], ids[3]]
    assert pages == 2
