"""A started sub-agent that goes silent must be adjudicated, not blindly failed.

``reap_stalled_subagent_launches`` only bounds a child wedged *before* its
first edge. Once a child reaches ``running`` nothing else bounded its lifetime,
so any harness drift that lost the child's terminal edge presented identically:
the work handle stayed ``running`` forever and the parent's inbox stayed empty
with no error surfaced anywhere.

The stall sweep is the backstop. It is keyed on the absence of runner-visible
edges, never on wall-clock since dispatch, so a healthy long-running child that
is still emitting activity is not killed. Silence past the budget is only a
*candidate*: the child is then adjudicated against its authoritative
server-side status before anything is delivered, so a native-approval wait, a
completion the runner merely missed, and an unreadable server are each handled
without losing a result — and a genuinely-stuck child is interrupted and given
a *provisional* failure that a real terminal edge can still supersede.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app, pending_approvals, tool_dispatch
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_worker"
STALL_TIMEOUT_S = 900.0


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


def _verdict(value: str) -> Any:
    """Return an async ``classify`` stub that always answers *value*."""

    async def _classify(entry: runner_app._SubagentWorkEntry) -> str:
        del entry
        return value

    return _classify


def _recording_interrupt(sink: list[str]) -> Any:
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
        classify=_verdict(runner_app._SILENT_CHILD_STUCK),
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
            classify=_verdict(runner_app._SILENT_CHILD_STUCK),
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

    async def _classify_must_not_run(entry: runner_app._SubagentWorkEntry) -> str:
        raise AssertionError("a child still emitting activity must not be adjudicated")

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S - 1,
            timeout_s=STALL_TIMEOUT_S,
            classify=_classify_must_not_run,
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
            now=entry.last_activity_at + 10_000, timeout_s=0
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
            classify=_verdict(runner_app._SILENT_CHILD_STUCK),
        )
        == []
    )
    assert entry.status == "launching"


@pytest.mark.asyncio
async def test_recovered_waiting_child_is_left_to_reconciliation(
    _clean_subagent_registry: None,
) -> None:
    """A restart-recovered ``waiting`` dispatch is the reconcile loop's business.

    Such an entry awaits *remote* completion and has no local edges by
    construction, so the local-silence sweep must not adjudicate or fail it.
    """
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
            classify=_verdict(runner_app._SILENT_CHILD_STUCK),
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

    async def _classify_must_not_run(entry: runner_app._SubagentWorkEntry) -> str:
        raise AssertionError("a locally-approval-parked child must not be adjudicated")

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            classify=_classify_must_not_run,
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

    Native prompts live only in the server-side elicitation index, so the
    verdict comes from the authoritative snapshot, not runner-local state.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()
    interrupts: list[str] = []

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            classify=_verdict(runner_app._SILENT_CHILD_APPROVAL),
            interrupt=_recording_interrupt(interrupts),
        )
        == []
    )
    assert entry.status == "running"
    assert not entry.stalled
    assert interrupts == []
    assert inbox.empty()


@pytest.mark.asyncio
async def test_server_terminal_child_is_demoted_to_waiting_not_failed(
    _clean_subagent_registry: None,
) -> None:
    """The server already holds the real result: hand it to reconciliation.

    The runner merely missed the terminal edge, so the sweep must not
    synthesize a failure over the server's authoritative completion; it demotes
    the entry to ``waiting`` for the reconcile pass to deliver.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    assert (
        await runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            classify=_verdict(runner_app._SILENT_CHILD_TERMINAL),
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
            classify=_verdict(runner_app._SILENT_CHILD_UNKNOWN),
        )
        == []
    )
    assert entry.status == "running"
    assert not entry.stalled
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
        classify=_verdict(runner_app._SILENT_CHILD_STUCK),
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
        classify=_verdict(runner_app._SILENT_CHILD_STUCK),
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


def test_stall_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget is env-configurable with a sane default."""
    monkeypatch.delenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", raising=False)
    assert runner_app.resolve_subagent_stall_timeout_s() == 900.0

    monkeypatch.setenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", "42.5")
    assert runner_app.resolve_subagent_stall_timeout_s() == 42.5

    monkeypatch.setenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", "not-a-number")
    assert runner_app.resolve_subagent_stall_timeout_s() == 900.0
