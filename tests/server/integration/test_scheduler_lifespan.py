"""Tests for the recurring-task scheduler's FastAPI lifespan wiring.

Verifies that :func:`omnigent.server.app.create_app`, when given a
``scheduled_task_store``, starts an :class:`ScheduledTaskScheduler` on lifespan
entry (arming a timer per active task) and stops it on exit. When no store is
supplied the scheduler is absent entirely.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI

from omnigent.db.db_models import workspace_scope
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.managed_hosts import ManagedSandboxDeployment
from omnigent.server.scheduled.config import ScheduledTaskSchedulerConfig
from omnigent.server.scheduled.dispatch_scheduler import (
    PersistentTaskScheduler,
    ScheduledTaskDispatchHooks,
)
from omnigent.server.scheduled.scheduler import ScheduledTaskScheduler
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.scheduled_task_store import ScheduledTaskStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
    SqlAlchemyScheduledTaskStore,
)

pytestmark = pytest.mark.asyncio


def _uid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    scheduled_task_store: ScheduledTaskStore | None,
    dispatch: ScheduledTaskDispatchHooks | None = None,
    scheduler_config: ScheduledTaskSchedulerConfig | None = None,
    server_config: dict | None = None,
    sandbox_config: ManagedSandboxDeployment | None = None,
) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        scheduled_task_store=scheduled_task_store,
        scheduled_task_dispatch=dispatch,
        scheduled_task_scheduler_config=scheduler_config,
        server_config=server_config,
        sandbox_config=sandbox_config,
        host_store=HostStore(db_uri) if sandbox_config is not None else None,
    )


async def test_lifespan_starts_and_stops_scheduler(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """With a store containing an active task, the lifespan starts a scheduler
    with one armed job and stops it (dropping the job) on exit."""
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.create(
        scheduled_task_id=_uid("nightly"),
        name="nightly triage",
        prompt="triage the queue",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id=None,
        agent_id=_uid("agent-1"),
        timezone="America/Los_Angeles",
    )
    app = _build_app(db_uri, tmp_path, scheduled_task_store=store)

    async with app.router.lifespan_context(app):
        scheduler = app.state.scheduled_task_scheduler
        assert scheduler.is_started
        assert scheduler.job_count == 1

    # After lifespan exit the scheduler is stopped and its jobs are cleared.
    assert not scheduler.is_started
    assert scheduler.job_count == 0


async def test_lifespan_arms_active_task_from_non_default_workspace(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """Startup loads active tasks across workspaces, not just ambient workspace 0."""
    store = SqlAlchemyScheduledTaskStore(db_uri)
    with workspace_scope(42):
        task = store.create(
            scheduled_task_id=_uid("tenant-nightly"),
            name="tenant nightly triage",
            prompt="triage the queue",
            rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            user_id=None,
            agent_id=_uid("agent-1"),
            timezone="America/Los_Angeles",
        )
    app = _build_app(db_uri, tmp_path, scheduled_task_store=store)

    async with app.router.lifespan_context(app):
        scheduler = app.state.scheduled_task_scheduler
        assert scheduler.is_started
        assert scheduler.job_count == 1
        assert scheduler.next_run_at(uuid.UUID(task.id).hex) is not None


async def test_lifespan_survives_scheduler_start_failure(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure loading the schedule at boot is logged and swallowed."""
    store = SqlAlchemyScheduledTaskStore(db_uri)

    def _boom() -> list:
        raise RuntimeError("db is down")

    monkeypatch.setattr(store, "list_active_all_workspaces", _boom)
    app = _build_app(db_uri, tmp_path, scheduled_task_store=store)

    # Entering the lifespan must not raise despite start() blowing up.
    async with app.router.lifespan_context(app):
        assert app.state.scheduled_task_scheduler is None
        retry = next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "scheduled-task-startup-retry"
        )
    assert retry.done()


async def test_lifespan_without_store_has_no_scheduler(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """When no ``scheduled_task_store`` is supplied, no scheduler is attached."""
    app = _build_app(db_uri, tmp_path, scheduled_task_store=None)
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "scheduled_task_scheduler", None) is None


async def test_lifespan_skips_paused_task(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """A paused task is not armed at boot."""
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.create(
        scheduled_task_id=_uid("paused"),
        name="paused task",
        prompt="do nothing",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id=None,
        agent_id=_uid("agent-1"),
        timezone="UTC",
        state="paused",
    )
    app = _build_app(db_uri, tmp_path, scheduled_task_store=store)
    async with app.router.lifespan_context(app):
        assert app.state.scheduled_task_scheduler.job_count == 0


@pytest.mark.parametrize("config_source", ["defaults", "empty_yaml", "yaml", "injected"])
async def test_lifespan_database_switch_dispatches_claim_and_drains(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_source: str,
) -> None:
    # Create a task whose persisted occurrence is already due at server startup.
    import time

    from omnigent.entities import ScheduledTask
    from omnigent.server.scheduled.dispatch_scheduler import StartupLease
    from omnigent.stores.scheduled_task_store import sqlalchemy_store

    monkeypatch.setenv("OMNIGENT_FEATURES", "automations_v2")
    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: int(time.time()) - 3600)
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    task = store.create(
        _uid("dispatch"), "dispatch", "prompt", "FREQ=HOURLY", None, _uid("agent"), "UTC"
    )
    store.configure_dispatch(False)
    settings = ScheduledTaskSchedulerConfig()
    injected_config = None
    if config_source == "empty_yaml":
        config_path = tmp_path / "server.yaml"
        config_path.write_text("scheduled_task_scheduler:\n")
        monkeypatch.setenv("OMNIGENT_CONFIG", str(config_path))
    elif config_source != "defaults":
        config_path = tmp_path / "server.yaml"
        config_path.write_text(
            "scheduled_task_scheduler:\n"
            "  poll_seconds: 5\n"
            "  renewal_seconds: 10\n"
            "  lease_seconds: 240\n"
            "  batch_size: 7\n"
            "  max_startups: 3\n"
            "  recovery_seconds: 15\n"
        )
        monkeypatch.setenv("OMNIGENT_CONFIG", str(config_path))
        settings = ScheduledTaskSchedulerConfig(
            poll_seconds=5,
            renewal_seconds=10,
            lease_seconds=240,
            batch_size=7,
            max_startups=3,
            recovery_seconds=15,
        )
        if config_source == "injected":
            # Explicit deployment settings take precedence over the YAML section.
            config_path.write_text("scheduled_task_scheduler: invalid\n")
            injected_config = settings
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fire(snapshot: ScheduledTask, lease: StartupLease) -> None:
        assert snapshot.id == uuid.UUID(task.id).hex
        assert lease.run.status == "starting"
        assert lease.run.lease_expires_at == pytest.approx(
            time.time() + settings.lease_seconds, abs=10
        )
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    app = _build_app(
        db_uri,
        tmp_path,
        scheduled_task_store=store,
        dispatch=ScheduledTaskDispatchHooks(fire, AsyncMock(return_value=None)),
        scheduler_config=injected_config,
    )
    async with app.router.lifespan_context(app):
        scheduler = app.state.scheduled_task_scheduler
        assert isinstance(scheduler, PersistentTaskScheduler)
        for name, value in settings.model_dump(exclude={"startup_retry_seconds"}).items():
            assert getattr(scheduler, f"_{name}") == value
        new_task = store.create(
            _uid("new-dispatch"), "new", "prompt", "FREQ=DAILY", None, _uid("agent"), "UTC"
        )
        assert new_task.next_run_at is not None
        await asyncio.wait_for(started.wait(), 10)
        assert len(store.list_runs(task.id)[0]) == 1
    assert stopped.is_set()
    assert not scheduler.is_started
    assert store.list_runs(task.id)[0][0].status == "starting"


@pytest.mark.parametrize("features", [None, "-automations_v2", "automations_v2,-automations_v2"])
async def test_automations_v2_flag_off_keeps_timers_even_with_hooks(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: str | None,
) -> None:
    if features is None:
        monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_FEATURES", features)
    fire, recover = AsyncMock(), AsyncMock()
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    tasks = []
    for workspace in (0, 42):
        with workspace_scope(workspace):
            tasks.append(
                store.create(
                    _uid("before-rollback"),
                    "rollback",
                    "prompt",
                    "FREQ=DAILY",
                    None,
                    _uid("agent"),
                    "UTC",
                )
            )
    assert all(task.next_run_at is not None for task in tasks)
    app = _build_app(
        db_uri,
        tmp_path,
        scheduled_task_store=store,
        dispatch=ScheduledTaskDispatchHooks(fire, recover),
        server_config={"scheduled_task_scheduler": {"lease_seconds": -1}},
    )
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.scheduled_task_scheduler, ScheduledTaskScheduler)
        for task in tasks:
            with workspace_scope(task.workspace_id):
                assert store.get(task.id).next_run_at is None
        assert (
            store.create(
                _uid("after-rollback"),
                "legacy",
                "prompt",
                "FREQ=DAILY",
                None,
                _uid("agent"),
                "UTC",
            ).next_run_at
            is None
        )
        fire.assert_not_awaited()
        recover.assert_not_awaited()


@pytest.mark.parametrize("has_store,has_hooks", [(True, False), (False, True), (False, False)])
async def test_automations_v2_flag_requires_dispatch_dependencies(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_store: bool,
    has_hooks: bool,
) -> None:
    monkeypatch.setenv("OMNIGENT_FEATURES", "automations_v2")
    with pytest.raises(ValueError, match="automations_v2 requires"):
        _build_app(
            db_uri,
            tmp_path,
            scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri) if has_store else None,
            dispatch=ScheduledTaskDispatchHooks(AsyncMock(), AsyncMock()) if has_hooks else None,
        )


async def test_automations_v2_rejects_store_without_dispatch_operations(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_FEATURES", "automations_v2")
    with pytest.raises(TypeError, match="does not implement the operations"):
        _build_app(
            db_uri,
            tmp_path,
            scheduled_task_store=Mock(spec=ScheduledTaskStore),
            dispatch=ScheduledTaskDispatchHooks(AsyncMock(), AsyncMock()),
        )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"lease_seconds": 30}, "renewal_seconds must be shorter"),
        (False, "valid dictionary"),
        (0, "valid dictionary"),
        ("", "valid dictionary"),
        ([], "valid dictionary"),
    ],
)
async def test_automations_v2_rejects_invalid_scheduler_config_at_app_creation(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings,
    message: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_FEATURES", "automations_v2")
    with pytest.raises(ValueError, match=message):
        _build_app(
            db_uri,
            tmp_path,
            scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
            dispatch=ScheduledTaskDispatchHooks(AsyncMock(), AsyncMock()),
            server_config={"scheduled_task_scheduler": settings},
        )


@pytest.mark.parametrize("config_source", ["yaml", "injected"])
@pytest.mark.parametrize("failure", ["reset_dispatch_cursors", "list_active_all_workspaces"])
async def test_legacy_startup_retries_without_skipping_cursor_reset(
    runtime_init, db_uri, tmp_path, monkeypatch, failure, config_source
):
    from tests._helpers.polling import poll_until_async

    monkeypatch.setenv("OMNIGENT_FEATURES", "-automations_v2")
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    task = store.create(
        _uid("retry-start"), "retry", "prompt", "FREQ=DAILY", None, _uid("agent"), "UTC"
    )
    original = getattr(store, failure)
    attempts = 0

    def unavailable_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database outage")
        return original()

    monkeypatch.setattr(store, failure, unavailable_once)
    app = _build_app(
        db_uri,
        tmp_path,
        scheduled_task_store=store,
        server_config={
            "scheduled_task_scheduler": {
                "startup_retry_seconds": 0.01 if config_source == "yaml" else 60,
                "lease_seconds": -1,
            }
        },
        scheduler_config=(
            ScheduledTaskSchedulerConfig(startup_retry_seconds=0.01)
            if config_source == "injected"
            else None
        ),
    )
    async with app.router.lifespan_context(app):
        assert app.state.scheduled_task_scheduler is None
        await poll_until_async(
            lambda: (
                app.state.scheduled_task_scheduler is not None
                and app.state.scheduled_task_scheduler.is_started
            ),
            timeout_s=10,
        )
        scheduler = app.state.scheduled_task_scheduler
        assert scheduler.is_started
        assert scheduler.job_count == 1
        assert store.get(task.id).next_run_at is None
        assert attempts == 2
    assert not scheduler.is_started


async def test_persistent_startup_uses_configured_retry(
    runtime_init, db_uri, tmp_path, monkeypatch
):
    from tests._helpers.polling import poll_until_async

    monkeypatch.setenv("OMNIGENT_FEATURES", "automations_v2")
    start = PersistentTaskScheduler.start
    attempts = 0

    async def unavailable_once(self):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database outage")
        await start(self)

    monkeypatch.setattr(PersistentTaskScheduler, "start", unavailable_once)
    app = _build_app(
        db_uri,
        tmp_path,
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        dispatch=ScheduledTaskDispatchHooks(AsyncMock(), AsyncMock()),
        server_config={"scheduled_task_scheduler": {"startup_retry_seconds": 0.01}},
    )
    async with app.router.lifespan_context(app):
        assert app.state.scheduled_task_scheduler is None
        await poll_until_async(
            lambda: (
                app.state.scheduled_task_scheduler is not None
                and app.state.scheduled_task_scheduler.is_started
            ),
            timeout_s=10,
        )
        assert app.state.scheduled_task_scheduler.is_started
        assert attempts == 2


async def test_legacy_rejects_invalid_retry_interval(runtime_init, db_uri, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_FEATURES", "-automations_v2")
    with pytest.raises(ValueError, match="startup_retry_seconds"):
        _build_app(
            db_uri,
            tmp_path,
            scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
            server_config={"scheduled_task_scheduler": {"startup_retry_seconds": 0}},
        )


@pytest.mark.parametrize("operation", ["reset_dispatch_cursors", "list_active_all_workspaces"])
async def test_legacy_startup_database_work_leaves_event_loop_responsive(
    runtime_init, db_uri, tmp_path, monkeypatch, operation
):
    import threading

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    store = SqlAlchemyScheduledTaskStore(db_uri)
    original = getattr(store, operation)

    def blocked():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "Database read blocked the event loop"
        return original()

    monkeypatch.setenv("OMNIGENT_FEATURES", "-automations_v2")
    monkeypatch.setattr(store, operation, blocked)
    app = _build_app(db_uri, tmp_path, scheduled_task_store=store)
    lifespan = app.router.lifespan_context(app)
    startup = asyncio.create_task(lifespan.__aenter__())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert not startup.done()
    finally:
        release.set()
        await startup
        await lifespan.__aexit__(None, None, None)


@pytest.mark.parametrize("reset_fails", [False, True])
async def test_shutdown_waits_for_cursor_reset_without_starting_timers(
    runtime_init, db_uri, tmp_path, monkeypatch, reset_fails
):
    import threading

    from tests._helpers.polling import poll_until_async

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    attempts = 0
    store = SqlAlchemyScheduledTaskStore(db_uri)
    original = store.reset_dispatch_cursors

    def blocked_reset():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database outage")
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(10), "Shutdown never released the database operation"
            if reset_fails:
                raise RuntimeError("database reset failed")
            return original()
        finally:
            finished.set()

    monkeypatch.setenv("OMNIGENT_FEATURES", "-automations_v2")
    monkeypatch.setattr(store, "reset_dispatch_cursors", blocked_reset)
    load_tasks = Mock(wraps=store.list_active_all_workspaces)
    monkeypatch.setattr(store, "list_active_all_workspaces", load_tasks)
    app = _build_app(
        db_uri,
        tmp_path,
        scheduled_task_store=store,
        scheduler_config=ScheduledTaskSchedulerConfig(startup_retry_seconds=0.01),
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        retry = next(
            t for t in asyncio.all_tasks() if t.get_name() == "scheduled-task-startup-retry"
        )
        shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
        await poll_until_async(lambda: retry.cancelling(), timeout_s=10)
        assert not shutdown.done()
        assert not finished.is_set()
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        else:
            await lifespan.__aexit__(None, None, None)
    assert finished.is_set()
    assert retry.done()
    load_tasks.assert_not_called()
    assert app.state.scheduled_task_scheduler is None


@pytest.mark.parametrize("mode", ["legacy", "persistent", "retrying"])
async def test_shutdown_stops_scheduling_before_sandbox_reaper(
    runtime_init, db_uri, tmp_path, monkeypatch, mode
):
    from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxReaperConfig
    from omnigent.server.managed_sandbox_reaper import ManagedSandboxReaper

    monkeypatch.setenv(
        "OMNIGENT_FEATURES", "automations_v2" if mode == "persistent" else "-automations_v2"
    )
    store = SqlAlchemyScheduledTaskStore(db_uri)
    if mode == "retrying":
        monkeypatch.setattr(
            store, "list_active_all_workspaces", Mock(side_effect=RuntimeError("DB unavailable"))
        )
    sandbox = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.com",
            launcher_factory=Mock(side_effect=AssertionError("No sandbox should launch")),
            managed_launch_supported=False,
            token_ttl_s=3600,
        ),
        reaper=ManagedSandboxReaperConfig(enabled=True),
    )
    app = _build_app(
        db_uri,
        tmp_path,
        scheduled_task_store=store,
        sandbox_config=sandbox,
        dispatch=ScheduledTaskDispatchHooks(AsyncMock(), AsyncMock()),
    )
    scheduler = None
    retry = None

    async def check_shutdown():
        if retry is not None:
            assert retry.done()
        else:
            assert scheduler is not None
            assert not scheduler.is_started

    shutdown = AsyncMock(side_effect=check_shutdown)
    monkeypatch.setattr(ManagedSandboxReaper, "start", AsyncMock())
    monkeypatch.setattr(ManagedSandboxReaper, "shutdown", shutdown)
    async with app.router.lifespan_context(app):
        scheduler = app.state.scheduled_task_scheduler
        if mode == "retrying":
            retry = next(
                task
                for task in asyncio.all_tasks()
                if task.get_name() == "scheduled-task-startup-retry"
            )
        else:
            assert scheduler.is_started
    shutdown.assert_awaited_once()
