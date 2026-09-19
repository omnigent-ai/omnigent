"""A started sub-agent that goes silent must fail loudly, not hang forever.

``reap_stalled_subagent_launches`` only bounds a child wedged *before* its
first edge. Once a child reaches ``running``/``waiting`` nothing else bounded
its lifetime, so any harness drift that lost the child's terminal edge
presented identically: the work handle stayed ``running`` forever and the
parent's inbox stayed empty with no error surfaced anywhere.

The stall sweep is the backstop. It is keyed on the absence of runner-visible
edges, never on wall-clock since dispatch, so a healthy long-running child
that is still emitting activity is not killed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
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
    """Server-client stub that records the parent wake POSTs it receives."""

    def __init__(self) -> None:
        self.wake_posts: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Record ``/events`` POSTs so parent wakes can be counted.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_parent/events"``.
        :param kwargs: Request keyword arguments; ``json`` carries the body.
        :returns: Stub 200 response.
        """
        if url.endswith("/events"):
            self.wake_posts.append(url)
        return self._Response()


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
async def test_silent_running_child_is_failed_and_wakes_parent_exactly_once(
    _clean_subagent_registry: None,
) -> None:
    """A child with no edge inside the budget is failed and wakes the parent once."""
    server = _WakeRecordingServerClient()
    app = create_runner_app(server_client=server)  # type: ignore[arg-type]
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()

    reaped = runner_app.reap_stalled_subagent_dispatches(
        now=entry.last_activity_at + STALL_TIMEOUT_S + 1,
        timeout_s=STALL_TIMEOUT_S,
        mark_terminal=app.state.mark_subagent_terminal_and_wake,
    )

    assert [e.child_session_id for e in reaped] == [CHILD_SESSION_ID]
    assert entry.status == "failed"
    payload = inbox.get_nowait()
    assert payload["conversation_id"] == CHILD_SESSION_ID
    assert payload["status"] == "failed"
    assert "no activity" in payload["output"]

    # A second sweep must not re-deliver: the entry is already terminal.
    assert (
        runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S * 10,
            timeout_s=STALL_TIMEOUT_S,
            mark_terminal=app.state.mark_subagent_terminal_and_wake,
        )
        == []
    )
    assert inbox.empty()

    await asyncio.sleep(0)
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
    moment ago.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
    entry = _dispatch_running_child()
    dispatched_at = entry.created_at
    entry.created_at = dispatched_at - STALL_TIMEOUT_S * 5

    # The child reports in through the runner's child-event funnel.
    runner_app.note_subagent_activity(CHILD_SESSION_ID)

    assert (
        runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + STALL_TIMEOUT_S - 1,
            timeout_s=STALL_TIMEOUT_S,
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
        runner_app.reap_stalled_subagent_dispatches(
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
        runner_app.reap_stalled_subagent_dispatches(
            now=entry.last_activity_at + 10_000, timeout_s=STALL_TIMEOUT_S
        )
        == []
    )
    assert entry.status == "launching"


def test_stall_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget is env-configurable with a sane default."""
    monkeypatch.delenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", raising=False)
    assert runner_app.resolve_subagent_stall_timeout_s() == 900.0

    monkeypatch.setenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", "42.5")
    assert runner_app.resolve_subagent_stall_timeout_s() == 42.5

    monkeypatch.setenv("OMNIGENT_SUBAGENT_STALL_TIMEOUT_S", "not-a-number")
    assert runner_app.resolve_subagent_stall_timeout_s() == 900.0
