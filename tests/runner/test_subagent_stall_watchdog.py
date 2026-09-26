"""A started sub-agent that goes silent must be adjudicated, not blindly failed.

``reap_stalled_subagent_launches`` only bounds a child wedged *before* its
first edge. Once a child reaches ``running`` nothing else bounded its lifetime,
so any harness drift that lost the child's terminal edge presented identically:
the work handle stayed ``running`` forever and the parent's inbox stayed empty
with no error surfaced anywhere.

The stall sweep is the backstop. It is keyed on the absence of runner-visible
edges, never on wall-clock since dispatch, so a healthy long-running child that
is still emitting activity is not killed. Silence past the budget is only a
*candidate*: the child is then adjudicated against its own bounded server-side
snapshot before anything is delivered, so a native-approval wait, a completion
the runner merely missed, and an unreadable server are each handled without
losing a result. A genuinely-stuck child is interrupted and given a
*provisional* failure that a real terminal edge can still supersede — and
because every verdict and interrupt is awaited, the entry's identity and status
are re-checked after each await so a result arriving mid-decision is never
overwritten.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, pending_approvals, tool_dispatch
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_worker"
STALL_TIMEOUT_S = 900.0

_JsonObject = dict[str, Any]
_ReadChild = Callable[[str], Awaitable[_JsonObject | None]]


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox maps.

    The registry lives in module-level dicts on ``omnigent.runner.app`` that
    otherwise leak across tests.
    """
    saved = (
        dict(runner_app._subagent_work_by_child),
        {k: set(v) for k, v in runner_app._subagent_work_by_parent.items()},
        dict(runner_app._session_inboxes_ref),
        set(runner_app._drained_delivered_subagent_children),
    )
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved[0])
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved[1])
        runner_app._session_inboxes_ref.clear()
        runner_app._session_inboxes_ref.update(saved[2])
        runner_app._drained_delivered_subagent_children.clear()
        runner_app._drained_delivered_subagent_children.update(saved[3])


class _WakeRecordingServerClient(NullServerClient):
    """Server-client stub that records the parent wake / control POSTs it gets."""

    def __init__(self) -> None:
        self.wake_posts: list[str] = []
        self.event_posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Record ``/events`` POSTs so parent wakes and interrupts can be counted.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_parent/events"``.
        :param kwargs: Request keyword arguments; ``json`` carries the body.
        :returns: Stub 200 response.
        """
        if url.endswith("/events"):
            body = kwargs.get("json") or {}
            self.event_posts.append((url, body))
            if body.get("type") not in ("interrupt", "stop_session"):
                self.wake_posts.append(url)
        return self._Response()


def _snapshot(*, status: str = "running", pending: list[_JsonObject] | None = None) -> _JsonObject:
    """Build one child ``SessionResponse``-shaped snapshot for adjudication."""
    return {"status": status, "pending_elicitations": pending or []}


def _reader(snapshot: _JsonObject | None) -> _ReadChild:
    """Return a ``read_child`` stub that always answers *snapshot*."""

    async def _read(child_id: str) -> _JsonObject | None:
        del child_id
        return snapshot

    return _read


def _forbidden_reader() -> _ReadChild:
    """Return a ``read_child`` stub that must never be called."""

    async def _read(child_id: str) -> _JsonObject | None:
        raise AssertionError("a non-candidate child must not be adjudicated")

    return _read


def _recording_interrupt(sink: list[str]) -> Callable[..., Awaitable[None]]:
    """Return an async ``interrupt`` stub that records the child ids it stops."""

    async def _interrupt(entry: runner_app._SubagentWorkEntry) -> None:
        sink.append(entry.child_session_id)

    return _interrupt


def _dispatch_running_child() -> runner_app._SubagentWorkEntry:
    """Register a dispatched child and promote it to ``running``.

    :returns: The registered work entry, in ``running`` status.
    """
    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="worker",
        title="long task",
    )
    entry.status = "running"
    return entry


@pytest.mark.asyncio
async def test_genuinely_stuck_child_is_interrupted_and_provisionally_failed(
    _clean_subagent_registry: None,
) -> None:
    """A silent child the server confirms in-progress is interrupted, then failed."""
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()
    interrupts: list[str] = []

    reaped = await runner_app.reap_stalled_subagent_dispatches(
        now=entry.last_activity_at + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
        read_child=_reader(_snapshot(status="running")),
        interrupt=_recording_interrupt(interrupts),
    )

    assert [e.child_session_id for e in reaped] == [CHILD_SESSION_ID]
    assert interrupts == [CHILD_SESSION_ID]
    assert entry.status == "failed"
    assert entry.stalled  # provisional, not authoritative
    payload = inbox.get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "failed"
    assert "provisional" in payload["output"]

    # A second sweep must not re-warn: the entry is no longer ``running``.
    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            mark_terminal=app.state.mark_subagent_terminal_and_wake,
            read_child=_reader(_snapshot(status="running")),
        )
        == []
    )
    assert inbox.empty()

    for _ in range(100):
        if server.wake_posts:
            break
        await asyncio.sleep(0.01)
    assert len(server.wake_posts) == 1, (
        f"Expected exactly one parent wake POST, got {server.wake_posts}"
    )


@pytest.mark.asyncio
async def test_heartbeat_keeps_child_alive_then_silence_is_reaped(
    _clean_subagent_registry: None,
) -> None:
    """The end-to-end activity path: refreshed while healthy, reaped once silent.

    A child that keeps reporting through the runner's child-event funnel
    (``note_subagent_activity``) stays alive past the budget; once its
    heartbeat stops and the budget elapses, the parent gets the failure.
    """
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()
    entry.created_at = entry.created_at - STALL_TIMEOUT_S * 9  # dispatched long ago

    # Heartbeats keep refreshing activity; each sweep is a budget-and-a-bit after
    # the previous heartbeat but the fresh edge keeps the child alive.
    for beat in range(3):
        runner_app.note_subagent_activity(CHILD_SESSION_ID)
        beat_at = entry.last_activity_at
        assert (
            await runner_app.reap_stalled_subagent_dispatches(
                now=beat_at + STALL_TIMEOUT_S - 1,
                timeout_s=STALL_TIMEOUT_S,
                mark_terminal=app.state.mark_subagent_terminal_and_wake,
                read_child=_forbidden_reader(),  # never even a candidate while fresh
            )
            == []
        ), f"heartbeat {beat} must keep the child alive"
        assert entry.status == "running"

    # Heartbeat stops; silence past the budget now reaps it and wakes the parent.
    last_beat = entry.last_activity_at
    reaped = await runner_app.reap_stalled_subagent_dispatches(
        now=last_beat + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
        read_child=_reader(_snapshot(status="running")),
    )
    assert [e.child_session_id for e in reaped] == [CHILD_SESSION_ID]
    assert inbox.get_nowait()["status"] == "failed"


@pytest.mark.asyncio
async def test_child_still_emitting_activity_is_not_reaped(
    _clean_subagent_registry: None,
) -> None:
    """A long-running child that keeps emitting edges outlives the stall budget.

    The sweep must key on silence, not on wall-clock since dispatch: this
    child was dispatched well beyond the budget ago but produced an edge a
    moment ago, so it is never even adjudicated.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()
    entry.created_at = entry.created_at - STALL_TIMEOUT_S * 5
    runner_app.note_subagent_activity(CHILD_SESSION_ID)

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S - 1,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_forbidden_reader(),
        )
        == []
    )
    assert entry.status == "running"
    assert inbox.empty()


@pytest.mark.asyncio
async def test_stall_sweep_is_disabled_by_a_non_positive_budget(
    _clean_subagent_registry: None,
) -> None:
    """A ``<= 0`` budget opts out of the backstop entirely."""
    entry = _dispatch_running_child()

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + 10_000,
            timeout_s=0,
            read_child=_forbidden_reader(),
        )
        == []
    )
    assert entry.status == "running"


@pytest.mark.asyncio
async def test_stall_sweep_leaves_launching_children_to_the_launch_sweep(
    _clean_subagent_registry: None,
) -> None:
    """A child that never started is the launch sweep's business, not this one."""
    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="worker",
        title="never started",
    )
    assert entry.status == "launching"

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + 10_000,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_forbidden_reader(),
        )
        == []
    )
    assert entry.status == "launching"


@pytest.mark.asyncio
async def test_recovered_waiting_child_is_left_to_reconciliation(
    _clean_subagent_registry: None,
) -> None:
    """A restart-recovered ``waiting`` dispatch is the reconcile loop's business."""
    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID,
        child_session_id=CHILD_SESSION_ID,
        agent="worker",
        title="recovered",
    )
    entry.status = "waiting"

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_forbidden_reader(),
        )
        == []
    )
    assert entry.status == "waiting"


@pytest.mark.asyncio
async def test_child_with_runner_local_pending_approval_is_not_reaped(
    _clean_subagent_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child parked on a runner-local ASK verdict is skipped before adjudication."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()
    monkeypatch.setattr(
        pending_approvals, "has_pending", lambda conv_id: conv_id == CHILD_SESSION_ID
    )

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_forbidden_reader(),
        )
        == []
    )
    assert entry.status == "running"
    assert not entry.stalled
    assert inbox.empty()


@pytest.mark.asyncio
async def test_child_parked_on_native_approval_is_not_reaped(
    _clean_subagent_registry: None,
) -> None:
    """The server reports a pending native permission prompt: leave the child alone.

    Native prompts live only in the server-side elicitation index and are
    replayed on the child's snapshot, so the verdict comes from there, not
    runner-local state.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()
    interrupts: list[str] = []

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_reader(_snapshot(status="running", pending=[{"elicitation_id": "e1"}])),
            interrupt=_recording_interrupt(interrupts),
        )
        == []
    )
    assert entry.status == "running"
    assert not entry.stalled
    assert interrupts == []
    assert inbox.empty()


@pytest.mark.asyncio
async def test_server_idle_child_is_demoted_to_waiting_not_failed(
    _clean_subagent_registry: None,
) -> None:
    """The dispatched turn already ended server-side: hand it to reconciliation.

    The runner merely missed the terminal edge (the server session is
    ``idle``), so the sweep must not synthesize a failure over the real
    result; it demotes the entry to ``waiting`` for the reconcile pass.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_reader(_snapshot(status="idle")),
        )
        == []
    )
    assert entry.status == "waiting"
    assert not entry.stalled
    assert inbox.empty()


@pytest.mark.asyncio
async def test_unreadable_server_leaves_child_for_the_next_sweep(
    _clean_subagent_registry: None,
) -> None:
    """An unreadable server is not proof of a hang: retry, do not fail on a guess."""
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_reader(None),
        )
        == []
    )
    assert entry.status == "running"
    assert not entry.stalled
    assert inbox.empty()


@pytest.mark.asyncio
async def test_completion_arriving_during_classification_is_not_overwritten(
    _clean_subagent_registry: None,
) -> None:
    """A real completion landing during the verdict await wins the race.

    The child finishes for real while the sweep is reading its snapshot; the
    re-check after that await must abort so the authoritative completion is
    never overwritten by a provisional failure.
    """
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    async def _read_then_complete(child_id: str) -> dict[str, Any]:
        # The child completes for real mid-classification.
        app.state.mark_subagent_terminal_and_wake(
            CHILD_SESSION_ID, status="completed", output="the real answer"
        )
        return _snapshot(status="running")  # stale view

    reaped = await runner_app.reap_stalled_subagent_dispatches(
        now=entry.last_activity_at + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
        read_child=_read_then_complete,
    )

    assert reaped == []
    assert entry.status == "completed"
    assert not entry.stalled
    payload = inbox.get_nowait()
    assert payload["status"] == "completed"
    assert payload["output"] == "the real answer"
    assert inbox.empty()


@pytest.mark.asyncio
async def test_interrupt_that_reports_cancellation_is_not_overwritten(
    _clean_subagent_registry: None,
) -> None:
    """An interrupt that synchronously cancels the child wins the race.

    The interrupt handler delivers an authoritative ``cancelled`` result; the
    re-check after the interrupt await must abort so the provisional failure
    never overwrites it.
    """
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    async def _interrupt_then_cancel(work: runner_app._SubagentWorkEntry) -> None:
        app.state.mark_subagent_terminal_and_wake(
            work.child_session_id, status="cancelled", output="stopped by user"
        )

    reaped = await runner_app.reap_stalled_subagent_dispatches(
        now=entry.last_activity_at + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
        read_child=_reader(_snapshot(status="running")),
        interrupt=_interrupt_then_cancel,
    )

    assert reaped == []
    assert entry.status == "cancelled"
    assert not entry.stalled
    payload = inbox.get_nowait()
    assert payload["status"] == "cancelled"
    assert payload["output"] == "stopped by user"
    assert inbox.empty()


@pytest.mark.asyncio
async def test_redispatch_during_await_is_not_failed(
    _clean_subagent_registry: None,
) -> None:
    """A new dispatch replacing the child during the await is left untouched.

    If the original entry is unregistered and a fresh dispatch takes the same
    child id mid-decision, the sweep must not fail the newcomer by child id.
    """
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    original = _dispatch_running_child()

    async def _read_then_redispatch(child_id: str) -> dict[str, Any]:
        runner_app.unregister_subagent_work(CHILD_SESSION_ID, work_id=original.work_id)
        replacement = runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="worker",
            title="fresh dispatch",
        )
        replacement.status = "running"
        return _snapshot(status="running")

    reaped = await runner_app.reap_stalled_subagent_dispatches(
        now=original.last_activity_at + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
        read_child=_read_then_redispatch,
    )

    assert reaped == []
    replacement = runner_app.get_subagent_work(CHILD_SESSION_ID)
    assert replacement is not None and replacement is not original
    assert replacement.status == "running"
    assert not replacement.stalled
    assert inbox.empty()


@pytest.mark.asyncio
async def test_genuine_result_supersedes_provisional_stall_through_drain(
    _clean_subagent_registry: None,
) -> None:
    """The confirmed regression: a real result after the parent drains the stall.

    Reproduces the production sequence exactly — the watchdog delivers a
    provisional failure, the parent drains it through
    ``_cleanup_drained_subagent_work`` (the real inbox cleanup, not a queue
    peek), and only THEN does the child's genuine completion arrive. Because
    the provisional stall keeps the entry registered rather than remembered as
    drained, the real result supersedes it and is re-delivered instead of being
    discarded as ``already_delivered``.
    """
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    await runner_app.reap_stalled_subagent_dispatches(
        now=entry.last_activity_at + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
        read_child=_reader(_snapshot(status="running")),
    )
    assert entry.stalled
    stall_payload = inbox.get_nowait()
    assert stall_payload["status"] == "failed"

    # Drain through the production cleanup step. A provisional stall must NOT be
    # evicted or remembered as drained.
    await tool_dispatch._cleanup_drained_subagent_work(stall_payload, server_client=server)
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is entry
    assert CHILD_SESSION_ID not in runner_app._drained_delivered_subagent_children

    # The child's real completion lands after the parent already drained.
    ack = app.state.mark_subagent_terminal_and_wake(
        CHILD_SESSION_ID, status="completed", output="the real answer"
    )
    assert ack.delivered_now
    assert entry.status == "completed"
    assert not entry.stalled
    completed_payload = inbox.get_nowait()
    assert completed_payload["status"] == "completed"
    assert completed_payload["output"] == "the real answer"

    # And once the genuine result is drained, the entry cleans up normally.
    await tool_dispatch._cleanup_drained_subagent_work(completed_payload, server_client=server)
    assert runner_app.get_subagent_work(CHILD_SESSION_ID) is None


@pytest.mark.asyncio
async def test_provisionally_stalled_task_stays_cancellable(
    _clean_subagent_registry: None,
) -> None:
    """A provisional stall must keep the child cancellable by task id.

    ``sys_cancel_task`` normally returns a cached terminal status without
    interrupting once an entry is ``failed``. A provisional stall is not
    authoritative, so the cancel must still route a real interrupt to the
    possibly-live child instead of a cached ``failed``.
    """
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    await runner_app.reap_stalled_subagent_dispatches(
        now=entry.last_activity_at + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
        read_child=_reader(_snapshot(status="running")),
    )
    assert entry.status == "failed"
    assert entry.stalled
    server.event_posts.clear()

    result = await tool_dispatch._cancel_subagent_task(
        {"task_id": CHILD_SESSION_ID},
        conversation_id=PARENT_SESSION_ID,
        server_client=server,
    )

    interrupts = [url for url, body in server.event_posts if body.get("type") == "interrupt"]
    assert interrupts == [f"/v1/sessions/{CHILD_SESSION_ID}/events"], (
        f"cancel must route a real interrupt for a provisional stall, got {server.event_posts}"
    )
    assert '"cached"' not in result
    assert "cancel_requested" in result or "best_effort" in result or "cancelled" in result


@pytest.mark.asyncio
async def test_grandchild_activity_keeps_the_waiting_orchestrator_alive(
    _clean_subagent_registry: None,
) -> None:
    """P -> C -> G: a productive grandchild keeps its waiting parent C alive.

    C dispatched G and is parked awaiting its result, so C emits no edges of
    its own. G's activity must refresh C up the ancestor chain, or the watchdog
    would falsely reap a healthy orchestrator.
    """
    child_c = "conv_orchestrator_c"
    child_g = "conv_grandchild_g"
    entry_c = runner_app.register_subagent_work(
        parent_session_id=PARENT_SESSION_ID, child_session_id=child_c, agent="c", title="mid"
    )
    entry_c.status = "running"
    entry_g = runner_app.register_subagent_work(
        parent_session_id=child_c, child_session_id=child_g, agent="g", title="leaf"
    )
    entry_g.status = "running"
    stale = entry_c.last_activity_at - STALL_TIMEOUT_S * 9
    entry_c.last_activity_at = stale
    entry_g.last_activity_at = stale

    # The grandchild reports in through the runner's child-event funnel.
    runner_app.note_subagent_activity(child_g)
    assert entry_g.last_activity_at > stale
    assert entry_c.last_activity_at > stale, "grandchild activity must refresh its parent"

    # A sweep within a budget of that fresh edge reaps neither: C is alive.
    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry_c.last_activity_at + STALL_TIMEOUT_S - 1,
            timeout_s=STALL_TIMEOUT_S,
            read_child=_forbidden_reader(),
        )
        == []
    )
    assert entry_c.status == "running"
    assert entry_g.status == "running"


@pytest.mark.asyncio
async def test_sweep_adjudicates_a_bounded_batch_per_tick(
    _clean_subagent_registry: None,
) -> None:
    """More stale entries than the per-sweep cap yield only a bounded read batch."""
    cap = runner_app._SUBAGENT_STALL_ADJUDICATIONS_PER_SWEEP
    now = 1_000_000.0
    stale = now - STALL_TIMEOUT_S * 2
    for i in range(cap + 5):
        entry = runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=f"conv_child_{i}",
            agent="worker",
            title=f"task {i}",
        )
        entry.status = "running"
        entry.last_activity_at = stale

    reads: list[str] = []

    async def _read(child_id: str) -> dict[str, Any]:
        reads.append(child_id)
        return _snapshot(status="idle")  # terminal -> demote to waiting, non-destructive

    await runner_app.reap_stalled_subagent_dispatches(
        now=now, timeout_s=STALL_TIMEOUT_S, read_child=_read
    )
    assert len(reads) == cap, f"sweep must cap reads at {cap}, issued {len(reads)}"


@pytest.mark.asyncio
async def test_approval_parked_child_is_not_re_read_every_sweep(
    _clean_subagent_registry: None,
) -> None:
    """An approval-parked child is re-adjudicated once per budget, not every sweep."""
    entry = _dispatch_running_child()
    reads: list[str] = []

    async def _read(child_id: str) -> dict[str, Any]:
        reads.append(child_id)
        return _snapshot(status="running", pending=[{"elicitation_id": "e1"}])

    now0 = entry.last_activity_at + STALL_TIMEOUT_S + 1
    await runner_app.reap_stalled_subagent_dispatches(
        now=now0, timeout_s=STALL_TIMEOUT_S, read_child=_read
    )
    assert len(reads) == 1
    assert entry.last_activity_at == now0  # refreshed, so no longer freshly silent

    # An immediate next sweep must not re-read the parked child.
    await runner_app.reap_stalled_subagent_dispatches(
        now=now0 + 1, timeout_s=STALL_TIMEOUT_S, read_child=_read
    )
    assert len(reads) == 1

    # A whole budget later, it is adjudicated again.
    await runner_app.reap_stalled_subagent_dispatches(
        now=now0 + STALL_TIMEOUT_S + 1, timeout_s=STALL_TIMEOUT_S, read_child=_read
    )
    assert len(reads) == 2


def test_stall_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget is env-configurable with a sane default."""
    monkeypatch.delenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", raising=False)
    assert runner_app.resolve_subagent_stall_timeout_s() == 900.0

    monkeypatch.setenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", "42.5")
    assert runner_app.resolve_subagent_stall_timeout_s() == 42.5

    monkeypatch.setenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", "not-a-number")
    assert runner_app.resolve_subagent_stall_timeout_s() == 900.0
