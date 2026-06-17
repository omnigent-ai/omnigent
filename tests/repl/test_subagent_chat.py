"""Unit tests for REPL sub-agent CHAT (web-UI parity).

Covers the interactive co-drive feature layered on the ``↓`` selector:

* Interactive-child send co-drives the CHILD session (POST to its own runner)
  WITHOUT moving the parent's runner binding and WITHOUT ``switch_to_session``.
* The interactive flag is tracked SEPARATELY from ``_readonly_view`` so the
  selector root stays frozen — Left-arrow still returns to the parent after a
  chat, and the plain-send guard only refuses on a non-chattable (closed) view.
* A closed child is not chattable (a ``message`` to it 409s) and stays
  view-only.
* A ``RUNNER_UNAVAILABLE`` POST surfaces as an error rather than hanging.
* Discovery polling repopulates the selector for a resumed / switched session
  that already has children, even with no fresh SSE.

The adapter tests drive the REAL ``_SessionsChatReplAdapter`` against typed
client/session stubs (no HTTP), so the full ``view_session`` / ``send`` path is
exercised, not a re-implementation of it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from omnigent_client import OmnigentError
from omnigent_client._sessions import Session as SessionSnapshot
from omnigent_client._sessions import SessionsNamespace
from omnigent_ui_sdk.terminal._host import TerminalHost

from omnigent.repl._repl import (
    _refresh_subagent_tree,
    _SessionsChatReplAdapter,
    _should_discover_subagents,
)
from omnigent.server.schemas import SessionStatusEvent
from omnigent.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

# ── Host-level: chattability + closed status (B / F2 / F5) ─────────────────


def test_closed_child_is_not_chattable() -> None:
    """A child whose session is closed (label-marked) is view-only — the
    selector reports it as not chattable so Enter dives read-only and a typed
    message is refused (a ``message`` to a closed session 409s)."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_closed",
        parent_id="conv_main",
        child={
            "id": "conv_closed",
            "tool": "reviewer",
            "busy": False,
            "current_task_status": "completed",
            "labels": {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
        },
    )
    assert host.is_subagent_chattable("conv_closed") is False
    # An open (warm) child IS chattable.
    host.upsert_subagent(
        "conv_open",
        parent_id="conv_main",
        child={"id": "conv_open", "tool": "coder", "busy": False, "current_task_status": None},
    )
    assert host.is_subagent_chattable("conv_open") is True
    # Unknown ids (e.g. the "main" row / a stale selection) are not chattable.
    assert host.is_subagent_chattable("conv_unknown") is False


def test_closed_marker_is_sticky_across_partial_updates() -> None:
    """Once closed, a child stays closed even if a later partial delta omits
    the labels — a closed session never reopens."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_c1",
        parent_id="conv_main",
        child={"id": "conv_c1", "labels": {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}},
    )
    assert host.is_subagent_chattable("conv_c1") is False
    # A status-only delta with no labels must not flip it back to open.
    host.upsert_subagent("conv_c1", child={"current_task_status": "completed"})
    assert host.is_subagent_chattable("conv_c1") is False


def test_status_label_last_task_error_outranks_completed() -> None:
    """``last_task_error`` reads ``Failed`` even when a stale ``completed``
    status lingers — mirroring the web ``childStatus`` precedence."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_c1",
        parent_id="conv_main",
        child={
            "id": "conv_c1",
            "busy": False,
            "current_task_status": "completed",
            "last_task_error": {"code": "boom", "message": "kaboom"},
        },
    )
    node = host._subagents["conv_c1"]
    assert host._subagent_status_label(node) == "Failed"


def test_status_label_pending_outranks_busy_and_warm_idle_not_done() -> None:
    """Pending elicitations outrank ``busy`` (Needs response); a warm-idle
    child reads ``Idle`` (not ``Done``)."""
    host = TerminalHost(model_name="test")
    host.upsert_subagent(
        "conv_busy",
        parent_id="conv_main",
        child={"id": "conv_busy", "busy": True, "pending_elicitations_count": 2},
    )
    host.upsert_subagent(
        "conv_warm",
        parent_id="conv_main",
        child={"id": "conv_warm", "busy": False, "current_task_status": None},
    )
    assert host._subagent_status_label(host._subagents["conv_busy"]) == "Needs response"
    assert host._subagent_status_label(host._subagents["conv_warm"]) == "Idle"


# ── Poll discovery gate (D / F7) ───────────────────────────────────────────


def test_should_discover_polls_while_nodes_exist() -> None:
    """Polls while any node exists (keep statuses + nested levels live)."""
    assert _should_discover_subagents(
        "conv_main", has_any_subagents=True, last_polled_root="conv_main"
    )


def test_should_discover_runs_discovery_on_root_change() -> None:
    """A resumed / switched root with NO nodes yet still polls once — the
    discovery that repopulates the selector without fresh SSE (the D fix)."""
    # Root just changed (never polled) -> discover even with zero nodes.
    assert _should_discover_subagents(
        "conv_resumed", has_any_subagents=False, last_polled_root=None
    )
    assert _should_discover_subagents(
        "conv_new", has_any_subagents=False, last_polled_root="conv_old"
    )
    # Same root already discovered + no nodes -> stop polling (SSE re-triggers).
    assert not _should_discover_subagents(
        "conv_new", has_any_subagents=False, last_polled_root="conv_new"
    )
    # No root -> never poll.
    assert not _should_discover_subagents(None, has_any_subagents=True, last_polled_root=None)


class _DiscoverySessions:
    """``client.sessions`` stub serving a fixed child-session snapshot."""

    def __init__(self, by_parent: dict[str, list[dict[str, Any]]]) -> None:
        self._by_parent = by_parent

    async def child_sessions(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._by_parent.get(session_id, []))

    async def child_sessions_tree(
        self, session_id: str, *, max_depth: int = 3, limit: int = 100
    ) -> list[dict[str, Any]]:
        # The recursion now lives in the SDK; reuse the real implementation
        # (it only needs this stub's ``child_sessions``).
        return await SessionsNamespace.child_sessions_tree(
            self, session_id, max_depth=max_depth, limit=limit
        )


class _DiscoveryClient:
    def __init__(self, by_parent: dict[str, list[dict[str, Any]]]) -> None:
        self.sessions = _DiscoverySessions(by_parent)


@pytest.mark.asyncio
async def test_resumed_session_with_children_repopulates_selector() -> None:
    """A freshly-attached host (empty registry) re-fetches the tree for a
    resumed root that already has children and populates the selector — the
    discovery poll, exercised end-to-end through ``_refresh_subagent_tree``."""
    client = _DiscoveryClient(
        {
            "conv_resumed": [
                {"id": "conv_child", "tool": "coder", "busy": True, "current_task_status": None},
            ],
        }
    )
    host = TerminalHost(model_name="test")
    assert host.has_any_subagents() is False  # nothing seeded yet (no SSE)

    await _refresh_subagent_tree(client, host, "conv_resumed")  # type: ignore[arg-type]

    assert host.has_any_subagents() is True
    assert [n.session_id for n, _ in host.subagent_tree()] == ["conv_child"]


def test_seed_generation_guard_drops_stale_snapshot() -> None:
    """A seed carrying a stale generation (the tree was cleared mid-fetch)
    no-ops instead of resurrecting cleared nodes onto the new root."""
    host = TerminalHost(model_name="test")
    gen = host.subagent_generation
    # Simulate a clear-during-poll: the fetch captured ``gen`` before the
    # clear bumped the generation.
    host.clear_subagents()
    host.seed_subagent_tree(
        "conv_old",
        [{"id": "conv_stale", "parent_id": "conv_old", "busy": True}],
        generation=gen,
    )
    assert host.has_any_subagents() is False  # stale seed was dropped
    assert host._subagent_root is None  # root not re-pointed to the old root


# ── Adapter-level: interactive co-drive (C / D / F3 / F4 / F5 / F6) ─────────


def _snapshot(session_id: str, status: str = "idle") -> SessionSnapshot:
    """A minimal SDK session snapshot for ``view_session`` hydration."""
    return SessionSnapshot(
        id=session_id,
        agent_id="ag_x",
        status=status,
        created_at=0,
    )


class _ChatSessions:
    """``client.sessions`` stub for the interactive co-drive path.

    Records ``post_event`` / ``bind_runner`` calls so a test can prove a send
    targets the CHILD and never PATCHes the runner. ``stream`` yields a single
    terminal ``idle`` status once a turn has been posted, so ``send`` completes
    deterministically (no 1 s snapshot fallback) without spin-reconnecting.
    """

    def __init__(self, *, post_error: Exception | None = None) -> None:
        self.post_event_calls: list[tuple[str, dict[str, Any]]] = []
        self.bind_runner_calls: list[str] = []
        self.get_calls: list[str] = []
        self._post_error = post_error
        self._posted = asyncio.Event()
        self._idle_delivered = asyncio.Event()

    async def get(self, session_id: str) -> SessionSnapshot:
        self.get_calls.append(session_id)
        return _snapshot(session_id)

    async def post_event(self, session_id: str, event: dict[str, Any]) -> None:
        self.post_event_calls.append((session_id, event))
        if self._post_error is not None:
            raise self._post_error
        self._posted.set()

    async def bind_runner(self, session_id: str, *, runner_id: str) -> SessionSnapshot:
        self.bind_runner_calls.append(session_id)
        return _snapshot(session_id)

    async def stream(self, session_id: str):  # type: ignore[no-untyped-def]
        # Block until a turn is posted, then emit one terminal idle status and
        # park (so the pump doesn't reconnect-spin) until cancelled.
        await self._posted.wait()
        if not self._idle_delivered.is_set():
            self._idle_delivered.set()
            yield SessionStatusEvent(
                type="session.status", conversation_id=session_id, status="idle"
            )
        await asyncio.Event().wait()


class _ChatClient:
    def __init__(self, **kw: Any) -> None:
        self.sessions = _ChatSessions(**kw)


def _make_chat_adapter(client: _ChatClient) -> _SessionsChatReplAdapter:
    """Adapter pre-attached to a parent session, with a runner_id set so a
    stray bind would be observable (it must NOT fire while co-driving)."""
    return _SessionsChatReplAdapter(
        client,  # type: ignore[arg-type] — duck-typed stub
        "nessie",
        session_id="conv_parent",
        runner_id="runner_parent",
    )


@pytest.mark.asyncio
async def test_interactive_child_send_codrives_child_without_rebinding() -> None:
    """Co-driving a child: the send POSTs to the CHILD session id and NEVER
    PATCHes the runner binding (no ``bind_runner``), exactly like the web UI.
    ``switch_to_session`` is never involved."""
    client = _ChatClient()
    session = _make_chat_adapter(client)
    try:
        await session.view_session("conv_child", read_only=True, interactive=True)
        assert session._session_id == "conv_child"
        assert session._readonly_view is True  # binding still suppressed
        assert session._interactive_child is True  # but sends are enabled

        async for _ in session.send("hello child"):
            pass

        # The message went to the CHILD, not the parent.
        assert len(client.sessions.post_event_calls) == 1
        target, payload = client.sessions.post_event_calls[0]
        assert target == "conv_child"
        assert payload["type"] == "message"
        assert payload["data"]["role"] == "user"
        # Co-drive parity: the runner binding was never moved onto the child.
        assert client.sessions.bind_runner_calls == []
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_return_to_root_clears_interactive_and_restores_binding() -> None:
    """Left-arrow back to the root re-points with ``read_only=False`` /
    ``interactive=False``, clearing both flags so the root is owned again — the
    invariant that keeps the original root reachable after a chat (F4)."""
    client = _ChatClient()
    session = _make_chat_adapter(client)
    try:
        await session.view_session("conv_child", read_only=True, interactive=True)
        assert session._interactive_child is True

        # Return to the top-level (root) session.
        await session.view_session("conv_parent", read_only=False)
        assert session._session_id == "conv_parent"
        assert session._readonly_view is False
        assert session._interactive_child is False
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_send_guard_refuses_closed_view_but_allows_interactive() -> None:
    """The plain-send guard (``_readonly_view and not _interactive_child``)
    refuses on a read-only (closed-child) view but allows interactive co-drive.
    This is the exact condition the REPL's ``on_input`` evaluates (F5)."""
    client = _ChatClient()
    session = _make_chat_adapter(client)
    try:
        # Closed / non-chattable child: read-only, not interactive -> refuse.
        await session.view_session("conv_closed", read_only=True, interactive=False)
        refuse = session._readonly_view and not session._interactive_child
        assert refuse is True

        # Open child: interactive -> allow.
        await session.view_session("conv_open", read_only=True, interactive=True)
        refuse = session._readonly_view and not session._interactive_child
        assert refuse is False
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_interactive_send_runner_unavailable_surfaces_not_hangs() -> None:
    """A ``RUNNER_UNAVAILABLE`` POST raises out of ``send`` promptly (the REPL
    renders it as an inline error) rather than hanging on the turn-done wait."""
    client = _ChatClient(post_error=OmnigentError("runner unavailable", code="runner_unavailable"))
    session = _make_chat_adapter(client)
    try:
        await session.view_session("conv_child", read_only=True, interactive=True)

        async def _drive() -> None:
            async for _ in session.send("hello"):
                pass

        with pytest.raises(OmnigentError):
            # Tight timeout: a hang (waiting forever for a turn that never
            # starts) would trip this instead of the expected raise.
            await asyncio.wait_for(_drive(), timeout=5.0)
        # No bind attempted on the unavailable runner.
        assert client.sessions.bind_runner_calls == []
    finally:
        await session.aclose()
