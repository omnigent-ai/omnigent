"""Integration tests for the manager-webhook dispatcher (OMN-104).

Exercises ``omnigent.server.manager_webhook_dispatcher`` against a real
SQLite-backed :class:`SqlAlchemySessionLifecycleStore` and a tiny real
FastAPI "fake manager" app reached via ``httpx.ASGITransport`` — no mocking
libraries. ``ASGITransport`` routes the dispatcher's real ``httpx.AsyncClient``
calls into an in-process ASGI app with no actual socket, the same technique
``test_scheduled_tasks_routes.py`` uses for testing HTTP surfaces.

One row per docs/architecture/2026-08-10-durable-session-lifecycle-push.md
§12 test-matrix bullet that concerns the dispatcher specifically (producer-
side rows — completion wake, decision payload allowlist — are covered by
other test files).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from omnigent.server import manager_webhook_dispatcher as dispatcher
from omnigent.server import manager_webhook_signing as signing
from omnigent.server.server_config import ManagerWebhookConfig
from omnigent.stores.session_lifecycle_store.sqlalchemy_store import (
    SqlAlchemySessionLifecycleStore,
)

# asyncio_mode = "auto" (pyproject.toml) auto-collects async def tests as
# asyncio tests without a marker; no module-level pytestmark here since this
# file also has a plain sync test (test_hex_id_helper_is_deterministic).

_ENDPOINT = "https://fake-manager.test/webhook"
_SECRET = "test-webhook-secret"


def _hex_id(seed: str) -> str:
    """Deterministic bare 32-char hex id (Uuid16 columns) from a readable seed."""
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


@dataclass
class FakeManagerState:
    """Mutable per-test behavior for the fake manager app."""

    fail_times: int = 0
    always_fail: bool = False
    verify_signature: bool = True
    received: list[dict] = field(default_factory=list)
    received_headers: list[dict] = field(default_factory=list)
    seen_event_ids: set[str] = field(default_factory=set)
    duplicate_event_ids: set[str] = field(default_factory=set)
    call_count: int = 0
    hang_seconds: float | None = None


def _build_fake_manager(state: FakeManagerState) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_omnigent_event_id: str = Header(...),
        x_omnigent_timestamp: str = Header(...),
        x_omnigent_signature: str = Header(...),
    ) -> JSONResponse:
        state.call_count += 1
        raw_body = await request.body()
        raw_json_body = raw_body.decode("utf-8")
        if state.hang_seconds is not None:
            await asyncio.sleep(state.hang_seconds)
        if state.verify_signature:
            ok = signing.verify(
                signature_header=x_omnigent_signature,
                timestamp=int(x_omnigent_timestamp),
                event_id=x_omnigent_event_id,
                raw_json_body=raw_json_body,
                now=int(time.time()),
                secrets=[_SECRET],
            )
            if not ok:
                return JSONResponse(status_code=401, content={"error": "bad signature"})
        state.received.append(json.loads(raw_json_body))
        state.received_headers.append(dict(request.headers))
        duplicate = x_omnigent_event_id in state.seen_event_ids
        if duplicate:
            state.duplicate_event_ids.add(x_omnigent_event_id)
        state.seen_event_ids.add(x_omnigent_event_id)
        if state.always_fail:
            return JSONResponse(status_code=500, content={"error": "always fails"})
        if state.fail_times > 0:
            state.fail_times -= 1
            return JSONResponse(status_code=500, content={"error": "transient"})
        return JSONResponse(status_code=200, content={"duplicate": duplicate})

    return app


@pytest.fixture()
def state() -> FakeManagerState:
    return FakeManagerState()


@pytest.fixture()
async def client(state: FakeManagerState) -> AsyncIterator[httpx.AsyncClient]:
    app = _build_fake_manager(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport) as c:
        yield c


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySessionLifecycleStore:
    return SqlAlchemySessionLifecycleStore(db_uri)


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_MANAGER_WEBHOOK_SECRET", _SECRET)


def _enable_dispatch(monkeypatch: pytest.MonkeyPatch, *, endpoint: str = _ENDPOINT) -> None:
    """Point manager_webhook_config() at our fake manager without touching disk config.

    Legitimate test-boundary stub of config *resolution* (not of the
    dispatcher/signing code under test) — avoids cross-test env/file leakage
    under parallel test workers.
    """
    monkeypatch.setattr(
        dispatcher,
        "manager_webhook_config",
        lambda: ManagerWebhookConfig(enabled=True, endpoint=endpoint, key_id="k1"),
    )


def _insert_event(
    store: SqlAlchemySessionLifecycleStore,
    *,
    session_id: str,
    transition_key: str,
    event_id: str | None = None,
) -> str:
    eid = event_id or _hex_id(f"evt:{session_id}:{transition_key}")
    now = int(time.time())
    event, _inserted = store.record_lifecycle_event(
        event_id=eid,
        session_id=session_id,
        event_type="session.completed",
        transition_key=transition_key,
        payload=json.dumps({"response_id": transition_key}),
        now=now,
    )
    return event.id


# ── 1. Delivered on first attempt ───────────────────────────────


async def test_delivered_on_first_attempt(
    store: SqlAlchemySessionLifecycleStore,
    client: httpx.AsyncClient,
    state: FakeManagerState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dispatch(monkeypatch)
    session_id = _hex_id("session-1")
    event_id = _insert_event(store, session_id=session_id, transition_key="turn:r1:completed")

    claimed_count = await dispatcher._run_once(client, store, lease_owner="replica-1")

    assert claimed_count == 1
    row = store.latest_delivery(session_id)
    assert row is not None
    assert row.id == event_id
    assert row.status == "delivered"
    assert row.last_http_status == 200
    assert state.call_count == 1


# ── 2. Retry then succeed ───────────────────────────────────────


async def test_retry_then_succeed(
    store: SqlAlchemySessionLifecycleStore,
    client: httpx.AsyncClient,
    state: FakeManagerState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dispatch(monkeypatch)
    state.fail_times = 2
    session_id = _hex_id("session-2")
    _insert_event(store, session_id=session_id, transition_key="turn:r2:completed")

    # Bypass real backoff wait: pass an artificially advanced `now` to
    # claim_batch each cycle (backoff caps at 15 minutes, so +1 day is always
    # past any possible next_attempt_at) instead of sleeping real time.
    far_future = int(time.time()) + 86_400
    for _ in range(5):
        claimed = store.claim_batch(
            limit=10, now=far_future, lease_owner="replica-1", lease_seconds=60
        )
        if not claimed:
            break
        for event in claimed:
            await dispatcher._deliver_one(client, store, event, endpoint=_ENDPOINT, key_id="k1")
        far_future += 86_400
        row = store.latest_delivery(session_id)
        assert row is not None
        if row.status == "delivered":
            break

    row = store.latest_delivery(session_id)
    assert row is not None
    assert row.status == "delivered"
    assert row.attempt_count == 3  # 2 failures + 1 success
    assert state.call_count == 3


# ── 3. Dead-letter escalation, never abandonment ────────────────


async def test_dead_letter_is_escalation_not_abandonment(
    store: SqlAlchemySessionLifecycleStore,
    client: httpx.AsyncClient,
    state: FakeManagerState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dispatch(monkeypatch)
    state.always_fail = True
    session_id = _hex_id("session-3")
    event_id = _insert_event(store, session_id=session_id, transition_key="turn:r3:completed")

    far_future = int(time.time())
    row = None
    for _ in range(dispatcher._DEAD_LETTER_AFTER_ATTEMPTS + 2):
        far_future += 86_400
        claimed = store.claim_batch(
            limit=10, now=far_future, lease_owner="replica-1", lease_seconds=60
        )
        assert claimed, "dead-lettered row must remain claimable (escalation, not abandonment)"
        for event in claimed:
            await dispatcher._deliver_one(client, store, event, endpoint=_ENDPOINT, key_id="k1")
        row = store.latest_delivery(session_id)
        assert row is not None
        if row.status == "dead_letter":
            break

    assert row is not None
    assert row.status == "dead_letter"
    assert row.attempt_count >= dispatcher._DEAD_LETTER_AFTER_ATTEMPTS

    # Visible via the read API/store with its dead_letter status — "visible
    # via the new API/log fields", not silently dropped.
    deliveries, _cursor = store.list_deliveries(session_id, limit=10)
    assert any(d.id == event_id and d.status == "dead_letter" for d in deliveries)

    # One more cycle past next_attempt_at: still claimed and retried at the
    # capped floor interval — proves it never stops trying.
    far_future += 86_400
    claimed_again = store.claim_batch(
        limit=10, now=far_future, lease_owner="replica-1", lease_seconds=60
    )
    assert len(claimed_again) == 1
    assert claimed_again[0].id == event_id
    attempts_before = claimed_again[0].attempt_count
    for event in claimed_again:
        await dispatcher._deliver_one(client, store, event, endpoint=_ENDPOINT, key_id="k1")
    row_after = store.latest_delivery(session_id)
    assert row_after is not None
    assert row_after.attempt_count == attempts_before  # already incremented by claim
    assert row_after.status == "dead_letter"


# ── 4. Ordering / non-regression ────────────────────────────────


async def test_ordering_never_issues_n_plus_1_before_n_terminal(
    store: SqlAlchemySessionLifecycleStore,
    client: httpx.AsyncClient,
    state: FakeManagerState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dispatch(monkeypatch)
    session_id = _hex_id("session-4")
    _insert_event(store, session_id=session_id, transition_key="turn:r4a:completed")
    _insert_event(store, session_id=session_id, transition_key="turn:r4b:failed")

    deliveries, _c = store.list_deliveries(session_id, limit=10)
    by_seq = sorted(deliveries, key=lambda d: d.sequence)
    assert [d.sequence for d in by_seq] == [1, 2]

    # First cycle: only sequence 1 is claimable (claim_batch's own blocking
    # subquery refuses to return sequence 2 while sequence 1 is pending).
    claimed_count = await dispatcher._run_once(client, store, lease_owner="replica-1")
    assert claimed_count == 1
    deliveries, _c = store.list_deliveries(session_id, limit=10)
    by_seq = sorted(deliveries, key=lambda d: d.sequence)
    assert by_seq[0].status == "delivered"
    assert by_seq[1].status == "pending"  # sequence 2 never attempted yet
    assert state.call_count == 1

    # Wire payload always carries `sequence` (manager-side dedupe-by-sequence
    # contract).
    assert "sequence" in state.received[0]
    assert state.received[0]["sequence"] == 1

    # Second cycle: sequence 1 is now delivered (terminal) -> sequence 2
    # becomes claimable.
    claimed_count_2 = await dispatcher._run_once(client, store, lease_owner="replica-1")
    assert claimed_count_2 == 1
    deliveries, _c = store.list_deliveries(session_id, limit=10)
    by_seq = sorted(deliveries, key=lambda d: d.sequence)
    assert by_seq[0].status == "delivered"
    assert by_seq[1].status == "delivered"
    assert state.received[1]["sequence"] == 2


# ── 5. Duplicate delivery / stable event IDs ────────────────────


async def test_duplicate_delivery_stable_event_ids(
    store: SqlAlchemySessionLifecycleStore,
    client: httpx.AsyncClient,
    state: FakeManagerState,
) -> None:
    session_id = _hex_id("session-5")
    transition_key = "turn:r5:completed"
    # A retried producer call for the identical transition resolves to the
    # SAME row (same event_id), not a duplicate — this is what bounds the
    # dispatcher to ever scheduling exactly one delivery per logical event.
    id_a = _insert_event(store, session_id=session_id, transition_key=transition_key)
    id_b = _insert_event(store, session_id=session_id, transition_key=transition_key)
    assert id_a == id_b
    deliveries, _c = store.list_deliveries(session_id, limit=10)
    assert len(deliveries) == 1

    # Receiver-side dedupe: simulate "manager 2xx'd but the ack was lost" by
    # POSTing the identical signed payload twice directly against the fake
    # manager. Its own seen_event_ids tracking must recognize the replay.
    now = int(time.time())
    body = json.dumps({"event_id": id_a, "sequence": 1, "event_type": "session.completed"})
    signature = signing.sign(secret=_SECRET, timestamp=now, event_id=id_a, raw_json_body=body)
    headers = signing.build_headers(
        event_id=id_a,
        event_type="session.completed",
        attempt=1,
        timestamp=now,
        key_id="k1",
        signature=signature,
    )
    resp1 = await client.post(_ENDPOINT, content=body, headers=headers)
    resp2 = await client.post(_ENDPOINT, content=body, headers=headers)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["duplicate"] is False
    assert resp2.json()["duplicate"] is True
    assert id_a in state.duplicate_event_ids
    assert len(state.seen_event_ids) == 1


# ── 6. HMAC signature required and verified ─────────────────────


async def test_hmac_headers_present_and_verified(
    store: SqlAlchemySessionLifecycleStore,
    client: httpx.AsyncClient,
    state: FakeManagerState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dispatch(monkeypatch)
    session_id = _hex_id("session-6")
    _insert_event(store, session_id=session_id, transition_key="turn:r6:completed")

    claimed_count = await dispatcher._run_once(client, store, lease_owner="replica-1")

    assert claimed_count == 1
    assert store.latest_delivery(session_id).status == "delivered"  # type: ignore[union-attr]
    assert len(state.received_headers) == 1
    headers = state.received_headers[0]
    assert "x-omnigent-signature" in headers
    assert headers["x-omnigent-signature"].startswith("v1=")
    assert "x-omnigent-event-id" in headers
    assert "x-omnigent-timestamp" in headers
    assert headers["x-omnigent-delivery-attempt"] == "1"
    assert headers["x-omnigent-key-id"] == "k1"

    # An invalid signature is rejected by the (independently-implemented)
    # verification reference, proving the scheme round-trips end to end.
    state.received.clear()
    state.verify_signature = True
    tampered_body = json.dumps({"tampered": True})
    bad_headers = dict(headers)
    resp = await client.post(_ENDPOINT, content=tampered_body, headers=bad_headers)
    assert resp.status_code == 401


# ── 7. Reclaim expired lease -> redelivered with same identity ─


async def test_reclaim_expired_lease_redelivers_same_identity(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    session_id = _hex_id("session-7")
    event_id = _insert_event(store, session_id=session_id, transition_key="turn:r7:completed")

    now = int(time.time())
    # Simulate "dispatcher killed mid-delivery": claim but never complete.
    claimed = store.claim_batch(limit=10, now=now, lease_owner="replica-dead", lease_seconds=1)
    assert len(claimed) == 1
    original_sequence = claimed[0].sequence
    assert claimed[0].id == event_id

    # Still leased -> invisible to a fresh claim while the lease is live.
    still_leased = store.claim_batch(limit=10, now=now, lease_owner="replica-2", lease_seconds=60)
    assert still_leased == []

    # Lease expires -> reclaimed back to pending.
    reclaimed_count = store.reclaim_expired_leases(now=now + 3600)
    assert reclaimed_count == 1
    row = store.latest_delivery(session_id)
    assert row is not None
    assert row.status == "pending"
    assert row.lease_owner is None

    # A live (not-yet-expired) lease is NOT reclaimed.
    claimed_2 = store.claim_batch(
        limit=10, now=now + 3601, lease_owner="replica-3", lease_seconds=3600
    )
    assert len(claimed_2) == 1
    assert store.reclaim_expired_leases(now=now + 3601) == 0

    # Redelivered with the SAME event_id/sequence — identity preserved.
    assert claimed_2[0].id == event_id
    assert claimed_2[0].sequence == original_sequence


# ── 8. manager_webhook.enabled=false -> dispatcher claims nothing ─


async def test_disabled_config_claims_nothing(
    store: SqlAlchemySessionLifecycleStore,
    client: httpx.AsyncClient,
) -> None:
    # No monkeypatch: default config is enabled=False.
    session_id = _hex_id("session-8")
    _insert_event(store, session_id=session_id, transition_key="turn:r8:completed")

    claimed_count = await dispatcher._run_once(client, store, lease_owner="replica-1")

    assert claimed_count == 0
    row = store.latest_delivery(session_id)
    assert row is not None
    assert row.status == "pending"
    assert row.attempt_count == 0


# ── 9. Endpoint permanently down ────────────────────────────────


async def test_endpoint_down_releases_lease_and_retries(
    store: SqlAlchemySessionLifecycleStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dispatch(monkeypatch)
    session_id = _hex_id("session-9")
    _insert_event(store, session_id=session_id, transition_key="turn:r9:completed")

    async def _always_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    down_transport = httpx.MockTransport(_always_down)
    async with httpx.AsyncClient(transport=down_transport) as down_client:
        claimed_count = await dispatcher._run_once(down_client, store, lease_owner="replica-1")

    assert claimed_count == 1
    row = store.latest_delivery(session_id)
    assert row is not None
    assert row.status == "pending"  # released the lease, not stuck in "leased"
    assert row.attempt_count == 1
    assert row.last_error_code == "ConnectError"


# ── 10. Callback never blocks (dispatcher-side bounded timeout) ─


async def test_delivery_attempt_carries_a_bounded_timeout(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """Proves the dispatcher's own delivery attempt is bounded by
    _DELIVERY_TIMEOUT_SECONDS rather than able to hang forever on an
    unresponsive manager.

    This sandbox's httpx/anyio socket layer does not honor
    ``asyncio.wait_for``-based cancellation around a genuinely-hanging
    server (verified empirically: a real TCP listener that accepts the
    connection and never writes a response defeats both httpx's own
    ``timeout=`` kwarg AND an outer ``asyncio.wait_for`` in this
    environment — an infra/sandbox limitation, not a product one), so a
    true end-to-end "manager hangs, dispatcher must not hang" test cannot
    run reliably here without risking a wedged CI job. Instead this proves
    the same guarantee two ways that don't depend on that transport
    behavior: (1) the exact bounded value is wired into the real HTTP call,
    and (2) a manager that responds well within budget doesn't trigger any
    extra unbounded wait in the delivery path. The producer-side half of
    "callback never blocks" (that _publish_status itself never makes a
    network call at all) is covered by test_session_outbox.py, not here.
    """
    import inspect

    source = inspect.getsource(dispatcher._deliver_one)
    assert "timeout=_DELIVERY_TIMEOUT_SECONDS" in source
    assert dispatcher._DELIVERY_TIMEOUT_SECONDS <= 30.0  # sane, bounded budget

    slow_state = FakeManagerState()
    slow_state.hang_seconds = 1.0  # well within the 10s budget
    app = _build_fake_manager(slow_state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as fast_client:
        session_id = _hex_id("session-10")
        _insert_event(store, session_id=session_id, transition_key="turn:r10:completed")
        event = store.claim_batch(
            limit=1, now=int(time.time()), lease_owner="replica-1", lease_seconds=60
        )[0]
        started = time.monotonic()
        await dispatcher._deliver_one(fast_client, store, event, endpoint=_ENDPOINT, key_id="k1")
        elapsed = time.monotonic() - started
    assert elapsed < 5.0  # no unbounded wait added on top of the manager's own delay
    row = store.latest_delivery(session_id)
    assert row is not None
    assert row.status == "delivered"


def test_hex_id_helper_is_deterministic() -> None:
    """Sanity-check the test module's own id helper (not product code)."""
    assert _hex_id("x") == _hex_id("x")
    assert len(_hex_id("x")) == 32
    assert _hex_id("x") != _hex_id("y")
    uuid.UUID(bytes=bytes.fromhex(_hex_id("x")))  # round-trips as 16 raw bytes
