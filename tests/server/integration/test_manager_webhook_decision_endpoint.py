"""
Integration tests for the OMN-104 harness-classified decision endpoint.

``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` now durably persists
the verdict first (via ``session_outbox.record_decision``), then classifies
resolution by which process died (design doc
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md`` §5.4):

- (a) no restart: a parked server-side ``_harness_elicitation_registry``
  Future resolves synchronously -> ``resolved=True``.
- (b) server restart, runner alive: no Future to resolve and the runner
  isn't reachable from THIS replica, but the runner is believed alive
  (fresh ``runner_last_seen``) -> 202, ``resolved=False,
  pending_redelivery=True``; the verdict is redelivered on the runner's
  actual tunnel reconnect (``omnigent/server/app.py``'s ``_on_runner_connect``).
- (c) runner dead: not resolved and not believed alive -> 410
  ``elicitation_not_resolvable``, verdict still durably recorded.

Classification does not depend on harness type (native/tmux vs
subprocess-backed) — it is purely "did the runner truthfully ack" vs "is the
runner believed alive at all", both harness-agnostic signals. So these three
tests, driven through the shared HTTP resolve endpoint, cover all harness
classes without needing to spin up real claude-native / codex-native /
subprocess runner processes.

(b)'s reconnect-redelivery mechanism itself (``_on_runner_connect`` re-POSTing
decided-but-undelivered rows on an actual WebSocket tunnel reconnect) is not
separately re-tested end-to-end here — that would require driving a real
``runner_tunnel`` WebSocket connect, which this repo's test infra does not
provide a lightweight fixture for. Instead, test (b) proves the QUERY side
of that mechanism directly: ``session_lifecycle_store.list_decided_undelivered``
returns exactly the row ``_on_runner_connect`` would redeliver. The redelivery
POST itself reuses the exact same ``type="approval"`` event contract already
covered by test (a) and by ``test_sessions_elicitation_resolve_url.py``.

Truthful runner-consumption (cross-vendor review BLOCKING #2) is a two-signal
OR, not a single AND-gated header: the runner's approval endpoint
unconditionally forwards the same event to BOTH ``pending_approvals`` (the
policy/cost-ASK path — sets ``X-Omnigent-Pending-Approval-Resolved``) AND the
harness subprocess (the ``ctx.elicit()`` path — a bare 2xx only when the
harness genuinely matched an ``_in_flight`` Future; 404 otherwise). Either
signal alone is truthful; an id unmatched by both means a 404 from the
harness forward with no header — never a bare 2xx with no header, which can
only represent a genuine harness-native consumption. See
``test_decision_endpoint_not_truthfully_consumed_does_not_fire_resumed``,
``test_decision_endpoint_harness_native_2xx_without_header_still_delivers``,
and ``test_decision_endpoint_policy_ask_header_true_with_harness_404_still_delivers``.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.server import session_outbox
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_elicitation_resolve_url import (
    _create_session,
    _park_permission_hook,
)

pytestmark = pytest.mark.asyncio


async def test_decision_endpoint_no_restart_resolves_immediately(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    (a) No restart: a parked server-side harness Future resolves
    synchronously. The endpoint reports ``resolved=True,
    pending_redelivery=False``, marks the ledger row
    ``delivered_to_runner``, and emits a ``session.resumed`` outbox row.
    """
    from omnigent.runtime import pending_elicitations

    agent = await create_test_agent(client, "test-omn104-no-restart")
    session_id = await _create_session(client, agent["id"])
    hook_task, elicitation_id = await _park_permission_hook(client, session_id)

    try:
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text
        body = verdict.json()
        assert body["resolved"] is True
        assert body["pending_redelivery"] is False

        async with asyncio.timeout(5):
            resp = await hook_task
        assert resp.status_code == 200, resp.text
        assert resp.json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"

        store = app.state.session_lifecycle_store
        elicitation = store.get_elicitation(elicitation_id)
        assert elicitation is not None
        assert elicitation.status == "delivered_to_runner"
        assert elicitation.resolved_at is not None

        deliveries, _ = store.list_deliveries(session_id, limit=100)
        resumed = [d for d in deliveries if d.event_type == "session.resumed"]
        assert len(resumed) == 1, deliveries
    finally:
        if not hook_task.done():
            hook_task.cancel()
            await asyncio.gather(hook_task, return_exceptions=True)
        pending_elicitations.reset_for_tests()


async def test_decision_endpoint_server_restart_runner_alive_pending_redelivery(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    (b) Server restart, runner alive: no ``_harness_elicitation_registry``
    entry exists for this id (simulating a wiped in-memory registry after a
    restart), the runner isn't reachable from this replica (no real tunnel
    in this test process), but the runner is believed alive (fresh
    ``runner_last_seen``). The endpoint returns 202 with ``resolved=False,
    pending_redelivery=True``; the ledger row stays ``decided`` (not
    ``delivered_to_runner``); no ``session.resumed`` row is written yet; and
    ``list_decided_undelivered`` surfaces the row for reconnect redelivery.
    """
    agent = await create_test_agent(client, "test-omn104-restart-runner-alive")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_restart_alive_case_b"

    # Seed the durable ledger row directly (simulates the elicitation having
    # been raised before the restart wiped the in-memory registry) — no real
    # harness Future is parked for this id.
    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    assert conv_store.set_runner_id(session_id, "runner_case_b")
    conv_store.touch_runner_liveness(["runner_case_b"], int(time.time()))

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    body = verdict.json()
    assert body["resolved"] is False
    assert body["pending_redelivery"] is True

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(elicitation_id)
    assert elicitation is not None
    assert elicitation.status == "decided"
    assert elicitation.decision_payload is not None

    deliveries, _ = store.list_deliveries(session_id, limit=100)
    resumed = [d for d in deliveries if d.event_type == "session.resumed"]
    assert resumed == []

    undelivered = store.list_decided_undelivered(session_id)
    assert [e.id for e in undelivered] == [elicitation_id]


async def test_decision_endpoint_runner_dead_returns_410_but_records_verdict(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    (c) Runner dead: no harness Future, and the runner is not believed
    alive (no runner_id bound at all — the same shape a stale
    ``runner_last_seen`` would produce). The endpoint returns 410
    ``elicitation_not_resolvable``. The verdict is still durably recorded —
    asserted via a DIRECT store read, not the HTTP response, per the
    acceptance criterion. No ``session.resumed`` row is written.
    """
    agent = await create_test_agent(client, "test-omn104-runner-dead")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_runner_dead_case_c"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )
    # Deliberately do NOT bind a runner_id — the session has no runner
    # believed alive at all (the same outcome a stale runner_last_seen
    # would produce, exercised more explicitly here since it needs no
    # extra store write).

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 410, verdict.text
    detail = verdict.json()["detail"]
    assert detail["error_code"] == "elicitation_not_resolvable"

    # Verdict durably recorded — read directly via the store, NOT the HTTP
    # response, per the acceptance test's own wording.
    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(elicitation_id)
    assert elicitation is not None
    assert elicitation.status == "decided"
    assert elicitation.decision_payload is not None

    deliveries, _ = store.list_deliveries(session_id, limit=100)
    resumed = [d for d in deliveries if d.event_type == "session.resumed"]
    assert resumed == []


async def test_decision_endpoint_runner_dead_stale_liveness_also_410s(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    (c) variant: a ``runner_id`` IS bound, but its ``runner_last_seen``
    stamp is older than the 90s liveness TTL — also classified as dead
    (410), not "pending redelivery". Proves the classification reads
    freshness, not mere presence of a runner_id.
    """
    agent = await create_test_agent(client, "test-omn104-runner-stale")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_runner_stale_case_c"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    assert conv_store.set_runner_id(session_id, "runner_case_c_stale")
    # Stamp far in the past — outside RUNNER_LIVENESS_TTL_S (90s).
    conv_store.touch_runner_liveness(["runner_case_c_stale"], int(time.time()) - 1000)

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 410, verdict.text
    assert verdict.json()["detail"]["error_code"] == "elicitation_not_resolvable"


async def test_decision_endpoint_untracked_elicitation_is_unaffected(
    client: httpx.AsyncClient,
) -> None:
    """
    Regression guard: an elicitation_id with no durable ledger entry (never
    registered via ``record_elicitation_raised`` — the pre-OMN-104 shape,
    e.g. an already-resolved or wholly unknown id) is NOT reclassified into
    a 410. The endpoint's original idempotent-no-op contract is preserved
    exactly (``{"queued": False}``), matching
    ``test_post_resolve_nonexistent_elicitation_on_valid_session`` in
    ``test_sessions_elicitation_api.py``.
    """
    agent = await create_test_agent(client, "test-omn104-untracked-eid")
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/elicitations/elicit_never_registered/resolve",
        json={"action": "accept"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}


async def test_decision_endpoint_mid_disconnect_grace_gets_pending_not_410(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Cross-vendor review BLOCKING #1: a decision arriving while the runner is
    inside its post-a632550e disconnect grace (``RUNNER_DISCONNECT_GRACE_S``)
    must be classified as "pending redelivery" (202), not "runner dead"
    (410) — the runner may reconnect and redeliver momentarily.
    ``session_live_state.clear_runner_liveness`` nulls ``runner_last_seen``
    IMMEDIATELY on disconnect (before the grace elapses), so without
    consulting the grace-pending marker this would incorrectly 410.
    """
    agent = await create_test_agent(client, "test-omn104-mid-grace")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_mid_grace_case_b"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner_mid_grace"
    assert conv_store.set_runner_id(session_id, runner_id)
    # Simulate the exact state a real disconnect leaves behind: liveness
    # already cleared (stale/None), same as session_live_state.clear_runner_liveness.
    conv_store.touch_runner_liveness([runner_id], int(time.time()) - 10_000)

    # Mark the runner grace-pending, mirroring what _on_runner_disconnect
    # stamps into app.state.runner_disconnect_grace_deadline.
    app.state.runner_disconnect_grace_deadline = {runner_id: time.time() + 10.0}

    try:
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text
        body = verdict.json()
        assert body["resolved"] is False
        assert body["pending_redelivery"] is True

        store = app.state.session_lifecycle_store
        elicitation = store.get_elicitation(elicitation_id)
        assert elicitation is not None
        assert elicitation.status == "decided"

        deliveries, _ = store.list_deliveries(session_id, limit=100)
        assert [d for d in deliveries if d.event_type == "session.resumed"] == []
    finally:
        app.state.runner_disconnect_grace_deadline = {}


async def test_decision_endpoint_after_grace_expires_410s_normally(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Counterpart to the mid-grace test: once the grace deadline is in the
    PAST (expired), the endpoint falls through to the normal freshness
    check and correctly 410s — proving the grace-pending check doesn't
    permanently mask a genuinely dead runner.
    """
    agent = await create_test_agent(client, "test-omn104-grace-expired")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_grace_expired_case_c"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner_grace_expired"
    assert conv_store.set_runner_id(session_id, runner_id)
    conv_store.touch_runner_liveness([runner_id], int(time.time()) - 10_000)

    # Deadline in the past: the grace has expired, no reconnect happened.
    app.state.runner_disconnect_grace_deadline = {runner_id: time.time() - 1.0}

    try:
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 410, verdict.text
        assert verdict.json()["detail"]["error_code"] == "elicitation_not_resolvable"
    finally:
        app.state.runner_disconnect_grace_deadline = {}


def _fake_runner_client_factory(status_code: int, *, header: str | None):
    """Build a ``_get_runner_client`` stub returning a fixed status/header pair."""

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        del session_id, runner_router

        fake_runner = FastAPI()

        @fake_runner.post("/v1/sessions/{conversation_id}/events")
        async def _events(conversation_id: str) -> JSONResponse:
            del conversation_id
            headers = {"X-Omnigent-Pending-Approval-Resolved": header} if header else {}
            return JSONResponse(status_code=status_code, content={"ok": True}, headers=headers)

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake_runner), base_url="http://fake-runner"
        )

    return _fake_get_runner_client


async def test_decision_endpoint_not_truthfully_consumed_does_not_fire_resumed(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cross-vendor review BLOCKING #2: a runner that reports NEITHER truthful-
    consumption signal must NOT be treated as "delivered". In the real
    system this is the shape a genuinely unknown/stale/already-resolved
    ``elicitation_id`` produces: ``pending_approvals.resolve()`` returns
    ``False`` (no header), AND the runner's unconditional forward to the
    harness subprocess also 404s (``_scaffold.py``'s ``_resolve_elicitation``
    raises ``ErrorCode.NOT_FOUND`` for any id with no matching ``_in_flight``
    Future) — never a bare 2xx with no header, which (per the follow-up
    investigation for BLOCKING #3) can only happen for a GENUINE
    harness-native ``ctx.elicit()`` consumption, not an unmatched id; see
    ``test_decision_endpoint_harness_native_2xx_without_header_still_delivers``
    directly below for that corrected case. No ``session.resumed`` fires,
    and the row stays queued for redelivery (``pending_redelivery=True``)
    rather than being marked ``delivered_to_runner``.
    """
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._get_runner_client",
        _fake_runner_client_factory(404, header=None),
    )

    agent = await create_test_agent(client, "test-omn104-not-consumed")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_not_consumed_case"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner_not_consumed"
    assert conv_store.set_runner_id(session_id, runner_id)
    conv_store.touch_runner_liveness([runner_id], int(time.time()))

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    body = verdict.json()
    # NOT "delivered" -- neither signal fired.
    assert body["resolved"] is False
    assert body["pending_redelivery"] is True

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(elicitation_id)
    assert elicitation is not None
    assert elicitation.status == "decided"  # NOT delivered_to_runner

    deliveries, _ = store.list_deliveries(session_id, limit=100)
    assert [d for d in deliveries if d.event_type == "session.resumed"] == []


async def test_decision_endpoint_harness_native_2xx_without_header_still_delivers(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Follow-up fix found while building BLOCKING #3's E2E test: the runner's
    approval endpoint unconditionally forwards the SAME event to the harness
    subprocess (in addition to checking ``pending_approvals``), so a
    genuinely-matched harness-native ``ctx.elicit()`` elicitation resolves
    via the harness's own 204 with NO ``X-Omnigent-Pending-Approval-Resolved``
    header (``pending_approvals`` never tracked that id in the first place).
    Requiring the header AND a 2xx (the original BLOCKING #2 fix) made this
    real, legitimate consumption path unreachable for ``session.resumed`` in
    production. The corrected contract treats either signal as sufficient.
    """
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._get_runner_client",
        _fake_runner_client_factory(204, header=None),
    )

    agent = await create_test_agent(client, "test-omn104-harness-native-consumed")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_harness_native_case"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner_harness_native"
    assert conv_store.set_runner_id(session_id, runner_id)
    conv_store.touch_runner_liveness([runner_id], int(time.time()))

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    body = verdict.json()
    assert body["resolved"] is True
    assert body["pending_redelivery"] is False

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(elicitation_id)
    assert elicitation is not None
    assert elicitation.status == "delivered_to_runner"

    deliveries, _ = store.list_deliveries(session_id, limit=100)
    assert len([d for d in deliveries if d.event_type == "session.resumed"]) == 1


async def test_decision_endpoint_policy_ask_header_true_with_harness_404_still_delivers(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Companion to the harness-native case above: the OTHER real production
    shape — a policy/cost ASK tracked only in ``pending_approvals`` — sets
    the truthful header ``true`` while the SAME event's unconditional
    harness forward 404s (no ``ctx.elicit()`` Future exists for that id).
    The header alone must be sufficient; the harness-forward 404 must not
    veto it.
    """
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._get_runner_client",
        _fake_runner_client_factory(404, header="true"),
    )

    agent = await create_test_agent(client, "test-omn104-policy-ask-consumed")
    session_id = await _create_session(client, agent["id"])
    elicitation_id = "elicit_policy_ask_case"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    conv_store = SqlAlchemyConversationStore(db_uri)
    runner_id = "runner_policy_ask"
    assert conv_store.set_runner_id(session_id, runner_id)
    conv_store.touch_runner_liveness([runner_id], int(time.time()))

    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202, verdict.text
    body = verdict.json()
    assert body["resolved"] is True
    assert body["pending_redelivery"] is False

    store = app.state.session_lifecycle_store
    elicitation = store.get_elicitation(elicitation_id)
    assert elicitation is not None
    assert elicitation.status == "delivered_to_runner"
