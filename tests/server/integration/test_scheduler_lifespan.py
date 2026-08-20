"""Tests for the recurring-task scheduler's FastAPI lifespan wiring.

Verifies that :func:`omnigent.server.app.create_app`, when given a
``scheduled_task_store``, starts an :class:`ScheduledTaskScheduler` on lifespan
entry (arming a timer per active task) and stops it on exit. When no store is
supplied the scheduler is absent entirely.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI

from omnigent.db.db_models import workspace_scope
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
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
    scheduled_task_store: SqlAlchemyScheduledTaskStore | None,
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
        scheduler = app.state.scheduled_task_scheduler
        assert scheduler is not None
        assert not scheduler.is_started
        assert scheduler.job_count == 0


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
