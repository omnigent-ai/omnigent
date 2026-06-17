"""Tests for the server-authoritative :class:`TurnStore`.

Covers :mod:`omnigent.stores.turn_store` (issue #466,
``designs/PHASE1_SERVER_AUTHORITATIVE_TURNS.md``). The store is the
persistence half of Phase 1; these tests exercise it against a real
SQLite database (via the ``db_uri`` fixture, which runs the full alembic
chain) with an **injected clock** — every call passes ``now`` explicitly,
so behavior is deterministic and no wall-clock is read internally.

Priority behaviors locked down (both design reviewers flagged these):

* Fenced compare-and-set: a stale ``lease_epoch`` or wrong ``runner_id``
  is rejected for heartbeat and terminal writes.
* The claim is the only place (besides the sweeper) that bumps the epoch;
  heartbeats extend expiry without bumping it.
* The orphan sweeper marks only expired RUNNING/PAUSING turns
  ``RUNNER_LOST`` **and bumps the epoch**, so a reconnecting "zombie"
  runner's terminal write then CAS-fails.
* ``mark_client_liveness`` touches only ``attached`` / ``last_client_seen``
  — never a lifecycle column (design rule 1, enforced not by convention).
* Idempotency: replay returns the same turn; same-key/different-body is a
  422-class conflict.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.runtime import turn_state as ts
from omnigent.stores.turn_store import (
    IdempotencyConflictError,
    TurnStore,
    request_fingerprint,
)

_CONV_ID = "conv_turnstore_test"


def _seed_conversation(db_uri: str, conversation_id: str) -> None:
    """Insert a minimal ``conversations`` row to satisfy the turns FK.

    ``get_or_create_engine`` enables SQLite foreign-key enforcement, so a
    turn needs a real parent conversation. Only the NOT-NULL columns
    without a default need values (same minimal insert the migration test
    uses), which keeps the fixture independent of the conversation store's
    id-generation and CHECK-constraint surface.

    :param db_uri: Per-test SQLite URI.
    :param conversation_id: The conversation id to insert.
    """
    engine = get_or_create_engine(db_uri)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations (id, created_at, updated_at, "
                "root_conversation_id) VALUES (:id, :now, :now, :id)"
            ),
            {"id": conversation_id, "now": now_epoch()},
        )


@pytest.fixture()
def turn_store(db_uri: str) -> TurnStore:
    """A TurnStore plus a seeded parent conversation to satisfy the FK.

    :param db_uri: Per-test SQLite URI with the full migration chain.
    :returns: A TurnStore bound to the same database.
    """
    _seed_conversation(db_uri, _CONV_ID)
    return TurnStore(db_uri)


def _make_turn(store: TurnStore, *, turn_id: str = "resp_t1", now: int = 1000) -> None:
    """Create a CREATED turn for the seeded conversation."""
    store.create_turn(
        turn_id=turn_id,
        conversation_id=_CONV_ID,
        vendor="claude_code",
        intent="enqueue",
        input_json="{}",
        now=now,
    )


# ── creation / reads ───────────────────────────────────────────────


def test_create_and_get_turn(turn_store: TurnStore) -> None:
    """A created turn round-trips with CREATED status and epoch 0."""
    _make_turn(turn_store, turn_id="resp_t1", now=1000)
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_CREATED
    assert turn.lease_epoch == 0
    assert turn.lease_owner is None
    assert turn.created_at == 1000
    assert turn.attached is False


def test_get_missing_turn_returns_none(turn_store: TurnStore) -> None:
    """Looking up an unknown turn id returns None, not an error."""
    assert turn_store.get_turn("resp_nope") is None


# ── claim ──────────────────────────────────────────────────────────


def test_claim_transitions_to_running_and_bumps_epoch(turn_store: TurnStore) -> None:
    """Claiming a turn moves it to RUNNING, stamps the lease, and bumps epoch."""
    _make_turn(turn_store, now=1000)
    epoch = turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    assert epoch == 1
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_RUNNING
    assert turn.lease_owner == "runner_a"
    assert turn.lease_epoch == 1
    assert turn.lease_expires_at == 1030
    assert turn.start_ts == 1000


def test_double_claim_only_one_winner(turn_store: TurnStore) -> None:
    """A second claim on an already-RUNNING turn returns None (no takeover)."""
    _make_turn(turn_store, now=1000)
    first = turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    second = turn_store.claim(turn_id="resp_t1", runner_id="runner_b", now=1001, ttl=30)
    assert first == 1
    assert second is None
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.lease_owner == "runner_a"
    assert turn.lease_epoch == 1


def test_claim_missing_turn_returns_none(turn_store: TurnStore) -> None:
    """Claiming a non-existent turn returns None rather than raising."""
    assert turn_store.claim(turn_id="resp_nope", runner_id="r", now=1, ttl=30) is None


# ── heartbeat ──────────────────────────────────────────────────────


def test_heartbeat_extends_expiry_without_bumping_epoch(turn_store: TurnStore) -> None:
    """A heartbeat pushes out lease_expires_at and never changes the epoch."""
    _make_turn(turn_store, now=1000)
    epoch = turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    assert epoch == 1
    ok = turn_store.heartbeat(
        turn_id="resp_t1", runner_id="runner_a", lease_epoch=1, now=1020, ttl=30
    )
    assert ok is True
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.lease_epoch == 1  # unchanged
    assert turn.last_heartbeat_at == 1020
    assert turn.lease_expires_at == 1050


def test_heartbeat_with_stale_epoch_rejected(turn_store: TurnStore) -> None:
    """A heartbeat carrying the wrong epoch is fenced out (returns False)."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    ok = turn_store.heartbeat(
        turn_id="resp_t1", runner_id="runner_a", lease_epoch=0, now=1020, ttl=30
    )
    assert ok is False


def test_heartbeat_with_wrong_owner_rejected(turn_store: TurnStore) -> None:
    """A heartbeat from a runner that isn't the lease owner is rejected."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    ok = turn_store.heartbeat(
        turn_id="resp_t1", runner_id="runner_b", lease_epoch=1, now=1020, ttl=30
    )
    assert ok is False


# ── complete (terminal, fenced) ────────────────────────────────────


def test_complete_finalizes_running_turn(turn_store: TurnStore) -> None:
    """The lease holder can move RUNNING -> COMPLETED and stamp end_ts."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    ok = turn_store.complete(
        turn_id="resp_t1",
        runner_id="runner_a",
        lease_epoch=1,
        terminal_status=ts.STATUS_COMPLETED,
        now=1010,
    )
    assert ok is True
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_COMPLETED
    assert turn.end_ts == 1010
    assert turn.error_code is None


def test_complete_with_worker_failure_records_error_code(turn_store: TurnStore) -> None:
    """A worker task failure isrecorded with its taxonomy code."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    ok = turn_store.complete(
        turn_id="resp_t1",
        runner_id="runner_a",
        lease_epoch=1,
        terminal_status=ts.STATUS_FAILED,
        now=1010,
        error_code=ts.ERROR_WORKER_TASK_FAILURE,
        error_message="boom",
    )
    assert ok is True
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_FAILED
    assert turn.error_code == ts.ERROR_WORKER_TASK_FAILURE
    assert ts.is_worker_attributable(turn.error_code) is True


def test_complete_with_stale_epoch_rejected(turn_store: TurnStore) -> None:
    """A terminal write with a stale epoch is fenced out (returns False)."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    ok = turn_store.complete(
        turn_id="resp_t1",
        runner_id="runner_a",
        lease_epoch=0,
        terminal_status=ts.STATUS_COMPLETED,
        now=1010,
    )
    assert ok is False
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_RUNNING  # untouched


def test_complete_rejects_non_terminal_status(turn_store: TurnStore) -> None:
    """Asking complete() to write a non-terminal status is a programming error."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    with pytest.raises(ValueError, match="terminal status"):
        turn_store.complete(
            turn_id="resp_t1",
            runner_id="runner_a",
            lease_epoch=1,
            terminal_status=ts.STATUS_RUNNING,
            now=1010,
        )


# ── orphan sweeper ─────────────────────────────────────────────────


def test_sweep_marks_expired_running_turn_runner_lost(turn_store: TurnStore) -> None:
    """An expired RUNNING turn is swept to FAILED(RUNNER_LOST) with epoch bump."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    # now (1031) is past lease_expires_at (1030).
    swept = turn_store.sweep_expired(now=1031)
    assert swept == ["resp_t1"]
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_FAILED
    assert turn.error_code == ts.ERROR_RUNNER_LOST
    assert turn.lease_epoch == 2  # bumped past the runner's epoch (1)
    # RUNNER_LOST must NOT feed vendor routing.
    assert ts.is_worker_attributable(turn.error_code) is False


def test_sweep_ignores_live_lease(turn_store: TurnStore) -> None:
    """A turn whose lease has not yet expired is left untouched."""
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    swept = turn_store.sweep_expired(now=1020)  # before expiry (1030)
    assert swept == []
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_RUNNING


def test_zombie_runner_complete_after_sweep_is_fenced(turn_store: TurnStore) -> None:
    """The headline reconnect race: a swept runner can't resurrect its turn.

    A runner is declared RUNNER_LOST while merely disconnected (the sweep
    bumps the epoch). When it reconnects and tries its terminal write with
    the *old* epoch, the fenced CAS updates zero rows, so it learns it lost
    the lease and must reconcile/discard instead of overwriting the
    server-authoritative RUNNER_LOST record. This is precisely the
    split-brain the fencing token exists to prevent.
    """
    _make_turn(turn_store, now=1000)
    held_epoch = turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)
    assert held_epoch == 1
    # Sweeper declares it lost (epoch -> 2).
    assert turn_store.sweep_expired(now=1031) == ["resp_t1"]
    # The still-alive runner reconnects and tries to complete with epoch 1.
    ok = turn_store.complete(
        turn_id="resp_t1",
        runner_id="runner_a",
        lease_epoch=held_epoch,
        terminal_status=ts.STATUS_COMPLETED,
        now=1040,
    )
    assert ok is False
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.status == ts.STATUS_FAILED
    assert turn.error_code == ts.ERROR_RUNNER_LOST


def test_sweep_respects_limit(turn_store: TurnStore, db_uri: str) -> None:
    """The sweep batches: at most ``limit`` turns transition per pass.

    Uses separate conversations because the partial-unique index allows
    only one active turn per conversation.
    """
    for i in range(3):
        cid = f"conv_sweep_{i}"
        _seed_conversation(db_uri, cid)
        turn_store.create_turn(
            turn_id=f"resp_s{i}",
            conversation_id=cid,
            vendor="claude_code",
            intent="enqueue",
            input_json="{}",
            now=1000,
        )
        turn_store.claim(turn_id=f"resp_s{i}", runner_id="runner_a", now=1000, ttl=30)
    swept = turn_store.sweep_expired(now=1031, limit=2)
    assert len(swept) == 2  # only the batch limit this pass
    remaining = turn_store.sweep_expired(now=1031, limit=2)
    assert len(remaining) == 1  # the third on the next pass


# ── client liveness (observation only) ─────────────────────────────


def test_mark_client_liveness_does_not_touch_lifecycle(turn_store: TurnStore) -> None:
    """Attach/detach updates only liveness columns, never the turn's status.

    This is design rule 1 — a dropped client can never kill a turn — and
    it is the whole point of the project. We claim the turn to RUNNING,
    then flip client liveness, and assert status / lease / epoch are all
    unchanged while attached / last_client_seen update.
    """
    _make_turn(turn_store, now=1000)
    turn_store.claim(turn_id="resp_t1", runner_id="runner_a", now=1000, ttl=30)

    turn_store.mark_client_liveness(turn_id="resp_t1", attached=True, now=1005)
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.attached is True
    assert turn.last_client_seen == 1005
    assert turn.status == ts.STATUS_RUNNING
    assert turn.lease_owner == "runner_a"
    assert turn.lease_epoch == 1

    # Client disconnects mid-run: still no lifecycle change.
    turn_store.mark_client_liveness(turn_id="resp_t1", attached=False, now=1009)
    turn = turn_store.get_turn("resp_t1")
    assert turn is not None
    assert turn.attached is False
    assert turn.last_client_seen == 1009
    assert turn.status == ts.STATUS_RUNNING  # the turn survives the disconnect
    assert turn.lease_expires_at == 1030  # lease untouched by client liveness


def test_mark_client_liveness_sql_touches_only_two_columns(turn_store: TurnStore) -> None:
    """The emitted UPDATE's SET clause names ONLY attached + last_client_seen.

    The behavioral test above proves status is unchanged, but the design
    rule is stronger: the observation path must be *structurally* incapable
    of writing any execution column. We inspect the compiled UPDATE
    statement's SET targets directly, so a future edit that accidentally
    adds, say, ``status`` to this method's ``.values(...)`` is caught even
    if no behavioral test happens to exercise that column.
    """
    captured: list[str] = []

    from sqlalchemy import event

    # The store's own engine — the same one its session maker is bound to.
    engine = turn_store._engine

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("UPDATE TURNS"):
            captured.append(statement)

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        _make_turn(turn_store, turn_id="resp_live", now=1000)
        turn_store.mark_client_liveness(turn_id="resp_live", attached=True, now=1005)
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)

    assert captured, "expected an UPDATE turns statement to be emitted"
    update_sql = captured[-1].upper()
    set_clause = update_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    # Exactly the two liveness columns may appear in the SET clause.
    assert "ATTACHED" in set_clause
    assert "LAST_CLIENT_SEEN" in set_clause
    for forbidden in ("STATUS", "LEASE_OWNER", "LEASE_EPOCH", "LEASE_EXPIRES_AT", "END_TS"):
        assert forbidden not in set_clause, (
            f"mark_client_liveness must never write {forbidden}; SET clause was: {set_clause}"
        )


# ── idempotency ────────────────────────────────────────────────────


def test_idempotency_first_use_binds_key(turn_store: TurnStore) -> None:
    """First use of a key binds it to the turn and returns that turn id."""
    _make_turn(turn_store, turn_id="resp_t1", now=1000)
    fp = request_fingerprint('{"input":"hello"}')
    bound = turn_store.upsert_idempotency_key(
        key="01890000-0000-7000-8000-000000000001",
        conversation_id=_CONV_ID,
        turn_id="resp_t1",
        fingerprint=fp,
        now=1000,
    )
    assert bound == "resp_t1"
    looked = turn_store.lookup_idempotency_key("01890000-0000-7000-8000-000000000001")
    assert looked == ("resp_t1", fp)


def test_idempotency_replay_returns_same_turn(turn_store: TurnStore) -> None:
    """Replaying the same key with the same body returns the original turn."""
    _make_turn(turn_store, turn_id="resp_t1", now=1000)
    key = "01890000-0000-7000-8000-000000000002"
    fp = request_fingerprint('{"input":"hello"}')
    turn_store.upsert_idempotency_key(
        key=key, conversation_id=_CONV_ID, turn_id="resp_t1", fingerprint=fp, now=1000
    )
    # A retried send (e.g. after reconnect) re-presents the same key/body;
    # it must resolve back to resp_t1, not mint a new turn.
    again = turn_store.upsert_idempotency_key(
        key=key,
        conversation_id=_CONV_ID,
        turn_id="resp_should_be_ignored",
        fingerprint=fp,
        now=1001,
    )
    assert again == "resp_t1"


def test_idempotency_same_key_different_body_conflicts(turn_store: TurnStore) -> None:
    """Reusing a key with a different payload raises (maps to HTTP 422)."""
    _make_turn(turn_store, turn_id="resp_t1", now=1000)
    key = "01890000-0000-7000-8000-000000000003"
    turn_store.upsert_idempotency_key(
        key=key,
        conversation_id=_CONV_ID,
        turn_id="resp_t1",
        fingerprint=request_fingerprint('{"input":"hello"}'),
        now=1000,
    )
    with pytest.raises(IdempotencyConflictError):
        turn_store.upsert_idempotency_key(
            key=key,
            conversation_id=_CONV_ID,
            turn_id="resp_t1",
            fingerprint=request_fingerprint('{"input":"DIFFERENT"}'),
            now=1001,
        )


def test_purge_idempotency_keys_before_cutoff(turn_store: TurnStore) -> None:
    """The TTL sweep removes keys created before the cutoff and counts them."""
    _make_turn(turn_store, turn_id="resp_t1", now=1000)
    turn_store.upsert_idempotency_key(
        key="01890000-0000-7000-8000-000000000010",
        conversation_id=_CONV_ID,
        turn_id="resp_t1",
        fingerprint=request_fingerprint("{}"),
        now=1000,
    )
    deleted = turn_store.purge_idempotency_keys_before(cutoff=2000)
    assert deleted == 1
    assert turn_store.lookup_idempotency_key("01890000-0000-7000-8000-000000000010") is None
