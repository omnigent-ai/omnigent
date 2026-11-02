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

A third facet covers a live runner: its own read of a Claude-native session
whose turn ended without an idle edge, quiet for hours, settles the session
so the runner can idle out (see :func:`_settle_quiet_native_turn`).

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

from omnigent._wrapper_labels import WRAPPER_LABEL_KEY
from omnigent.db.utils import generate_agent_id
from omnigent.native.native_coding_agents import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
)
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions import orchestration
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


# ── Facet 3: a live runner's own read settles a quiet Claude turn ────────

# The query a runner's metadata read sends (``_SESSION_METADATA_PARAMS``).
_RUNNER_READ = (
    "include_items=false&include_liveness=false&include_usage=false&include_live_status=false"
)


def _seed_claude_turn(
    db_uri: str,
    *,
    wrapper: str = CLAUDE_NATIVE_CODING_AGENT.wrapper_label,
) -> str:
    """Seed a running Claude-native session whose live runner relays its status.

    :param db_uri: SQLite database URI shared with the test app.
    :param wrapper: Wrapper label naming the session's native harness.
    :returns: The seeded session/conversation id.
    """
    session_id = _seed_running_session(db_uri, runner_fresh=True)
    SqlAlchemyConversationStore(db_uri).set_labels(session_id, {WRAPPER_LABEL_KEY: wrapper})
    _session_status_cache[session_id] = "running"
    return session_id


async def test_runner_read_settles_a_claude_turn_quiet_for_hours(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner's own read of a long-quiet Claude turn answers, and stores, ``idle``.

    A display read of the same session does not settle it: only the runner asks
    once its pane has gone quiet, and only it adopts the answer. A runner that
    reports the turn again over the same silent transcript keeps it running.
    """
    session_id = _seed_claude_turn(db_uri)
    monkeypatch.setattr(orchestration, "_QUIET_NATIVE_TURN_SETTLE_S", 0)

    display = await client.get(f"/v1/sessions/{session_id}?include_items=false")
    assert display.status_code == 200
    assert display.json()["status"] == "running"

    resp = await client.get(f"/v1/sessions/{session_id}?{_RUNNER_READ}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"
    assert _session_status_cache[session_id] == "idle"

    _session_status_cache[session_id] = "running"
    again = await client.get(f"/v1/sessions/{session_id}?{_RUNNER_READ}")
    assert again.json()["status"] == "running"


async def test_runner_read_leaves_a_recently_active_claude_turn_running(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A turn whose transcript moved within the quiet window stays ``running``."""
    session_id = _seed_claude_turn(db_uri)

    resp = await client.get(f"/v1/sessions/{session_id}?{_RUNNER_READ}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


@pytest.mark.parametrize(
    "held_by", ["codex_session", "sub_agent", "queued_message", "child_at_work"]
)
async def test_quiet_turn_settle_leaves_held_sessions_running(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    held_by: str,
) -> None:
    """Each guard keeps a quiet-looking session running.

    :param held_by: What still holds the session: another harness's session, a
        sub-agent (its parent's runtime owns its status), a queued web message,
        or a sub-agent still at work.
    """
    monkeypatch.setattr(orchestration, "_QUIET_NATIVE_TURN_SETTLE_S", 0)
    store = SqlAlchemyConversationStore(db_uri)
    wrapper = (
        CODEX_NATIVE_CODING_AGENT.wrapper_label
        if held_by == "codex_session"
        else CLAUDE_NATIVE_CODING_AGENT.wrapper_label
    )
    session_id = _seed_claude_turn(db_uri, wrapper=wrapper)
    conv = store.get_conversation(session_id)
    assert conv is not None
    pending_id: str | None = None
    if held_by == "sub_agent":
        child = store.create_conversation(
            kind="sub_agent", parent_conversation_id=session_id, agent_id=conv.agent_id
        )
        store.set_labels(child.id, {WRAPPER_LABEL_KEY: wrapper})
        store.set_session_live_status(child.id, "running")
        _session_status_cache[child.id] = "running"
        conv = store.get_conversation(child.id)
        assert conv is not None
    elif held_by == "queued_message":
        pending_id = pending_inputs.record(session_id, [{"type": "input_text", "text": "next"}])
    elif held_by == "child_at_work":
        child = store.create_conversation(
            kind="sub_agent", parent_conversation_id=session_id, agent_id=conv.agent_id
        )
        store.set_session_live_status(child.id, "running")

    try:
        assert not await orchestration._settle_quiet_native_turn(conv, store)
        assert _session_status_cache[conv.id] == "running"
    finally:
        if pending_id is not None:
            pending_inputs.resolve(session_id, pending_id)
