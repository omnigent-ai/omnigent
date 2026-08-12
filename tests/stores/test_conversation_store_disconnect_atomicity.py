"""Atomicity of the disconnect write vs a concurrent cross-replica read (OMN-104).

Cross-vendor review, P2: the disconnect path used to call
``ConversationStore.clear_runner_liveness`` and
``ConversationStore.set_runner_disconnect_grace`` as two SEPARATE store
writes (two transactions, two commits). Because they were separate, another
replica reading ``get_session_connectivity`` in the real gap between those
two commits could observe BOTH ``runner_last_seen`` cleared AND
``runner_disconnect_grace_deadline`` still absent simultaneously — the
runner looking neither live nor grace-pending — and a manager decision
landing in that exact window would be falsely classified as "runner dead"
(410) even though the runner is genuinely still within its reconnect grace.

The fix (``ConversationStore.mark_runner_disconnected``) persists both
fields in ONE ``UPDATE`` / one commit, so there is no third, in-between
state a reader can ever observe — this file proves that with genuine
concurrent threads racing real reads against real writes on whatever
backend ``db_uri`` resolves to (SQLite locally, real Postgres in CI's
``stores-postgres`` lane, where a torn read is actually possible under
READ COMMITTED with two separate transactions).
"""

from __future__ import annotations

import threading
import time

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

SID_A = "a1" * 16
RUNNER_ID = "runner_disconnect_atomicity_test"
_READER_ITERATIONS = 400


def _seed_bound_session(store: SqlAlchemyConversationStore) -> None:
    """Create a real session bound to RUNNER_ID with fresh liveness (the
    exact pre-disconnect state: connected, no grace pending)."""
    store.create_conversation(conversation_id=SID_A)
    assert store.set_runner_id(SID_A, RUNNER_ID)
    store.touch_runner_liveness([RUNNER_ID], int(time.time()))


def _is_torn(store: SqlAlchemyConversationStore) -> bool:
    """True iff the runner currently looks neither live nor grace-pending
    — the bad, "in between" state a reader must never observe while the
    runner is genuinely mid-disconnect (either it's still fresh-live, or
    it's cleared-but-graced; never both absent)."""
    conn = store.get_session_connectivity([SID_A])[SID_A]
    now = time.time()
    live = conn.runner_last_seen is not None and conn.runner_last_seen >= now - 90
    graced = (
        conn.runner_disconnect_grace_deadline is not None
        and conn.runner_disconnect_grace_deadline > now
    )
    return not live and not graced


def test_mark_runner_disconnected_atomic_write_has_no_torn_read_window(db_uri: str) -> None:
    """
    The FIXED path: a real concurrent reader hammering
    ``get_session_connectivity`` while ``mark_runner_disconnected`` writes,
    across many disconnect/reconnect cycles, must never observe the torn
    "neither live nor graced" state.
    """
    store = SqlAlchemyConversationStore(db_uri)
    _seed_bound_session(store)

    torn_observations: list[int] = []
    stop = threading.Event()

    def _reader() -> None:
        count = 0
        while not stop.is_set():
            if _is_torn(store):
                torn_observations.append(count)
            count += 1

    reader = threading.Thread(target=_reader)
    reader.start()
    try:
        for _ in range(30):
            store.mark_runner_disconnected(RUNNER_ID, int(time.time()) + 100)
            # Reconnect: restore fresh liveness and clear the grace marker
            # so the next iteration starts from the same valid pre-disconnect
            # state. (The reconnect ordering itself is safe even as two
            # writes — see mark_runner_disconnected's docstring: reconnect
            # only ever ADDS truthiness, so a reader mid-reconnect sees
            # both signals true at once, never both false.)
            store.touch_runner_liveness([RUNNER_ID], int(time.time()))
            store.clear_runner_disconnect_grace(RUNNER_ID)
    finally:
        stop.set()
        reader.join(timeout=10)

    assert torn_observations == [], (
        f"observed {len(torn_observations)} torn read(s) despite the atomic write"
    )


def test_two_separate_writes_are_not_atomic_demonstrates_the_fixed_bug(db_uri: str) -> None:
    """
    Demonstrates the exact bug P2 fixed: calling ``clear_runner_liveness``
    then ``set_runner_disconnect_grace`` as two SEPARATE store writes (the
    pre-fix disconnect sequence) is NOT atomic — a concurrent reader CAN
    observe the torn "neither live nor graced" state in the real gap
    between the two commits. This does not exercise any production code
    path (the disconnect handler was migrated to the atomic
    ``mark_runner_disconnected`` and never calls these two separately
    anymore) — it proves the OLD two-write PATTERN itself is genuinely
    unsafe, using the same store primitives still available for other
    callers, with a small explicit gap between the two calls. The gap only
    makes the window reliably observable under test timing; the real
    production gap (two separate ``_submit``-queued writes on a
    single-worker executor) was real but not bounded to be this narrow —
    widening it here removes timing luck from the test, not the mechanism
    under test.
    """
    store = SqlAlchemyConversationStore(db_uri)
    _seed_bound_session(store)

    torn_observations: list[int] = []
    stop = threading.Event()

    def _reader() -> None:
        count = 0
        while not stop.is_set():
            if _is_torn(store):
                torn_observations.append(count)
            count += 1

    reader = threading.Thread(target=_reader)
    reader.start()
    try:
        store.clear_runner_liveness(RUNNER_ID)
        time.sleep(0.2)  # widen the real gap deterministically for the test
        store.set_runner_disconnect_grace(RUNNER_ID, int(time.time()) + 100)
        time.sleep(0.05)
    finally:
        stop.set()
        reader.join(timeout=10)

    assert torn_observations, (
        "expected the two-separate-write sequence to expose a torn read — "
        "if this assertion fails, the demonstrated pattern is no longer "
        "reproducing the bug the atomic fix addresses"
    )
