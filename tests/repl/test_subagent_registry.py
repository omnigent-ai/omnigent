"""
Unit tests for the REPL's sub-agent tree plumbing in ``omnigent.repl._repl``.

Covers the two testable seams of the ``↓`` sub-agents feature:

* :func:`_apply_child_session_event` — maps ``session.created`` /
  ``session.child_session.updated`` SSE events onto the host registry,
  filtered by the active conversation id.
* :func:`_refresh_subagent_tree` — fetches the tree via the shared SDK
  helper (``client.sessions.child_sessions_tree``, one subtree listing) and
  seeds the host registry (the deeper-level poll).

The live triggers live in the ``_render_session_event`` closure inside
``run_repl`` (not callable in isolation), so a source-inspection guard
catches the wiring being dropped — mirroring
``tests/repl/test_agent_switch_refresh.py``.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from omnigent_ui_sdk.terminal._host import TerminalHost

from omnigent.repl import _repl
from omnigent.repl._repl import _apply_child_session_event, _refresh_subagent_tree
from omnigent.server.schemas import (
    SessionChildSessionUpdatedEvent,
    SessionCreatedEvent,
)


def _host() -> TerminalHost:
    return TerminalHost(model_name="test")


def test_session_created_registers_launching_child() -> None:
    """``session.created`` for the active session registers a busy child."""
    host = _host()
    event = SessionCreatedEvent(
        type="session.created",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        agent_id="ag_reviewer",
        parent_session_id="conv_main",
    )
    handled = _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    assert handled is True
    assert host.has_active_subagents() is True
    tree = host.subagent_tree()
    assert [(n.session_id, n.parent_id) for n, _ in tree] == [("conv_c1", "conv_main")]


def test_child_updated_merges_summary_with_parent() -> None:
    """``session.child_session.updated`` upserts the child under the active
    session as parent, carrying the partial summary fields."""
    host = _host()
    event = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={
            "id": "conv_c1",
            "tool": "reviewer",
            "title": "reviewer:auth",
            "busy": True,
            "current_task_status": "in_progress",
        },
    )
    handled = _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    assert handled is True
    assert host.active_subagent_count() == 1
    _sid, label = host.subagent_menu_rows()[0]
    assert "reviewer:auth" in label
    assert "Working" in label


def test_child_event_for_other_parent_is_ignored() -> None:
    """An event whose carrier conversation_id isn't the active session is
    consumed (handled) but must NOT mutate the registry — a relayed
    grandchild event riding an ancestor stream can't pollute the tree."""
    host = _host()
    event = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_OTHER",
        child_session_id="conv_c99",
        child={"id": "conv_c99", "busy": True, "current_task_status": "in_progress"},
    )
    handled = _apply_child_session_event(event, active_conversation_id="conv_main", host=host)
    assert handled is True  # still a child event -> caller stops dispatch
    assert host.has_active_subagents() is False
    assert host.subagent_tree() == []


def test_non_child_event_is_not_handled() -> None:
    """A non-child event returns False and leaves the registry untouched, so
    inserting the hook before the generic translation can't swallow it."""
    host = _host()
    handled = _apply_child_session_event(object(), active_conversation_id="conv_main", host=host)
    assert handled is False
    assert host.has_active_subagents() is False


def test_child_terminal_status_settles_out_of_count() -> None:
    """A terminal update marks the node finished; the running count holds it
    through the linger window (debounce) and then drops it once settled."""
    host = _host()
    busy = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={
            "id": "conv_c1",
            "tool": "reviewer",
            "busy": True,
            "current_task_status": "in_progress",
        },
    )
    done = SessionChildSessionUpdatedEvent(
        type="session.child_session.updated",
        conversation_id="conv_main",
        child_session_id="conv_c1",
        child={"id": "conv_c1", "busy": False, "current_task_status": "completed"},
    )
    _apply_child_session_event(busy, active_conversation_id="conv_main", host=host)
    assert host.active_subagent_count() == 1
    _apply_child_session_event(done, active_conversation_id="conv_main", host=host)
    # Debounced: still counted right after completing, until the linger lapses.
    assert host.active_subagent_count() == 1
    host._subagents["conv_c1"].done_at = host._monotonic() - 100.0
    assert host.active_subagent_count() == 0


class _FakeSessions:
    """Minimal stand-in for ``client.sessions`` exposing ``child_sessions_tree``.

    Flattens an in-memory parent→children map breadth-first the way
    ``child_sessions_tree`` returns the ``…/descendants`` listing — each row
    tagged with its ``parent_id`` — and records every call's arguments.
    """

    def __init__(self, by_parent: dict[str, list[dict[str, Any]]]) -> None:
        self._by_parent = by_parent
        self.calls: list[tuple[str, int, int, bool]] = []

    async def child_sessions_tree(
        self,
        session_id: str,
        *,
        max_depth: int = 3,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        self.calls.append((session_id, max_depth, limit, include_archived))
        nodes: list[dict[str, Any]] = []
        frontier = [session_id]
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[str] = []
            for parent_id in frontier:
                for row in self._by_parent.get(parent_id, []):
                    nodes.append({**row, "parent_id": parent_id, "depth": depth})
                    next_frontier.append(row["id"])
            frontier = next_frontier
        return nodes


class _FakeClient:
    def __init__(self, by_parent: dict[str, list[dict[str, Any]]]) -> None:
        self.sessions = _FakeSessions(by_parent)


@pytest.mark.asyncio
async def test_refresh_subagent_tree_seeds_grandchildren_from_one_fetch() -> None:
    """One subtree fetch assembles a 2-level tree with correct parents —
    the mechanism that keeps grandchildren live (the SSE stream only carries
    the active session's direct children)."""
    client = _FakeClient(
        {
            "conv_main": [
                {
                    "id": "conv_child",
                    "tool": "coder",
                    "busy": True,
                    "current_task_status": "in_progress",
                },
            ],
            "conv_child": [
                {
                    "id": "conv_grand",
                    "tool": "reviewer",
                    "busy": True,
                    "current_task_status": "in_progress",
                },
            ],
        }
    )
    host = _host()
    await _refresh_subagent_tree(client, host, "conv_main")  # type: ignore[arg-type]

    tree = host.subagent_tree()
    assert [(n.session_id, n.parent_id, depth) for n, depth in tree] == [
        ("conv_child", "conv_main", 1),
        ("conv_grand", "conv_child", 2),
    ]
    assert host.active_subagent_count() == 2
    # The whole tree came from a single root-scoped request.
    assert client.sessions.calls == [("conv_main", _repl._MAX_SUBAGENT_TREE_DEPTH, 100, False)]


@pytest.mark.asyncio
async def test_refresh_subagent_tree_respects_depth_cap() -> None:
    """Descendants past ``max_depth`` are neither fetched nor surfaced."""
    client = _FakeClient(
        {
            "conv_main": [{"id": "c1", "busy": True, "current_task_status": "in_progress"}],
            "c1": [{"id": "c2", "busy": True, "current_task_status": "in_progress"}],
            "c2": [{"id": "c3", "busy": True, "current_task_status": "in_progress"}],
        }
    )
    host = _host()
    await _refresh_subagent_tree(client, host, "conv_main", max_depth=1)  # type: ignore[arg-type]
    ids = {n.session_id for n, _ in host.subagent_tree()}
    assert ids == {"c1"}  # only depth-1 children surfaced
    assert client.sessions.calls == [
        ("conv_main", 1, 100, False)
    ]  # the cap travels with the request


def test_run_repl_wires_subagent_plumbing() -> None:
    """Source-inspection guard: ``run_repl`` must wire the event hook, the
    inline-menu select callback (switching sessions), and the poll task.

    These triggers live in closures that can't be invoked in isolation, so a
    refactor could silently drop them while the helper unit tests above keep
    passing. Mirrors ``test_agent_switch_refresh.test_both_triggers_*``.
    """
    src = inspect.getsource(_repl.run_repl)
    assert "_apply_child_session_event(" in src, (
        "run_repl no longer applies child-session events — the state badge "
        "and ↓ menu will never populate."
    )
    assert "host.on_subagent_select = _open_subagent_by_id" in src, (
        "the inline ↓ menu select callback was dropped — Enter would no "
        "longer switch into the chosen sub-agent."
    )
    assert "host.active_session_id_getter" in src, (
        "the active-session getter is no longer wired — Left-arrow can't "
        "tell when the user is inside a sub-agent, so back-to-top breaks."
    )
    assert "session.view_session(" in src, (
        "the select callback must use view_session (read-only re-point), NOT "
        "switch_to_session — moving the runner binding into a sub-agent "
        "orphans the parent and the sub-agent's result is never delivered."
    )
    assert "interactive=interactive" in src and "is_subagent_chattable" in src, (
        "the select callback dropped interactive-child mode — Enter on a "
        "chattable child can no longer co-drive (chat with) it."
    )
    assert "_subagent_poll_loop" in src, (
        "the background tree poll (deeper levels) is no longer started"
    )
    assert "_sync_subagent_root" in src and "_readonly_view" in src, (
        "the root-tracking dropped its read-only-view source of truth — the "
        "root could go stale vs the live main session, making '← back' show "
        "on the top-level session."
    )
    assert "_interactive_child" in src, (
        "the root-tracking no longer guards on _interactive_child — co-driving "
        "a child could re-root the selector onto it, breaking Left-arrow back."
    )
    assert "polled_root" in src and "has_active_subagents" in src, (
        "the discovery poll regressed — a resumed / switched session with "
        "existing children won't repopulate the selector without fresh SSE, "
        "or the poll no longer gates on active work and spins forever idle."
    )
