"""Tests for the background time-trigger job scheduler.

Covers the pure schedule parsing/clamping and the tick behaviour: a due,
enabled, agent-bound job spawns a ``scheduled`` run; the overlap guard and the
not-due guard each suppress a second run; and a disabled schedule never fires.
The tick is driven directly (no 5-minute sleep) against the same real stores the
route tests use.
"""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.server.job_scheduler import _parse_schedule_config, _run_scheduler_tick
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.job_store.sqlalchemy_store import SqlAlchemyJobStore


@pytest_asyncio.fixture()
async def agent_id(db_uri: str) -> str:
    """Seed a template agent a scheduled job can bind to and run as."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    aid = generate_agent_id()
    agent_store.create(aid, name="sched-agent", bundle_location="test:///bundle")
    return aid


# ── _parse_schedule_config ──────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        json.dumps({"enabled": False, "interval_minutes": 5}),
        json.dumps({"interval_minutes": 5}),  # enabled missing/falsey
        json.dumps({"enabled": True}),  # no interval
        json.dumps({"enabled": True, "interval_minutes": 0}),
        json.dumps({"enabled": True, "interval_minutes": True}),  # bool, not int
    ],
)
def test_parse_schedule_config_returns_none_when_not_runnable(raw: str | None) -> None:
    """Missing/disabled/malformed configs do not produce a schedule."""
    assert _parse_schedule_config(raw) is None


def test_parse_schedule_config_allows_one_minute() -> None:
    """A one-minute interval is the smallest supported and is preserved."""
    sched = _parse_schedule_config(json.dumps({"enabled": True, "interval_minutes": 1}))
    assert sched is not None
    assert sched.interval_minutes == 1


def test_parse_schedule_config_keeps_larger_interval() -> None:
    """An interval at/above the minimum is preserved."""
    sched = _parse_schedule_config(json.dumps({"enabled": True, "interval_minutes": 30}))
    assert sched is not None
    assert sched.interval_minutes == 30


# ── _run_scheduler_tick ─────────────────────────────────────────────


async def _tick(app: FastAPI, db_uri: str) -> int:
    """Run one scheduler tick against fresh stores over the shared DB."""
    return await _run_scheduler_tick(
        app=app,
        job_store=SqlAlchemyJobStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        agent_store=SqlAlchemyAgentStore(db_uri),
        runner_router=None,
        agent_cache=None,
        permission_store=None,
        file_store=None,
        artifact_store=None,
        liveness_lookup=None,
        default_run_agent_id=None,
    )


async def test_tick_spawns_scheduled_run_for_due_job(
    client: httpx.AsyncClient, app: FastAPI, db_uri: str, agent_id: str
) -> None:
    """An enabled, agent-bound, never-run job fires once and records a scheduled run."""
    job = (
        await client.post(
            "/v1/jobs",
            json={
                "name": "Sched",
                "graph": {},
                "narrative": "go",
                "agent_id": agent_id,
                "schedule_config": {"enabled": True, "interval_minutes": 5},
            },
        )
    ).json()

    spawned = await _tick(app, db_uri)
    assert spawned == 1

    runs = (await client.get(f"/v1/jobs/{job['id']}/runs")).json()
    assert len(runs) == 1
    assert runs[0]["trigger"] == "scheduled"


async def test_tick_skips_when_latest_scheduled_run_still_running(
    client: httpx.AsyncClient, app: FastAPI, db_uri: str, agent_id: str
) -> None:
    """The overlap guard prevents a second scheduled run while one is in flight."""
    await client.post(
        "/v1/jobs",
        json={
            "name": "Overlap",
            "graph": {},
            "narrative": "go",
            "agent_id": agent_id,
            "schedule_config": {"enabled": True, "interval_minutes": 5},
        },
    )
    assert await _tick(app, db_uri) == 1
    # The first run is still "running" (session never advanced), so the next
    # tick is suppressed by the overlap guard rather than the interval.
    assert await _tick(app, db_uri) == 0


async def test_tick_ignores_disabled_schedule(
    client: httpx.AsyncClient, app: FastAPI, db_uri: str, agent_id: str
) -> None:
    """A job whose schedule is disabled never fires, even though it has config."""
    await client.post(
        "/v1/jobs",
        json={
            "name": "Off",
            "graph": {},
            "narrative": "go",
            "agent_id": agent_id,
            "schedule_config": {"enabled": False, "interval_minutes": 5},
        },
    )
    assert await _tick(app, db_uri) == 0


async def test_tick_skips_job_without_agent(
    client: httpx.AsyncClient, app: FastAPI, db_uri: str
) -> None:
    """A scheduled job with no bound agent is skipped (can't run unattended)."""
    await client.post(
        "/v1/jobs",
        json={
            "name": "NoAgent",
            "graph": {},
            "narrative": "go",
            "schedule_config": {"enabled": True, "interval_minutes": 5},
        },
    )
    assert await _tick(app, db_uri) == 0
