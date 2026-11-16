"""Lazy-on-read reconciliation of orphaned "running" sessions.

Targeted, fast coverage for the two route-layer fixes that keep a
runner-less session from being stuck as "running" forever:

* ``GET /v1/sessions`` (``routes_core.list_sessions``) settles a row that
  still reads running/waiting but whose runner is confirmed gone.
* ``POST /v1/sessions/{id}/events`` with ``stop_session``
  (``routes_events``) settles the same orphaned row instead of returning a
  false success over a still-"running" row.

Both route through :func:`reconcile_orphaned_running_status`, whose own
``failed``-sticky invariant is unit-tested directly.

The reconciliation only fires when the runner is confirmed gone from every
replica (no live tunnel here AND ``runner_last_seen`` stale past the TTL);
each facet is paired with a fresh-runner control that must be left running,
proving the grace-window guard.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlHost, current_workspace_id
from omnigent.db.utils import generate_agent_id, get_or_create_engine, now_epoch
from omnigent.server.host_session_recovery import HostSessionRecoverer
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.routes._sessions import helpers, orchestration
from omnigent.server.routes._sessions.helpers import (
    _session_status_cache,
    reconcile_orphaned_running_status,
)
from omnigent.server.routes._sessions.orchestration import HOST_OFFLINE_HOLD_S
from omnigent.server.schemas import ErrorDetail
from omnigent.session_recovery import RECOVERY_LABEL_PREFIX, SessionRecovery
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore


def _seed_running_session(db_uri: str, *, runner_fresh: bool) -> str:
    """Seed a session persisted as ``running`` with a bound runner.

    :param db_uri: SQLite database URI shared with the test app.
    :param runner_fresh: When ``True``, stamp ``runner_last_seen`` now so
        the runner reads reachable (alive on another replica within the
        grace window); when ``False``, leave it unset so the runner reads
        confirmed-gone.
    :returns: The seeded session/conversation id.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(
        agent_id,
        name=f"orphan-agent-{agent_id}",
        bundle_location="test:///bundle",
    )
    conv = conv_store.create_conversation(agent_id=agent_id)
    runner_id = f"runner_{conv.id}"
    assert conv_store.set_runner_id(conv.id, runner_id)
    conv_store.set_session_live_status(conv.id, "running")
    if runner_fresh:
        conv_store.touch_runner_liveness([runner_id], int(time.time()))
    # Ensure no stale relay-cache entry: the reconciliation's suspect gate
    # is a cache MISS (the "running" came from the DB mirror, not a runner
    # this replica is actively relaying).
    _session_status_cache.pop(conv.id, None)
    return conv.id


@pytest_asyncio.fixture(autouse=True)
def _isolate_status_cache() -> None:
    """Keep the module-level relay status cache from leaking across tests."""
    snapshot = dict(_session_status_cache)
    yield
    _session_status_cache.clear()
    _session_status_cache.update(snapshot)


# ── reconcile_orphaned_running_status helper ─────────────────────────────


def test_reconcile_is_conditional_and_marks_scheduled_run_incomplete(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a stale running row settles, and a scheduled fire fails."""
    from omnigent.server import session_live_state

    sid = _seed_running_session(db_uri, runner_fresh=False)
    store = SqlAlchemyConversationStore(db_uri)
    completions: list[tuple[str, str, str | None]] = []

    def _record_completion(
        conversation_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error: str | None = None,
    ) -> None:
        del error
        completions.append((conversation_id, status, error_code))

    monkeypatch.setattr(session_live_state, "persist_scheduled_run_completion", _record_completion)

    assert reconcile_orphaned_running_status(sid, store, int(time.time()) - 90)
    assert store.get_conversation(sid).live_status == "idle"  # type: ignore[union-attr]
    assert _session_status_cache[sid] == "idle"
    assert completions == [(sid, "failed", "incomplete")]
    assert not reconcile_orphaned_running_status(sid, store, int(time.time()) - 90)

    fresh_sid = _seed_running_session(db_uri, runner_fresh=True)
    assert not reconcile_orphaned_running_status(fresh_sid, store, int(time.time()) - 90)
    fresh = store.get_conversation(fresh_sid)
    assert fresh is not None
    assert fresh.live_status == "running"


@pytest.mark.parametrize(
    ("enabled", "phase"),
    [
        pytest.param(False, None, id="opt-out"),
        pytest.param(True, None, id="opt-in"),
        pytest.param(True, "stopped", id="stopped"),
        pytest.param(True, "failed", id="failed"),
    ],
)
@pytest.mark.parametrize("path", ["orphan", "offline"])
async def test_interrupted_runner_bookkeeping_requires_recovery_opt_in(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    phase: Literal["stopped", "failed"] | None,
    path: str,
) -> None:
    sid = _seed_running_session(db_uri, runner_fresh=False)
    store = SqlAlchemyConversationStore(db_uri)
    host_id = "a" * 32
    store.set_host_id(sid, host_id, workspace="/workspace")
    conv = store.get_conversation(sid)
    assert conv is not None and conv.runner_id is not None
    previous = (
        SessionRecovery(host_id, conv.runner_id, now_epoch(), phase=phase)
        if phase is not None
        else None
    )
    if previous is not None:
        assert store.replace_session_recovery(sid, None, previous)
    config = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.test",
            launcher_factory=Mock(),
            token_ttl_s=3600,
            provider="test",
            host_config={"host": {"recover_interrupted_sessions": enabled}},
        )
    )
    service = HostSessionRecoverer(
        store,
        Mock(get=Mock(return_value=None)),
        Mock(
            get_host=Mock(return_value=SimpleNamespace(deleted_at=None, sandbox_provider="test"))
        ),
        Mock(),
        Mock(),
        sandbox_config=config,
    )
    publish_status = Mock()
    monkeypatch.setattr(helpers, "_publish_status", publish_status)
    monkeypatch.setattr(orchestration, "_publish_status", publish_status)
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    if path == "orphan":
        assert reconcile_orphaned_running_status(
            sid, store, int(time.time()) - 90, host_session_recoverer=service
        )
    else:
        await orchestration._mark_runner_sessions_offline_impl(
            [conv], error, store, host_session_recoverer=service
        )
    recovery = store.get_session_recovery(sid)
    if enabled and previous is None:
        assert recovery is not None
        assert (recovery.host_id, recovery.runner_id, recovery.phase) == (
            host_id,
            conv.runner_id,
            "pending",
        )
        publish_status.assert_not_called()
    else:
        assert recovery == previous
        current = store.get_conversation(sid)
        assert current is not None
        assert not any(key.startswith(RECOVERY_LABEL_PREFIX) for key in current.labels)
        if path == "orphan":
            publish_status.assert_called_once_with(
                sid, "idle", persist_live_status=False, scheduled_run_outcome="failed"
            )
        else:
            publish_status.assert_called_once_with(
                sid,
                "failed",
                error,
                failure_origin="runner_offline_sweep",
                disconnect_evidence=None,
            )


# ── Facet 1: GET /v1/sessions settles orphaned "running" rows ────────────


async def test_list_reconciles_orphaned_running_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A persisted-running session whose runner is confirmed gone reads
    "idle" in the list, not a phantom "running"."""
    session_id = _seed_running_session(db_uri, runner_fresh=False)

    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    item = next(s for s in resp.json()["data"] if s["id"] == session_id)
    assert item["status"] == "idle"


async def test_list_leaves_running_session_with_fresh_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A running session whose runner is fresh (alive on another replica
    within the grace window) is left running — the reconciliation must not
    fire while the runner could still be executing the turn."""
    session_id = _seed_running_session(db_uri, runner_fresh=True)

    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    item = next(s for s in resp.json()["data"] if s["id"] == session_id)
    assert item["status"] == "running"


# ── Facet 2: stop_session settles instead of false-succeeding ────────────


async def test_stop_reconciles_orphaned_running_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Stopping a runner-less session that still reads "running" settles it
    to idle so the 2xx success is honest, not a phantom stop over a
    still-"running" row."""
    session_id = _seed_running_session(db_uri, runner_fresh=False)

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
    )
    assert 200 <= resp.status_code < 300
    # The stop handler settles the status synchronously via the publish
    # chokepoint; assert the cache directly so this isolates the stop-path
    # fix from the list-path fix.
    assert _session_status_cache.get(session_id) == "idle"


async def test_stop_leaves_running_session_with_fresh_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A stop that can't reach a still-fresh runner does NOT force the
    session idle — it might be executing on another replica within the
    grace window."""
    session_id = _seed_running_session(db_uri, runner_fresh=True)

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "stop_session", "data": {}},
    )
    assert 200 <= resp.status_code < 300
    # No reconciliation fired: the relay cache was never written to idle.
    assert _session_status_cache.get(session_id) != "idle"


# ── Facet 3: GET child_sessions settles orphaned "running" children ──────


def _seed_running_child(db_uri: str, *, runner_fresh: bool) -> tuple[str, str]:
    """Seed a parent session and a sub-agent child persisted as ``running``.

    :returns: ``(parent_id, child_id)``.
    """
    parent_id = _seed_running_session(db_uri, runner_fresh=True)
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = conv_store.get_conversation(parent_id)
    assert parent is not None
    child = conv_store.create_conversation(
        agent_id=parent.agent_id,
        kind="sub_agent",
        parent_conversation_id=parent_id,
        title="coder:auth",
    )
    runner_id = f"runner_{child.id}"
    assert conv_store.set_runner_id(child.id, runner_id)
    conv_store.set_session_live_status(child.id, "running")
    if runner_fresh:
        conv_store.touch_runner_liveness([runner_id], int(time.time()))
    _session_status_cache.pop(child.id, None)
    return parent_id, child.id


async def test_child_sessions_reconciles_orphaned_running_child(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Runners poll this list for in-flight sub-agents, so a child whose
    runner is confirmed gone must read settled there too, as in the list."""
    parent_id, child_id = _seed_running_child(db_uri, runner_fresh=False)

    resp = await client.get(f"/v1/sessions/{parent_id}/child_sessions")
    assert resp.status_code == 200
    row = next(c for c in resp.json()["data"] if c["id"] == child_id)
    assert row["busy"] is False
    assert row["current_task_status"] == "completed"


# ── Facet 4: hostless sub-agents inherit their parent's sleeping-host hold ──

_HOST_ID = "ab" * 16


def _seed_hostless_child_of_host_bound_parent(db_uri: str, *, host_age_s: int) -> tuple[str, str]:
    """Seed a host-bound parent with an expired heartbeat and a hostless child.

    The child copies the parent's runner but has no host binding of its own.
    The parent's host is offline, with its row last seen ``host_age_s`` ago.

    :returns: ``(parent_id, child_id)``.
    """
    parent_id = _seed_running_session(db_uri, runner_fresh=False)
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = conv_store.get_conversation(parent_id)
    assert parent is not None
    assert parent.runner_id is not None
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(_HOST_ID, "laptop", "alice@example.com")
    conv_store.set_host_id(parent_id, _HOST_ID, workspace="/work/repo")
    host_store.set_offline(_HOST_ID)
    with Session(get_or_create_engine(db_uri)) as session:
        session.execute(
            update(SqlHost)
            .where(SqlHost.workspace_id == current_workspace_id(), SqlHost.host_id == _HOST_ID)
            .values(updated_at=now_epoch() - host_age_s)
        )
        session.commit()
    child = conv_store.create_conversation(
        agent_id=parent.agent_id,
        kind="sub_agent",
        parent_conversation_id=parent_id,
        title="coder:auth",
    )
    assert conv_store.set_runner_id(child.id, parent.runner_id)
    conv_store.set_session_live_status(child.id, "running")
    _session_status_cache.pop(child.id, None)
    return parent_id, child.id


async def test_child_sessions_holds_hostless_child_within_parent_host_hold(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A hostless child whose parent's host is asleep inside the hold keeps running.

    Its runner's heartbeat is expired and no local status entry remains, so
    only the inherited host hold can keep the child from settling.
    """
    app.state.host_store = HostStore(db_uri)
    parent_id, child_id = _seed_hostless_child_of_host_bound_parent(db_uri, host_age_s=60)

    resp = await client.get(f"/v1/sessions/{parent_id}/child_sessions")
    assert resp.status_code == 200
    row = next(c for c in resp.json()["data"] if c["id"] == child_id)
    assert row["current_task_status"] != "completed"
    child = SqlAlchemyConversationStore(db_uri).get_conversation(child_id)
    assert child is not None
    assert child.live_status == "running"


async def test_child_sessions_settles_hostless_child_after_parent_host_hold_expires(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Once the parent's host has been offline past the hold, the child settles."""
    app.state.host_store = HostStore(db_uri)
    expired_age_s = int(HOST_OFFLINE_HOLD_S) + 2 * RUNNER_LIVENESS_TTL_S + 60
    parent_id, child_id = _seed_hostless_child_of_host_bound_parent(
        db_uri, host_age_s=expired_age_s
    )

    resp = await client.get(f"/v1/sessions/{parent_id}/child_sessions")
    assert resp.status_code == 200
    row = next(c for c in resp.json()["data"] if c["id"] == child_id)
    assert row["busy"] is False
    assert row["current_task_status"] == "completed"


async def test_child_sessions_leaves_child_with_fresh_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child whose runner is fresh reads in-flight from the persisted status
    even on a replica whose status cache has no entry for it."""
    parent_id, child_id = _seed_running_child(db_uri, runner_fresh=True)

    resp = await client.get(f"/v1/sessions/{parent_id}/child_sessions")
    assert resp.status_code == 200
    row = next(c for c in resp.json()["data"] if c["id"] == child_id)
    assert row["busy"] is True
    assert row["current_task_status"] == "in_progress"
