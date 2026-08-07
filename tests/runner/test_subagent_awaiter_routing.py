"""Sub-agent completions route to the AWAITER, not always the structural parent.

The runner's work registry couples two roles that agent teams must separate:
who *spawned* a session (``parent_session_id``, the tree topology) and who
*awaits* this turn's completion (the inbox owner that gets woken). For a normal
child send the two are the same session. For an agent-team PEER send — teammate
B messaging teammate C, whose structural parent is the lead A — the sender B
awaits the result even though C's parent stays A.

These tests exercise the registry directly (plain dicts, no HTTP): they assert
the ``awaiter`` property, that the completion payload lands in the awaiter's
inbox, and that the by-parent grouping used for stranded-wake recovery is keyed
by the awaiter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from omnigent.runner import app as runner_app

LEAD_SESSION_ID = "conv_lead"
MEMBER_B_SESSION_ID = "conv_member_b"
MEMBER_C_SESSION_ID = "conv_member_c"


@pytest.fixture
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox maps.

    The registry lives in module-level dicts on ``omnigent.runner.app`` that
    otherwise leak across tests. Clear them before the test and restore the
    originals after.
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


def test_awaiter_defaults_to_structural_parent() -> None:
    """A child send (no awaiter override) delivers to the structural parent.

    This is the unchanged status quo: if ``awaiter`` stopped falling back to
    ``parent_session_id`` every existing child-send completion would misroute.
    """
    entry = runner_app._SubagentWorkEntry(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        work_id="w1",
        agent="reviewer",
        title="review",
    )
    assert entry.awaiter == LEAD_SESSION_ID


def test_awaiter_is_sender_for_peer_send() -> None:
    """A peer send routes completion to the sender, not the structural parent."""
    entry = runner_app._SubagentWorkEntry(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        work_id="w2",
        agent="reviewer",
        title="review",
        awaiter_session_id=MEMBER_B_SESSION_ID,
    )
    assert entry.awaiter == MEMBER_B_SESSION_ID


@pytest.mark.usefixtures("_clean_subagent_registry")
def test_peer_completion_delivers_to_sender_inbox() -> None:
    """The terminal payload for a peer send lands in the SENDER's inbox.

    B messages C (whose parent is the lead A). Only B's inbox should receive
    the completion; A's must stay empty. A regression that delivered to the
    structural parent would strand B (it never wakes) and spam A.
    """
    b_inbox: asyncio.Queue = asyncio.Queue()
    a_inbox: asyncio.Queue = asyncio.Queue()
    runner_app._session_inboxes_ref[MEMBER_B_SESSION_ID] = b_inbox
    runner_app._session_inboxes_ref[LEAD_SESSION_ID] = a_inbox

    runner_app.register_subagent_work(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        agent="reviewer",
        title="review",
        awaiter_session_id=MEMBER_B_SESSION_ID,
    )

    ack = runner_app.mark_subagent_work_terminal(
        MEMBER_C_SESSION_ID,
        status="completed",
        output="done",
    )
    assert ack.delivered_now is True

    # Sender got the payload; the structural parent did not.
    assert b_inbox.qsize() == 1
    assert a_inbox.qsize() == 0
    payload = b_inbox.get_nowait()
    assert payload["conversation_id"] == MEMBER_C_SESSION_ID
    assert payload["status"] == "completed"
    assert payload["output"] == "done"


@pytest.mark.usefixtures("_clean_subagent_registry")
def test_peer_work_grouped_under_awaiter_for_wake_recovery() -> None:
    """``list_subagent_work`` finds peer work under the awaiter, not the parent.

    Stranded-wake recovery queries the idled session's work via the by-parent
    grouping. For a peer send that idled session is the sender, so the grouping
    must be keyed by the awaiter.
    """
    runner_app.register_subagent_work(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        agent="reviewer",
        title="review",
        awaiter_session_id=MEMBER_B_SESSION_ID,
    )

    # Grouped under the sender (awaiter), not the structural parent.
    b_work = runner_app.list_subagent_work(MEMBER_B_SESSION_ID)
    assert [e.child_session_id for e in b_work] == [MEMBER_C_SESSION_ID]
    assert runner_app.list_subagent_work(LEAD_SESSION_ID) == []

    # Cleanup drops it from the awaiter's group.
    runner_app.unregister_subagent_work(MEMBER_C_SESSION_ID)
    assert runner_app.list_subagent_work(MEMBER_B_SESSION_ID) == []


@pytest.mark.usefixtures("_clean_subagent_registry")
def test_deleting_structural_parent_clears_peer_work() -> None:
    """Deleting the LEAD clears peer work whose awaiter is a teammate.

    The by-parent index is keyed by awaiter, so peer work registered by B
    against C lives under B even though C's structural parent is the lead A.
    Deleting A must still drop C's entry: leaving it behind strands runner
    state that references a session that no longer exists, and a later
    completion for C would look up a dead parent.
    """
    runner_app.register_subagent_work(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        agent="reviewer",
        title="review",
        awaiter_session_id=MEMBER_B_SESSION_ID,
    )

    runner_app.unregister_subagent_work_for_session(LEAD_SESSION_ID)

    assert MEMBER_C_SESSION_ID not in runner_app._subagent_work_by_child
    assert runner_app.list_subagent_work(MEMBER_B_SESSION_ID) == []


@pytest.mark.usefixtures("_clean_subagent_registry")
def test_deleting_awaiter_clears_peer_work() -> None:
    """Deleting the SENDER clears the peer work it was awaiting.

    Companion to the lead-deletion case: cleanup by awaiter already worked, so
    this pins it against a regression while the parent path is fixed.
    """
    runner_app.register_subagent_work(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        agent="reviewer",
        title="review",
        awaiter_session_id=MEMBER_B_SESSION_ID,
    )

    runner_app.unregister_subagent_work_for_session(MEMBER_B_SESSION_ID)

    assert MEMBER_C_SESSION_ID not in runner_app._subagent_work_by_child
    assert runner_app.list_subagent_work(MEMBER_B_SESSION_ID) == []


@pytest.mark.usefixtures("_clean_subagent_registry")
def test_child_send_cleanup_by_parent_still_works() -> None:
    """A plain child send is still cleaned up when its parent is deleted.

    Regression guard for the non-team path: with no awaiter override the
    awaiter IS the parent, so deleting the parent must drop the child.
    """
    runner_app.register_subagent_work(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        agent="reviewer",
        title="review",
    )

    runner_app.unregister_subagent_work_for_session(LEAD_SESSION_ID)

    assert MEMBER_C_SESSION_ID not in runner_app._subagent_work_by_child
    assert runner_app.list_subagent_work(LEAD_SESSION_ID) == []


@pytest.mark.usefixtures("_clean_subagent_registry")
def test_re_register_moves_work_to_new_awaiter() -> None:
    """Re-sending the same target under a new awaiter leaves no stale grouping.

    A child send from the lead followed by a peer send from B targets the same
    session twice. The second registration must move the grouping to B and
    leave nothing behind under the lead, or stranded-wake recovery would find
    the same work under two owners.
    """
    runner_app.register_subagent_work(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        agent="reviewer",
        title="first",
    )
    runner_app.register_subagent_work(
        parent_session_id=LEAD_SESSION_ID,
        child_session_id=MEMBER_C_SESSION_ID,
        agent="reviewer",
        title="second",
        awaiter_session_id=MEMBER_B_SESSION_ID,
    )

    assert [e.child_session_id for e in runner_app.list_subagent_work(MEMBER_B_SESSION_ID)] == [
        MEMBER_C_SESSION_ID
    ]
    assert runner_app.list_subagent_work(LEAD_SESSION_ID) == []
