"""Tests for :class:`SqlAlchemySessionLifecycleStore`.

Exercises the transactional outbox (producer + dispatcher sides) and the
durable elicitation ledger against a real SQLite database. See
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md``.
"""

from __future__ import annotations

import hashlib

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.session_lifecycle_store.sqlalchemy_store import (
    SqlAlchemySessionLifecycleStore,
)

SID_A = "a1" * 16
SID_B = "b2" * 16
EID_1 = "e1" * 16


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySessionLifecycleStore:
    """A fresh :class:`SqlAlchemySessionLifecycleStore` backed by the test SQLite DB."""
    return SqlAlchemySessionLifecycleStore(db_uri)


def _eid(seed: str) -> str:
    """Deterministic bare 32-char hex id from a short readable seed."""
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


# ── record_lifecycle_event: sequence allocation + idempotency ──


def test_record_lifecycle_event_first_insert(store: SqlAlchemySessionLifecycleStore) -> None:
    event, inserted = store.record_lifecycle_event(
        event_id=_eid("c1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:r1:completed",
        payload='{"response_id":"r1"}',
        now=1000,
    )
    assert inserted is True
    assert event.sequence == 1
    assert event.status == "pending"
    assert event.attempt_count == 0


def test_record_lifecycle_event_duplicate_is_idempotent(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    first, inserted_1 = store.record_lifecycle_event(
        event_id=_eid("c1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:r1:completed",
        payload='{"response_id":"r1"}',
        now=1000,
    )
    second, inserted_2 = store.record_lifecycle_event(
        event_id=_eid("c1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:r1:completed",
        payload='{"response_id":"r1"}',
        now=1001,
    )
    assert inserted_1 is True
    assert inserted_2 is False
    assert first.sequence == second.sequence == 1
    deliveries, _ = store.list_deliveries(SID_A, limit=100)
    assert len(deliveries) == 1


def test_second_distinct_event_gets_sequence_two(store: SqlAlchemySessionLifecycleStore) -> None:
    store.record_lifecycle_event(
        event_id=_eid("c1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:r1:completed",
        payload="{}",
        now=1000,
    )
    second, _ = store.record_lifecycle_event(
        event_id=_eid("c2"),
        session_id=SID_A,
        event_type="session.failed",
        transition_key="turn:r2:failed",
        payload="{}",
        now=1001,
    )
    assert second.sequence == 2


def test_sequence_allocated_per_session_not_globally(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    ev_a, _ = store.record_lifecycle_event(
        event_id=_eid("c1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:ra:completed",
        payload="{}",
        now=1000,
    )
    ev_b, _ = store.record_lifecycle_event(
        event_id=_eid("c2"),
        session_id=SID_B,
        event_type="session.completed",
        transition_key="turn:rb:completed",
        payload="{}",
        now=1000,
    )
    assert ev_a.sequence == 1
    assert ev_b.sequence == 1


# ── record_elicitation_raised / get_elicitation ─────────────────


def test_record_elicitation_raised_creates_ledger_and_outbox_row(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    elicitation, outbox_event, inserted = store.record_elicitation_raised(
        elicitation_id=EID_1,
        session_id=SID_A,
        request_payload='{"message":"approve?"}',
        outbox_event_id=_eid("o1"),
        transition_key=f"elicitation:{EID_1}:awaiting_decision",
        outbox_payload='{"elicitation_id":"' + EID_1 + '"}',
        now=1000,
    )
    assert inserted is True
    assert elicitation.status == "pending"
    assert outbox_event is not None
    assert outbox_event.event_type == "session.awaiting_decision"

    fetched = store.get_elicitation(EID_1)
    assert fetched is not None
    assert fetched.session_id == SID_A


def test_record_elicitation_raised_duplicate_is_idempotent(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_elicitation_raised(
        elicitation_id=EID_1,
        session_id=SID_A,
        request_payload="{}",
        outbox_event_id=_eid("o1"),
        transition_key=f"elicitation:{EID_1}:awaiting_decision",
        outbox_payload="{}",
        now=1000,
    )
    _elic, outbox_event, inserted = store.record_elicitation_raised(
        elicitation_id=EID_1,
        session_id=SID_A,
        request_payload="{}",
        outbox_event_id=_eid("o1"),
        transition_key=f"elicitation:{EID_1}:awaiting_decision",
        outbox_payload="{}",
        now=1001,
    )
    assert inserted is False
    assert outbox_event is None


def test_get_elicitation_returns_none_for_unknown_id(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    assert store.get_elicitation("elicit_never_registered") is None


# ── record_decision ──────────────────────────────────────────────


def test_record_decision_transitions_pending_to_decided(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_elicitation_raised(
        elicitation_id=EID_1,
        session_id=SID_A,
        request_payload="{}",
        outbox_event_id=_eid("o1"),
        transition_key=f"elicitation:{EID_1}:awaiting_decision",
        outbox_payload="{}",
        now=1000,
    )
    decided = store.record_decision(
        EID_1, decision_payload='{"action":"accept"}', decided_by="mgr@example.com", now=1005
    )
    assert decided is not None
    assert decided.status == "decided"
    assert decided.decision_payload == '{"action":"accept"}'
    assert decided.decided_by == "mgr@example.com"
    assert decided.decided_at == 1005


def test_record_decision_is_idempotent_against_retry(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_elicitation_raised(
        elicitation_id=EID_1,
        session_id=SID_A,
        request_payload="{}",
        outbox_event_id=_eid("o1"),
        transition_key=f"elicitation:{EID_1}:awaiting_decision",
        outbox_payload="{}",
        now=1000,
    )
    store.record_decision(EID_1, decision_payload='{"action":"accept"}', decided_by=None, now=1005)
    replay = store.record_decision(
        EID_1, decision_payload='{"action":"decline"}', decided_by=None, now=1006
    )
    # Manager retry with a (hypothetically) different body must not overwrite
    # the already-recorded verdict.
    assert replay is not None
    assert replay.decision_payload == '{"action":"accept"}'
    assert replay.decided_at == 1005


def test_record_decision_on_nonexistent_elicitation_returns_none(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    assert (
        store.record_decision("elicit_ghost", decision_payload="{}", decided_by=None, now=1)
        is None
    )


# ── record_elicitation_resolved ──────────────────────────────────


def test_record_elicitation_resolved_after_decision(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_elicitation_raised(
        elicitation_id=EID_1,
        session_id=SID_A,
        request_payload="{}",
        outbox_event_id=_eid("o1"),
        transition_key=f"elicitation:{EID_1}:awaiting_decision",
        outbox_payload="{}",
        now=1000,
    )
    store.record_decision(EID_1, decision_payload='{"action":"accept"}', decided_by=None, now=1005)
    elicitation, outbox_event, inserted = store.record_elicitation_resolved(
        EID_1,
        outbox_event_id=_eid("o2"),
        transition_key=f"elicitation:{EID_1}:resumed",
        outbox_payload="{}",
        resolved_at=1010,
    )
    assert inserted is True
    assert elicitation is not None
    assert elicitation.status == "delivered_to_runner"
    assert elicitation.resolved_at == 1010
    assert outbox_event is not None
    assert outbox_event.event_type == "session.resumed"


def test_record_elicitation_resolved_idempotent_on_replay(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_elicitation_raised(
        elicitation_id=EID_1,
        session_id=SID_A,
        request_payload="{}",
        outbox_event_id=_eid("o1"),
        transition_key=f"elicitation:{EID_1}:awaiting_decision",
        outbox_payload="{}",
        now=1000,
    )
    store.record_decision(EID_1, decision_payload="{}", decided_by=None, now=1005)
    store.record_elicitation_resolved(
        EID_1,
        outbox_event_id=_eid("o2"),
        transition_key=f"elicitation:{EID_1}:resumed",
        outbox_payload="{}",
        resolved_at=1010,
    )
    _elic, _outbox, inserted_again = store.record_elicitation_resolved(
        EID_1,
        outbox_event_id=_eid("o2"),
        transition_key=f"elicitation:{EID_1}:resumed",
        outbox_payload="{}",
        resolved_at=1020,
    )
    assert inserted_again is False


def test_record_elicitation_resolved_on_missing_elicitation_returns_none(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    elicitation, outbox_event, inserted = store.record_elicitation_resolved(
        "elicit_ghost",
        outbox_event_id=_eid("o9"),
        transition_key="elicitation:elicit_ghost:resumed",
        outbox_payload="{}",
        resolved_at=1,
    )
    assert (elicitation, outbox_event, inserted) == (None, None, False)


# ── list_decided_undelivered ─────────────────────────────────────


def test_list_decided_undelivered_excludes_pending_and_delivered(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    pending_id = _eid("p1")
    decided_id = _eid("d1")
    delivered_id = _eid("l1")
    for eid, seed in ((pending_id, "op1"), (decided_id, "od1"), (delivered_id, "ol1")):
        store.record_elicitation_raised(
            elicitation_id=eid,
            session_id=SID_A,
            request_payload="{}",
            outbox_event_id=_eid(seed),
            transition_key=f"elicitation:{eid}:awaiting_decision",
            outbox_payload="{}",
            now=1000,
        )
    store.record_decision(decided_id, decision_payload="{}", decided_by=None, now=1001)
    store.record_decision(delivered_id, decision_payload="{}", decided_by=None, now=1000)
    store.record_elicitation_resolved(
        delivered_id,
        outbox_event_id=_eid("res1"),
        transition_key=f"elicitation:{delivered_id}:resumed",
        outbox_payload="{}",
        resolved_at=1002,
    )

    undelivered = store.list_decided_undelivered(SID_A)
    assert [e.id for e in undelivered] == [decided_id]


# ── claim_batch: per-session ordering (THE core ordering row) ────


def test_claim_batch_only_claims_lowest_non_delivered_sequence(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """Three pending events for ONE session: claim_batch must claim ONLY
    sequence=1 and leave 2/3 unclaimed, because a blocking subquery excludes
    any row with an earlier (lower-sequence) sibling still pending/leased.
    This is what prevents the dispatcher from ever issuing event N+1's HTTP
    call before N reaches a terminal delivery state (delivered/dead_letter).
    """
    for i in range(1, 4):
        store.record_lifecycle_event(
            event_id=_eid(f"e{i}"),
            session_id=SID_A,
            event_type="session.completed",
            transition_key=f"turn:r{i}:completed",
            payload="{}",
            now=1000,
        )

    claimed = store.claim_batch(limit=10, now=2000, lease_owner="r1", lease_seconds=60)
    assert [c.sequence for c in claimed] == [1]

    # sequence=2 stays invisible to another claim attempt too.
    claimed_again = store.claim_batch(limit=10, now=2001, lease_owner="r2", lease_seconds=60)
    assert claimed_again == []

    store.mark_delivered(claimed[0].id, workspace_id=0, delivered_at=2002, http_status=200)

    claimed_2 = store.claim_batch(limit=10, now=2003, lease_owner="r1", lease_seconds=60)
    assert [c.sequence for c in claimed_2] == [2]


def test_claim_batch_respects_future_next_attempt_at(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_lifecycle_event(
        event_id=_eid("f1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rf:completed",
        payload="{}",
        now=5_000_000,  # next_attempt_at defaults to `now`, far in the future
    )
    claimed = store.claim_batch(limit=10, now=1000, lease_owner="r1", lease_seconds=60)
    assert claimed == []


def test_claim_batch_stamps_lease_fields(store: SqlAlchemySessionLifecycleStore) -> None:
    store.record_lifecycle_event(
        event_id=_eid("g1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rg:completed",
        payload="{}",
        now=1000,
    )
    claimed = store.claim_batch(limit=10, now=2000, lease_owner="replica-42", lease_seconds=30)
    assert len(claimed) == 1
    row = claimed[0]
    assert row.status == "leased"
    assert row.attempt_count == 1
    assert row.lease_owner == "replica-42"
    assert row.lease_expires_at == 2030
    assert row.last_attempt_at == 2000


# ── mark_delivered / mark_delivery_failed ─────────────────────────


def test_mark_delivered_transitions_leased_to_delivered(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_lifecycle_event(
        event_id=_eid("h1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rh:completed",
        payload="{}",
        now=1000,
    )
    claimed = store.claim_batch(limit=10, now=2000, lease_owner="r1", lease_seconds=60)[0]
    store.mark_delivered(claimed.id, workspace_id=0, delivered_at=2005, http_status=200)
    row = store.latest_delivery(SID_A)
    assert row is not None
    assert row.status == "delivered"
    assert row.delivered_at == 2005
    assert row.last_http_status == 200
    assert row.lease_owner is None


def test_mark_delivered_noop_when_not_leased(store: SqlAlchemySessionLifecycleStore) -> None:
    store.record_lifecycle_event(
        event_id=_eid("i1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:ri:completed",
        payload="{}",
        now=1000,
    )
    # Never claimed -> still "pending", not "leased".
    store.mark_delivered(_eid("i1"), workspace_id=0, delivered_at=9999, http_status=200)
    row = store.latest_delivery(SID_A)
    assert row is not None
    assert row.status == "pending"
    assert row.delivered_at is None


def test_mark_delivery_failed_dead_letters_past_threshold(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_lifecycle_event(
        event_id=_eid("j1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rj:completed",
        payload="{}",
        now=1000,
    )
    claimed = store.claim_batch(limit=10, now=2000, lease_owner="r1", lease_seconds=60)[0]
    assert claimed.attempt_count == 1
    store.mark_delivery_failed(
        claimed.id,
        workspace_id=0,
        next_attempt_at=2001,
        dead_letter_after_attempts=1,
        http_status=500,
        error_code="http_error",
        error_message="boom",
    )
    row = store.latest_delivery(SID_A)
    assert row is not None
    assert row.status == "dead_letter"
    assert row.last_error_code == "http_error"


def test_mark_delivery_failed_stays_pending_below_threshold(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_lifecycle_event(
        event_id=_eid("k1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rk:completed",
        payload="{}",
        now=1000,
    )
    claimed = store.claim_batch(limit=10, now=2000, lease_owner="r1", lease_seconds=60)[0]
    store.mark_delivery_failed(
        claimed.id, workspace_id=0, next_attempt_at=2001, dead_letter_after_attempts=10
    )
    row = store.latest_delivery(SID_A)
    assert row is not None
    assert row.status == "pending"


def test_dead_letter_row_is_claimed_again_escalation_not_abandonment(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """A dead-lettered row keeps retrying at the capped floor interval — it
    is never automatically abandoned."""
    store.record_lifecycle_event(
        event_id=_eid("m1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rm:completed",
        payload="{}",
        now=1000,
    )
    claimed = store.claim_batch(limit=10, now=2000, lease_owner="r1", lease_seconds=60)[0]
    store.mark_delivery_failed(
        claimed.id, workspace_id=0, next_attempt_at=2001, dead_letter_after_attempts=1
    )
    row = store.latest_delivery(SID_A)
    assert row is not None and row.status == "dead_letter"

    reclaimed_again = store.claim_batch(limit=10, now=2002, lease_owner="r2", lease_seconds=60)
    assert len(reclaimed_again) == 1
    assert reclaimed_again[0].status == "leased"
    assert reclaimed_again[0].id == _eid("m1")


# ── reclaim_expired_leases ────────────────────────────────────────


def test_reclaim_expired_leases_returns_row_to_pending(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_lifecycle_event(
        event_id=_eid("n1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rn:completed",
        payload="{}",
        now=1000,
    )
    store.claim_batch(limit=10, now=2000, lease_owner="r1", lease_seconds=30)
    # Lease not yet expired -> not reclaimed.
    assert store.reclaim_expired_leases(now=2010) == 0
    # Lease expired -> reclaimed.
    reclaimed = store.reclaim_expired_leases(now=2031)
    assert reclaimed == 1
    row = store.latest_delivery(SID_A)
    assert row is not None
    assert row.status == "pending"
    assert row.lease_owner is None
    assert row.lease_expires_at is None


# ── list_deliveries / latest_delivery ─────────────────────────────


def test_list_deliveries_pagination_no_gaps_or_dupes(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    for i in range(1, 6):
        store.record_lifecycle_event(
            event_id=_eid(f"q{i}"),
            session_id=SID_A,
            event_type="session.completed",
            transition_key=f"turn:rq{i}:completed",
            payload="{}",
            now=1000,
        )
    page1, cursor1 = store.list_deliveries(SID_A, limit=2)
    assert [d.sequence for d in page1] == [5, 4]
    assert cursor1 == page1[-1].id

    page2, cursor2 = store.list_deliveries(SID_A, limit=2, after_id=cursor1)
    assert [d.sequence for d in page2] == [3, 2]

    page3, cursor3 = store.list_deliveries(SID_A, limit=2, after_id=cursor2)
    assert [d.sequence for d in page3] == [1]
    assert cursor3 is None


def test_latest_delivery_returns_highest_sequence(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    store.record_lifecycle_event(
        event_id=_eid("s1"),
        session_id=SID_A,
        event_type="session.completed",
        transition_key="turn:rs1:completed",
        payload="{}",
        now=1000,
    )
    store.record_lifecycle_event(
        event_id=_eid("s2"),
        session_id=SID_A,
        event_type="session.failed",
        transition_key="turn:rs2:failed",
        payload="{}",
        now=1001,
    )
    latest = store.latest_delivery(SID_A)
    assert latest is not None
    assert latest.sequence == 2
    assert latest.event_type == "session.failed"


def test_latest_delivery_none_for_empty_session(store: SqlAlchemySessionLifecycleStore) -> None:
    assert store.latest_delivery(SID_B) is None


# ── multi-tenant: mark_delivered/mark_delivery_failed use explicit workspace_id ──


def test_mark_delivered_uses_explicit_workspace_id_not_ambient_scope(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """The dispatcher runs outside any request's workspace scope, so
    mark_delivered/mark_delivery_failed must not rely on current_workspace_id()
    — they take workspace_id explicitly, from the claimed
    LifecycleOutboxEvent."""
    with workspace_scope(5):
        store.record_lifecycle_event(
            event_id=_eid("t1"),
            session_id=SID_A,
            event_type="session.completed",
            transition_key="turn:rt:completed",
            payload="{}",
            now=1000,
        )
        claimed = store.claim_batch(limit=10, now=2000, lease_owner="r1", lease_seconds=60)
    assert len(claimed) == 1
    assert claimed[0].workspace_id == 5

    # Mark delivered OUTSIDE any workspace_scope (ambient default workspace),
    # passing workspace_id=5 explicitly.
    store.mark_delivered(claimed[0].id, workspace_id=5, delivered_at=2005, http_status=200)

    with workspace_scope(5):
        row = store.latest_delivery(SID_A)
    assert row is not None
    assert row.status == "delivered"
