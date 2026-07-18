"""Tests for the session live-state persistence chokepoint.

Covers ``omnigent.server.session_live_state``: writes are deduplicated,
ordered, and best-effort, and the pending-elicitations index drives the
persisted count through its hook. These writes are what let a replica
that does NOT hold a session's runner tunnel serve the sidebar's live
fields, so the contract under test is "every real transition reaches the
store exactly once".

Writes land on a background single-worker executor, so each test waits
on the observable effect (the recording store's captured writes) with a
short polling deadline — the same shape as the host-tunnel route tests'
``_wait_*`` helpers — rather than reaching into the module's executor.
"""

from __future__ import annotations

import time

import pytest

from omnigent.runtime import pending_elicitations
from omnigent.server import session_live_state


def _wait_until(predicate, *, timeout_s: float = 10.0) -> None:
    """Poll until *predicate* holds or the deadline elapses.

    The live-state writes are applied on a background executor thread, so
    a test asserting on their effect must wait for that thread rather than
    read synchronously. Returning on timeout (instead of raising) lets the
    caller's own assertion produce the informative failure — including the
    "should NOT have happened" cases where the predicate never becomes
    true by design.

    A passing predicate returns immediately, so the ceiling is only hit on
    a genuine failure; it's set generously (not ~2s) so a loaded CI runner
    doesn't spuriously time out before the background worker drains.

    :param predicate: Zero-arg callable returning truthy when done.
    :param timeout_s: Max seconds to poll before giving up.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)


class _RecordingStore:
    """Conversation-store stand-in that records live-state writes."""

    def __init__(self) -> None:
        self.status_writes: list[tuple[str, str]] = []
        self.pending_writes: list[tuple[str, int]] = []
        self.touches: list[list[str]] = []
        self.clears: list[str] = []

    def set_session_live_status(self, conversation_id: str, status: str) -> None:
        self.status_writes.append((conversation_id, status))

    def set_pending_elicitation_count(self, conversation_id: str, count: int) -> None:
        self.pending_writes.append((conversation_id, count))

    def touch_runner_liveness(self, runner_ids: list[str], now: int) -> None:
        del now
        self.touches.append(runner_ids)

    def clear_runner_liveness(self, runner_id: str) -> None:
        self.clears.append(runner_id)


@pytest.fixture()
def recording_store() -> _RecordingStore:
    """Wire a recording store into the module; unwire on teardown."""
    store = _RecordingStore()
    session_live_state.configure(store)  # type: ignore[arg-type]
    yield store
    session_live_state.configure(None)
    pending_elicitations.set_count_persist_hook(None)


def test_persist_live_status_dedupes_transitions(recording_store: _RecordingStore) -> None:
    """Only actual transitions reach the store; re-publishes are dropped.

    The SSE relay republishes statuses freely (e.g. a PTY watcher's
    repeated ``idle``); without dedupe every list tick would turn into
    row churn on the conversations table.
    """
    session_live_state.persist_live_status("conv_1", "running")
    session_live_state.persist_live_status("conv_1", "running")
    session_live_state.persist_live_status("conv_1", "idle")
    session_live_state.persist_live_status("conv_2", "idle")
    _wait_until(lambda: len(recording_store.status_writes) >= 3)
    assert recording_store.status_writes == [
        ("conv_1", "running"),
        ("conv_1", "idle"),
        ("conv_2", "idle"),
    ]


def test_pending_count_hook_persists_publish_and_resolve(
    recording_store: _RecordingStore,
) -> None:
    """The elicitation index drives the persisted count through its hook.

    A publish bumps the count, a duplicate publish of the same id does
    not (same count → deduped), and a resolve writes the decrement —
    including the direct-``resolve`` path the approval dispatch uses,
    which never flows through ``record_publish``.
    """
    pending_elicitations.set_count_persist_hook(session_live_state.persist_pending_count)
    request = {"type": "response.elicitation_request", "elicitation_id": "elicit_1"}
    pending_elicitations.record_publish("conv_1", request)
    pending_elicitations.record_publish("conv_1", request)  # idempotent re-publish
    pending_elicitations.record_publish(
        "conv_1", {"type": "response.elicitation_request", "elicitation_id": "elicit_2"}
    )
    pending_elicitations.resolve("conv_1", "elicit_1")
    pending_elicitations.resolve("conv_1", "elicit_1")  # idempotent re-resolve
    pending_elicitations.resolve("conv_1", "elicit_2")
    _wait_until(lambda: len(recording_store.pending_writes) >= 4)
    assert recording_store.pending_writes == [
        ("conv_1", 1),
        ("conv_1", 2),
        ("conv_1", 1),
        ("conv_1", 0),
    ]


def test_runner_liveness_touch_and_clear_pass_through(
    recording_store: _RecordingStore,
) -> None:
    """Touches and clears reach the store; empty touch is a no-op."""
    session_live_state.touch_runner_liveness(["runner_a", "runner_b"])
    session_live_state.touch_runner_liveness([])
    session_live_state.clear_runner_liveness("runner_a")
    _wait_until(lambda: recording_store.touches and recording_store.clears)
    assert recording_store.touches == [["runner_a", "runner_b"]]
    assert recording_store.clears == ["runner_a"]


def test_unconfigured_module_is_a_no_op() -> None:
    """Without a wired store (runner process, most tests) nothing runs.

    No store means no executor work is enqueued; the calls simply return.
    """
    session_live_state.configure(None)
    session_live_state.persist_live_status("conv_1", "running")
    session_live_state.persist_pending_count("conv_1", 1)
    session_live_state.touch_runner_liveness(["runner_a"])
    session_live_state.clear_runner_liveness("runner_a")


def test_write_runs_in_callers_workspace_scope(recording_store: _RecordingStore) -> None:
    """The store write inherits the caller's ``workspace_scope``.

    The store filters every query on ``current_workspace_id()`` — a
    ``ContextVar`` the multi-tenant middleware binds per request. The
    write runs on a background ``ThreadPoolExecutor``; a bare
    ``submit`` would run it at the default workspace (0), so on a
    multi-tenant replica every ``WHERE workspace_id == …`` would match
    no rows and the whole mirror would silently no-op. Copying the
    caller's context (as ``_submit`` does) is what carries the bound
    workspace to the worker thread. This test binds a non-default
    workspace and asserts the write thread observes it.
    """
    from omnigent.db.db_models import current_workspace_id, workspace_scope

    seen: list[int] = []

    def _record_ws(conversation_id: str, status: str) -> None:
        del conversation_id, status
        seen.append(current_workspace_id())

    recording_store.set_session_live_status = _record_ws  # type: ignore[method-assign]

    with workspace_scope(4242):
        session_live_state.persist_live_status("conv_1", "running")
    # The scope is left BEFORE the worker runs (the ``with`` block is
    # already closed here), so a passing assertion proves the value was
    # captured at submit time rather than read live off the worker thread
    # (which is always at the default workspace).
    _wait_until(lambda: bool(seen))

    assert seen == [4242], "write thread did not observe the caller's bound workspace"


def test_dropped_write_evicts_dedupe_entry_for_retry() -> None:
    """A dropped best-effort write must not pin a stale dedupe entry.

    ``persist_live_status`` records the value in its dedupe cache before
    enqueueing the (best-effort) write. If that write is dropped, a later
    *identical* publish would be deduped away and the row would stay stale
    until a different value arrived. On failure the entry is evicted, so
    the next identical publish re-attempts the write.
    """
    calls: list[tuple[str, str]] = []

    class _FlakyStore(_RecordingStore):
        def set_session_live_status(self, conversation_id: str, status: str) -> None:
            calls.append((conversation_id, status))
            if len(calls) == 1:
                raise RuntimeError("boom")  # first attempt fails
            super().set_session_live_status(conversation_id, status)

    store = _FlakyStore()
    session_live_state.configure(store)  # type: ignore[arg-type]
    try:
        session_live_state.persist_live_status("conv_1", "running")
        # Wait for the EVICTION, not just the first call: the worker
        # appends to ``calls`` and only then runs the failure callback that
        # drops the dedupe entry. Gating the retry on "conv_1" leaving the
        # dedupe map (the exact contract under test) removes the race where
        # the second publish races the eviction and gets deduped away.
        _wait_until(lambda: "conv_1" not in session_live_state._last_status)
        assert "conv_1" not in session_live_state._last_status, "dropped write did not evict"

        # Same value again: without the eviction above this would dedupe to
        # a no-op; because the entry was cleared, the retry re-attempts.
        session_live_state.persist_live_status("conv_1", "running")
        _wait_until(lambda: bool(store.status_writes))
    finally:
        session_live_state.configure(None)

    assert calls == [("conv_1", "running"), ("conv_1", "running")]
    assert store.status_writes == [("conv_1", "running")], "retry never landed"


def test_unencodable_status_is_dropped_before_enqueue(
    recording_store: _RecordingStore,
) -> None:
    """A status the codec can't encode never reaches the store.

    ``SessionStatusEvent.status`` permits ``"launching"``, which the
    live-status codec (``SESSION_LIVE_STATUS``) can't encode, and the SSE
    relay forwards raw event statuses. Enqueueing it would make the store
    write raise, whose best-effort failure hook clears the dedupe entry, so
    every republish would re-attempt and re-warn. ``persist_live_status``
    instead drops an unknown status before the enqueue: no store write, and
    a later *known* status for the same session still persists normally.
    """
    session_live_state.persist_live_status("conv_1", "launching")  # unknown
    session_live_state.persist_live_status("conv_1", "launching")  # repeat, deduped
    session_live_state.persist_live_status("conv_1", "running")  # known → persists
    _wait_until(lambda: bool(recording_store.status_writes))

    # Only the encodable status was ever enqueued; "launching" never was.
    assert recording_store.status_writes == [("conv_1", "running")]


@pytest.mark.asyncio
async def test_liveness_pass_zeroes_pending_count_for_offline_runner() -> None:
    """A stale persisted pending count can't light a phantom inbox badge.

    Nothing decrements the persisted ``pending_elicitation_count`` when a
    runner/host/replica crashes with prompts parked, so the row can outlive
    the prompts it counts. The liveness pass (shared by ``GET /v1/sessions``
    and the ``/v1/sessions/updates`` watched-items fetch) zeroes the count on
    rows whose runner is offline — dead runner means dead prompts — while an
    online runner's count passes through untouched.
    """
    from omnigent.server.routes.sessions import SessionLiveness, _apply_liveness_to_items
    from omnigent.server.schemas import SessionListItem

    def _item(session_id: str) -> SessionListItem:
        return SessionListItem(
            id=session_id,
            agent_id="ag_1",
            status="idle",
            created_at=1,
            updated_at=1,
            pending_elicitations_count=3,
        )

    items = [_item("conv_dead"), _item("conv_live")]
    liveness = {
        "conv_dead": SessionLiveness(runner_online=False, host_online=True),
        "conv_live": SessionLiveness(runner_online=True, host_online=True),
    }

    await _apply_liveness_to_items(items, lambda ids: {i: liveness[i] for i in ids})

    assert [(i.runner_online, i.pending_elicitations_count) for i in items] == [
        (False, 0),
        (True, 3),
    ]
