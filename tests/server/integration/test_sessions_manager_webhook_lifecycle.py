"""
Integration tests for OMN-104's producer hooks, exercised through the real
session/turn lifecycle (not by calling ``session_outbox`` functions
directly — that unit-level coverage lives in ``test_session_outbox.py``).

Drives ``_publish_status`` via the same ``POST /v1/sessions/{id}/events``
``external_session_status`` mechanism the claude-native forwarder uses (see
``test_post_external_session_status_publishes_session_status`` in
``test_sessions_endpoints.py`` for the established pattern), and asserts on
the resulting ``session_lifecycle_outbox`` rows via
``app.state.session_lifecycle_store``.

Covers the design doc's "Completion wake" and "Poll/reconcile stays
enabled" acceptance-test rows (§12).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_elicitation_resolve_url import _create_session

pytestmark = pytest.mark.asyncio


async def _post_status(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    status: str,
    response_id: str | None = None,
    background_task_count: int | None = None,
    output: str | None = None,
) -> httpx.Response:
    data: dict[str, object] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    if output is not None:
        data["output"] = output
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
    )
    assert resp.status_code in (200, 202), resp.text
    return resp


async def test_completion_wake_fires_with_background_shell_still_running(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    A turn reaching ``idle`` from an open ``response_id`` fires exactly one
    ``session.completed`` outbox row — INCLUDING when
    ``background_task_count`` is positive (a background shell/dev-server
    still running). Proves the previously-considered secondary gate on
    ``background_task_count`` was correctly NOT reintroduced (design doc
    §5.1): gating on it again here would silently swallow
    ``session.completed`` for every claude-native session that ends its
    turn with a shell still running, which is the common case.
    """
    agent = await create_test_agent(client, "test-omn104-completion-bg-shell")
    session_id = await _create_session(client, agent["id"])

    await _post_status(client, session_id, status="running", response_id="resp_bg_1")
    await _post_status(
        client,
        session_id,
        status="idle",
        response_id="resp_bg_1",
        background_task_count=3,
    )

    store = app.state.session_lifecycle_store
    deliveries, _ = store.list_deliveries(session_id, limit=100)
    completed = [d for d in deliveries if d.event_type == "session.completed"]
    assert len(completed) == 1, deliveries
    assert '"response_id": "resp_bg_1"' in completed[0].payload


async def test_completion_wake_zero_rows_when_turn_never_opened(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    An ``idle`` transition with no preceding open ``response_id`` emits
    zero ``session.completed`` rows.
    """
    agent = await create_test_agent(client, "test-omn104-completion-never-opened")
    session_id = await _create_session(client, agent["id"])

    await _post_status(client, session_id, status="idle")

    store = app.state.session_lifecycle_store
    deliveries, _ = store.list_deliveries(session_id, limit=100)
    completed = [d for d in deliveries if d.event_type == "session.completed"]
    assert completed == []


async def test_completion_wake_zero_rows_on_same_status_no_op_republish(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    A second ``idle`` republish (already idle, no newly-open turn) is a
    no-op: exactly one ``session.completed`` row exists after the FIRST
    ``running``->``idle`` transition, and the second ``idle``->``idle``
    republish adds no second row (the cached open ``response_id`` was
    already cleared by the first transition).
    """
    agent = await create_test_agent(client, "test-omn104-completion-noop-republish")
    session_id = await _create_session(client, agent["id"])

    await _post_status(client, session_id, status="running", response_id="resp_noop_1")
    await _post_status(client, session_id, status="idle", response_id="resp_noop_1")
    await _post_status(client, session_id, status="idle")

    store = app.state.session_lifecycle_store
    deliveries, _ = store.list_deliveries(session_id, limit=100)
    completed = [d for d in deliveries if d.event_type == "session.completed"]
    assert len(completed) == 1, deliveries


async def test_session_failed_fires_and_is_not_swallowed_by_sticky_guard(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    A ``running`` -> ``failed`` transition fires exactly one
    ``session.failed`` outbox row carrying the failure reason. (The sticky
    failed->idle guard in ``_publish_status`` only swallows a TRAILING
    idle after failed — it must not swallow the failed transition itself.)

    Cross-vendor review: the delivered ``reason.message`` is a fixed, safe
    string selected by ``error_code`` (here ``codex_turn_error``, from this
    route's own classification of a failed ``external_session_status``
    with ``output`` set) — never the raw ``output`` text verbatim. See
    ``session_outbox.record_session_failed``'s docstring.
    """
    agent = await create_test_agent(client, "test-omn104-failed-not-swallowed")
    session_id = await _create_session(client, agent["id"])

    await _post_status(client, session_id, status="running", response_id="resp_fail_1")
    await _post_status(
        client,
        session_id,
        status="failed",
        response_id="resp_fail_1",
        output="the turn errored out",
    )

    store = app.state.session_lifecycle_store
    deliveries, _ = store.list_deliveries(session_id, limit=100)
    failed = [d for d in deliveries if d.event_type == "session.failed"]
    assert len(failed) == 1, deliveries
    assert "the turn errored out" not in failed[0].payload
    assert '"code": "codex_turn_error"' in failed[0].payload
    assert '"message": "The turn failed on the harness side."' in failed[0].payload
    assert '"response_id": "resp_fail_1"' in failed[0].payload


async def test_sticky_failed_guard_still_drops_trailing_idle_and_no_completed_row(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    The PRE-EXISTING sticky failed->idle guard (unrelated to OMN-104, but
    load-bearing for it: §5.1 binds session.completed to this same
    ``_publish_status`` call) still drops a trailing idle after failed —
    and, since that transition never reaches the outbox hook at all
    (early return), no spurious ``session.completed`` row is produced for
    a session that ended in failure.
    """
    agent = await create_test_agent(client, "test-omn104-sticky-guard-no-completed")
    session_id = await _create_session(client, agent["id"])

    await _post_status(client, session_id, status="running", response_id="resp_sticky_1")
    await _post_status(
        client, session_id, status="failed", response_id="resp_sticky_1", output="boom"
    )
    # Trailing quiescence idle (e.g. PTY-activity watcher) — must be
    # swallowed by the sticky guard, not treated as a fresh completion.
    await _post_status(client, session_id, status="idle")

    store = app.state.session_lifecycle_store
    deliveries, _ = store.list_deliveries(session_id, limit=100)
    completed = [d for d in deliveries if d.event_type == "session.completed"]
    failed = [d for d in deliveries if d.event_type == "session.failed"]
    assert completed == [], deliveries
    assert len(failed) == 1, deliveries


async def test_manager_webhook_disabled_leaves_existing_status_read_path_unchanged(
    client: httpx.AsyncClient,
) -> None:
    """
    Poll/reconcile stays correct with ``manager_webhook.enabled=false``
    (the default; no config file exists in this test environment): this
    feature is purely additive — driving the exact same turn-completion
    flow as the other tests here still updates the PRE-EXISTING
    ``_session_status_cache`` / list-endpoint ``status`` field exactly as
    it did before this feature existed. The new outbox write is a NEW
    side effect layered on the same authoritative edge, not a replacement
    of the old one.
    """
    agent = await create_test_agent(client, "test-omn104-poll-reconcile-unaffected")
    session_id = await _create_session(client, agent["id"])

    await _post_status(client, session_id, status="running", response_id="resp_poll_1")
    list_resp = await client.get("/v1/sessions")
    item = next(s for s in list_resp.json()["data"] if s["id"] == session_id)
    assert item["status"] == "running"

    await _post_status(client, session_id, status="idle", response_id="resp_poll_1")
    list_resp = await client.get("/v1/sessions")
    item = next(s for s in list_resp.json()["data"] if s["id"] == session_id)
    assert item["status"] == "idle"
