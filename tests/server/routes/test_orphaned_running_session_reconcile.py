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

import httpx
import pytest
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.routes._sessions.helpers import (
    _session_status_cache,
    reconcile_orphaned_running_status,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


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
