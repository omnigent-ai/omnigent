"""
Integration tests for opt-in git worktree cleanup on session delete.

Drives ``DELETE /v1/sessions/{id}`` through the full app with a fake
host registered in ``app.state.host_registry``. Verifies the
``?delete_branch`` flag gates whether a ``host.remove_worktree`` frame
is sent, and that the stored worktree path + branch (not request input)
are used. See designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from omnigent.host.frames import (
    HostHelloFrame,
    HostRemoveWorktreeFrame,
    decode_host_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_wt_delete"


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


async def _register_fake_host(
    app: FastAPI,
    db_uri: str,
) -> list[HostRemoveWorktreeFrame]:
    """Register a fake host and start a drain that captures remove frames.

    :param app: The app whose ``host_registry`` to register into.
    :param db_uri: DB URI so the host row (FK target) can be upserted.
    :returns: A list that accumulates every ``HostRemoveWorktreeFrame``
        the server sends to this host.
    """
    # Upsert the host row so the conversation's host_id FK resolves.
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)
    registry = app.state.host_registry
    conn = registry.register(
        host_id=_HOST_ID,
        ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
        owner=RESERVED_USER_LOCAL,
    )
    captured: list[HostRemoveWorktreeFrame] = []

    async def _drain() -> None:
        """Capture remove-worktree frames and reply ok."""
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if isinstance(frame, HostRemoveWorktreeFrame):
                captured.append(frame)
                fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result({"status": "ok", "error": None})

    task = asyncio.create_task(_drain())
    # Stash so the caller can stop the drain on teardown.
    conn._drain_task_for_test = task  # type: ignore[attr-defined]
    return captured


def _make_worktree_conversation(db_uri: str) -> str:
    """Create a session row that looks like a server-created worktree.

    :param db_uri: DB URI for the conversation store.
    :returns: The new conversation id.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id=_HOST_ID,
        workspace="/Users/alice/myrepo-worktrees/feature-login",
        git_branch="feature/login",
    )
    return conv.id


async def test_delete_with_flag_sends_remove_worktree(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a worktree session sends a
    host.remove_worktree frame carrying the stored path + branch.

    If no frame is captured, the delete-flow gate or the proxy call
    is broken and the user's checkbox would silently do nothing. The
    path/branch assertions prove the server uses the *stored* values
    (not request input), which is the multi-user-safe contract.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert resp.status_code == 200

    # Exactly one remove frame, with the stored worktree path/branch
    # and delete_branch=True (the box was checked).
    assert len(captured) == 1, (
        f"Expected exactly one host.remove_worktree frame, got {len(captured)}. "
        "0 means the delete-flow cleanup gate didn't fire; >1 means it fired twice."
    )
    frame = captured[0]
    assert frame.worktree_path == "/Users/alice/myrepo-worktrees/feature-login"
    assert frame.branch == "feature/login"
    assert frame.delete_branch is True


async def test_delete_without_flag_sends_no_remove_worktree(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Deleting a worktree session WITHOUT the flag leaves the worktree
    alone — no host.remove_worktree frame is sent.

    If a frame is captured here, the cleanup is happening
    unconditionally and would destroy worktrees/branches the user
    never asked to remove.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}")
    assert resp.status_code == 200
    # Default is delete_branch=false → no cleanup.
    assert captured == []


async def test_delete_non_worktree_session_ignores_flag(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a session with no worktree
    (``git_branch`` NULL) is a no-op — no remove frame.

    The gate keys off ``git_branch IS NOT NULL``; without that check a
    plain session delete would try to remove a worktree that doesn't
    exist.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    # Plain session: no host, no workspace, no git_branch.
    conv = conv_store.create_conversation(agent_id=None)

    resp = await client.delete(f"/v1/sessions/{conv.id}?delete_branch=true")
    assert resp.status_code == 200
    assert captured == []
