"""Scheduled fires under an OIDC auth provider: owner check and timer.

Real app lifespan, SQLAlchemy stores and run-now trigger, authenticated with a
minted session JWT. No host is connected, so a fire that passes the owner check
still fails later; the assertions cover the owner check and the timer only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.passwords import hash_password
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.conftest import ControllableMockClient
from tests.server.integration.oidc_fixtures import TEST_SIGNING_KEY, make_oidc_config

pytestmark = pytest.mark.asyncio

_RUN_RECORDED_TIMEOUT_S = 10.0


@pytest.fixture()
def oidc_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        host_store=HostStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="oidc", oidc_config=make_oidc_config()),
    )


@pytest_asyncio.fixture()
async def oidc_client(
    oidc_app: FastAPI, mock_llm: ControllableMockClient, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    async with oidc_app.router.lifespan_context(oidc_app):
        transport = httpx.ASGITransport(app=oidc_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


def _bearer(user_id: str) -> dict[str, str]:
    token = mint_session_cookie(
        user_id=user_id, cookie_secret=TEST_SIGNING_KEY, ttl_hours=8, provider="oidc"
    )
    return {"Authorization": f"Bearer {token}"}


def _create_body() -> dict[str, object]:
    return {
        "name": "nightly triage",
        "prompt": "triage the queue",
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        "agent_id": builtin_agent_id(CLAUDE_NATIVE_AGENT_NAME),
        "timezone": "UTC",
    }


async def _create_task(client: httpx.AsyncClient, owner: str) -> dict[str, object]:
    resp = await client.post("/v1/scheduled-tasks", json=_create_body(), headers=_bearer(owner))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _run_now_and_wait(
    client: httpx.AsyncClient, task_id: str, owner: str
) -> dict[str, object]:
    resp = await client.post(f"/v1/scheduled-tasks/{task_id}/run", headers=_bearer(owner))
    assert resp.status_code == 202, resp.text
    deadline = time.monotonic() + _RUN_RECORDED_TIMEOUT_S
    while time.monotonic() < deadline:
        runs = (
            await client.get(f"/v1/scheduled-tasks/{task_id}/runs", headers=_bearer(owner))
        ).json()["runs"]
        if runs:
            return runs[0]
        await asyncio.sleep(0.1)
    raise AssertionError(f"run now did not record a run within {_RUN_RECORDED_TIMEOUT_S:.0f}s")


async def test_live_owner_with_account_generation_is_not_deleted(
    oidc_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A task saved without a generation fires for a live owner whose row has one."""
    owner = "alice@example.test"
    SqlAlchemyPermissionStore(db_uri).ensure_user(owner)
    generation = SqlAlchemyAccountStore(db_uri).update_password(owner, hash_password("pw-1234567"))
    assert generation

    created = await _create_task(oidc_client, owner)
    task_id = str(created["id"])
    stored = SqlAlchemyScheduledTaskStore(db_uri).get(task_id)
    print(f"owner generation={generation!r} stored task generation={stored.account_generation!r}")
    assert stored.account_generation is None

    run = await _run_now_and_wait(oidc_client, task_id, owner)
    task = (await oidc_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_bearer(owner))).json()
    assert run["error_code"] != "owner_deleted", (run, task)
    # The owner check passed; with no host connected the fire fails at host resolution.
    assert (run["status"], run["error_code"]) == ("failed", "no_online_host"), run
    assert task["state"] == "active", task
    assert task["next_run_at"] is not None, task


async def test_disabling_fire_drops_the_scheduler_timer(
    oidc_client: httpx.AsyncClient, oidc_app: FastAPI, db_uri: str
) -> None:
    """A fire that disables a task (owner row missing) also unregisters its timer."""
    owner = "ghost@example.test"
    created = await _create_task(oidc_client, owner)
    task_id = str(created["id"])
    scheduler = oidc_app.state.scheduled_task_scheduler
    assert scheduler.next_run_at(task_id) is not None

    run = await _run_now_and_wait(oidc_client, task_id, owner)
    assert run["error_code"] == "owner_deleted", run
    task = (await oidc_client.get(f"/v1/scheduled-tasks/{task_id}", headers=_bearer(owner))).json()
    assert task["state"] == "deleted", task
    assert task["next_run_at"] is None, task
    assert scheduler.next_run_at(task_id) is None
