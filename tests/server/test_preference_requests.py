"""Preference admission shares a budget and retains cancelled worker slots."""

import asyncio
import threading
from contextvars import ContextVar

import pytest
from fastapi import HTTPException

from omnigent.db.db_models import workspace_scope
from omnigent.server import preference_requests as module
from tests._helpers.polling import poll_until_async


@pytest.mark.parametrize("managed", [False, True])
async def test_cancelled_work_keeps_capacity_until_thread_finishes(monkeypatch, managed):
    monkeypatch.setattr(module, "MAX_PREFERENCE_WORKERS", 1)
    started, release = threading.Event(), threading.Event()
    context = ContextVar("request_context", default=None)
    leases = []

    def create_task(coroutine):
        leases.append(context.get())
        task = asyncio.create_task(coroutine)
        task.add_done_callback(lambda _: leases.pop())
        return task

    policy = (
        module.PreferencePolicy(create_task=create_task) if managed else module.PreferencePolicy()
    )

    def work():
        assert context.get() == "workspace-8"

        started.set()
        assert release.wait(10)
        return "finished"

    token = context.set("workspace-8")
    task = asyncio.create_task(policy.run("alice", work))
    context.reset(token)
    try:
        await poll_until_async(started.is_set, timeout_s=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(HTTPException) as rejected:
            await module.run_preference_work("bob", lambda: "unexpected")
        assert rejected.value.status_code == 429
        assert leases == (["workspace-8"] if managed else [])
    finally:
        release.set()
    await poll_until_async(
        lambda: not module._limits[asyncio.get_running_loop()].active, timeout_s=5
    )
    assert not leases
    assert await module.run_preference_work("bob", lambda: "recovered") == "recovered"


async def test_rate_limit_is_per_account_and_workspace(monkeypatch):
    monkeypatch.setattr(module, "MAX_PREFERENCE_REQUESTS_PER_MINUTE", 1)
    assert await module.run_preference_work("alice", lambda: "first") == "first"
    with pytest.raises(HTTPException) as rejected:
        await module.run_preference_work("alice", lambda: "unexpected")
    assert rejected.value.status_code == 429
    assert await module.run_preference_work("bob", lambda: "other-user") == "other-user"
    with workspace_scope(8):
        assert (
            await module.run_preference_work("alice", lambda: "other-workspace")
            == "other-workspace"
        )


async def test_http_and_owner_tunnel_share_admission_budget(db_uri, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from omnigent.preference_broker import PreferenceRead
    from omnigent.preference_controls import PreferenceInspect
    from omnigent.server.preference_broker import PreferenceBrokerGrant
    from omnigent.server.routes.preferences import create_preferences_router
    from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore

    monkeypatch.setattr(module, "MAX_PREFERENCE_WORKERS", 1)
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="admission")
    app = FastAPI()
    app.include_router(create_preferences_router(store), prefix="/v1")
    started, release = threading.Event(), threading.Event()
    read = store.read

    def delayed_read(**kwargs):
        started.set()
        assert release.wait(10)
        return read(**kwargs)

    monkeypatch.setattr(store, "read", delayed_read)
    grant = PreferenceBrokerGrant(store, None, "host", PreferenceInspect(action="status"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        waiting = asyncio.create_task(http.get("/v1/preferences/shared"))
        try:
            await poll_until_async(started.is_set, timeout_s=5)
            assert (await grant.execute(PreferenceRead())).status == 429
            assert (await http.get("/v1/preferences/shared")).status_code == 429
        finally:
            release.set()
        assert (await waiting).json()["revision"] == 0
        assert (await grant.execute(PreferenceRead())).status == 200


async def test_task_factory_failure_closes_work_without_holding_capacity():
    coroutines = []

    def unavailable(coroutine):
        coroutines.append(coroutine)
        raise RuntimeError("lease unavailable")

    policy = module.PreferencePolicy(create_task=unavailable)
    with pytest.raises(RuntimeError, match="lease unavailable"):
        await policy.run("alice", lambda: pytest.fail("must not run without a lease"))
    assert coroutines[0].cr_frame is None
    assert not module._limits[asyncio.get_running_loop()].active
    assert await module.run_preference_work("alice", lambda: "recovered") == "recovered"
