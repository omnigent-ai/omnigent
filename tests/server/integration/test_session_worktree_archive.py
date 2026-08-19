"""
Integration tests for git worktree cleanup on session archive.

Drives ``PATCH /v1/sessions/{id}`` with ``{"archived": true}`` through
the full app with a fake host registered in ``app.state.host_registry``.
Verifies the archive flow inspects the worktree first, removes it (with
branch) only when the host reports it safe — clean tree, nothing
unpushed, branch merged — and otherwise keeps it, recording why on the
session's ``omnigent.worktree_kept`` label.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.host.frames import (
    HostHelloFrame,
    HostInspectWorktreeFrame,
    HostRemoveWorktreeFrame,
    decode_host_frame,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import WORKTREE_KEPT_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "b76c8d9e4613a95946c9134383308ac7"
_WORKTREE_PATH = "/Users/alice/myrepo-worktrees/feature-login"
_BRANCH = "feature/login"


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


class _CapturedFrames:
    """Accumulates the worktree frames the server sends to the fake host."""

    def __init__(self) -> None:
        """Initialize empty captures and the default inspect reply."""
        self.inspect: list[HostInspectWorktreeFrame] = []
        self.remove: list[HostRemoveWorktreeFrame] = []
        # Tests override fields before archiving. Default is the
        # all-clear: clean, nothing unpushed, merged.
        self.inspect_reply: dict[str, Any] = {
            "status": "ok",
            "dirty_files": 0,
            "unpushed_commits": 0,
            "merged": True,
            "default_ref": "origin/main",
            "error": None,
        }


async def _register_fake_host(app: FastAPI, db_uri: str) -> _CapturedFrames:
    """Register a fake host and start a drain that answers worktree frames.

    :param app: The app whose ``host_registry`` to register into.
    :param db_uri: DB URI so the host row (FK target) can be upserted.
    :returns: The capture object accumulating inspect/remove frames.
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
    captured = _CapturedFrames()

    async def _drain() -> None:
        """Capture worktree frames and reply with the configured results."""
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if isinstance(frame, HostInspectWorktreeFrame):
                captured.inspect.append(frame)
                fut = conn.pending_inspect_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(captured.inspect_reply)
            elif isinstance(frame, HostRemoveWorktreeFrame):
                captured.remove.append(frame)
                fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result({"status": "ok", "error": None})

    task = asyncio.create_task(_drain())
    # Stash so the caller can stop the drain on teardown.
    conn._drain_task_for_test = task  # type: ignore[attr-defined]
    return captured


def _make_worktree_conversation(db_uri: str) -> str:
    """Create a session row that looks like a server-created worktree.

    :param db_uri: DB URI for the conversation/agent stores.
    :returns: The new conversation id.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    # Unique name: built-in agent names share a global unique index, and
    # tests seeding two worktree sessions create two agents.
    agent_store.create(
        agent_id,
        name=f"test-agent-{agent_id[:8]}",
        bundle_location="test:///bundle",
    )
    conv = conv_store.create_conversation(
        agent_id=agent_id,
        host_id=_HOST_ID,
        workspace=_WORKTREE_PATH,
        git_branch=_BRANCH,
    )
    return conv.id


async def _wait_for(predicate: object, description: str) -> None:
    """Poll ``predicate`` until truthy, failing after a few seconds.

    The archive teardown runs detached (the PATCH response must not wait
    out host round-trips), so tests poll for its observable effects.

    :param predicate: Zero-arg callable returning truthy when done.
    :param description: What is being awaited, for the failure message.
    :raises AssertionError: When the condition never becomes true.
    """
    check = predicate
    for _ in range(100):
        if check():  # type: ignore[operator]
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


def _kept_label(db_uri: str, conv_id: str) -> dict[str, Any] | None:
    """Read the session's ``omnigent.worktree_kept`` label as parsed JSON.

    :param db_uri: DB URI for the conversation store.
    :param conv_id: Conversation to read.
    :returns: The parsed label value, or ``None`` when unset.
    """
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(conv_id)
    assert conv is not None
    raw = conv.labels.get(WORKTREE_KEPT_LABEL_KEY)
    # Empty string is the soft-cleared state — reads as absent.
    return json.loads(raw) if raw else None


async def test_archive_safe_worktree_inspects_then_removes(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Archiving a clean, pushed, merged worktree removes it and its branch.

    The inspect frame must fire first and carry the STORED path/branch
    (not request input); the remove frame then follows with
    ``delete_branch=True``. No kept-label is left behind.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.patch(f"/v1/sessions/{conv_id}", json={"archived": True})
    assert resp.status_code == 200

    await _wait_for(lambda: len(captured.remove) == 1, "host.remove_worktree frame")
    # Inspection happened before removal, with the stored identifiers.
    assert len(captured.inspect) == 1
    assert captured.inspect[0].worktree_path == _WORKTREE_PATH
    assert captured.inspect[0].branch == _BRANCH
    frame = captured.remove[0]
    assert frame.worktree_path == _WORKTREE_PATH
    assert frame.branch == _BRANCH
    assert frame.delete_branch is True
    # A removed worktree leaves no "kept" note.
    assert _kept_label(db_uri, conv_id) is None


async def test_archive_unsafe_worktree_is_kept_and_labeled(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Unpushed commits on the branch keep the worktree and record why.

    No remove frame may be sent — removing here would destroy commits
    that exist nowhere else. The kept-label carries the facts so the UI
    can say what would have been lost.
    """
    captured = await _register_fake_host(app, db_uri)
    captured.inspect_reply.update({"dirty_files": 0, "unpushed_commits": 2, "merged": False})
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.patch(f"/v1/sessions/{conv_id}", json={"archived": True})
    assert resp.status_code == 200

    await _wait_for(
        lambda: _kept_label(db_uri, conv_id) is not None,
        "omnigent.worktree_kept label",
    )
    assert captured.inspect != []
    assert captured.remove == []
    label = _kept_label(db_uri, conv_id)
    assert label is not None
    assert label["unpushed_commits"] == 2
    assert label["dirty_files"] == 0
    assert label["merged"] is False


async def test_archive_failed_inspection_is_kept_and_labeled(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A failed inspection keeps the worktree — 'cannot determine' is unsafe.

    The host reported the check itself failed (e.g. the worktree path is
    unreadable), so there is no all-clear to act on. The label says the
    state is unknown rather than inventing counts.
    """
    captured = await _register_fake_host(app, db_uri)
    captured.inspect_reply.clear()
    captured.inspect_reply.update(
        {"status": "failed", "error": "worktree path does not exist: /x"}
    )
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.patch(f"/v1/sessions/{conv_id}", json={"archived": True})
    assert resp.status_code == 200

    await _wait_for(
        lambda: _kept_label(db_uri, conv_id) is not None,
        "omnigent.worktree_kept label",
    )
    assert captured.remove == []
    label = _kept_label(db_uri, conv_id)
    assert label is not None
    assert label["reason"] == "unknown"


async def test_archive_offline_host_is_kept_and_labeled(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An offline host keeps the worktree — no way to verify means keep.

    No host is registered, so no inspect frame can run. The label names
    the offline host as the reason rather than reporting work at risk.
    """
    conv_id = _make_worktree_conversation(db_uri)
    # No host registered in app.state.host_registry.

    resp = await client.patch(f"/v1/sessions/{conv_id}", json={"archived": True})
    assert resp.status_code == 200

    await _wait_for(
        lambda: _kept_label(db_uri, conv_id) is not None,
        "omnigent.worktree_kept label",
    )
    label = _kept_label(db_uri, conv_id)
    assert label is not None
    assert label["reason"] == "host_offline"


async def test_archive_shared_worktree_is_kept_for_the_other_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A worktree another live session uses is never removed on archive.

    Forks reuse the source's worktree directory by default, so two
    sessions can share one worktree. Removing it when one archives would
    pull the working directory out from under the other (the delete path
    has the same hazard — #5028). The guard runs BEFORE inspection: an
    in-use worktree is kept regardless of how clean it is.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)
    # A second, still-active session in the same worktree.
    other_id = _make_worktree_conversation(db_uri)

    resp = await client.patch(f"/v1/sessions/{conv_id}", json={"archived": True})
    assert resp.status_code == 200

    await _wait_for(
        lambda: _kept_label(db_uri, conv_id) is not None,
        "omnigent.worktree_kept label",
    )
    assert captured.inspect == []
    assert captured.remove == []
    label = _kept_label(db_uri, conv_id)
    assert label is not None
    assert label["reason"] == "in_use"
    # The other session is untouched.
    assert _kept_label(db_uri, other_id) is None


async def test_archive_plain_session_sends_no_worktree_frames(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Archiving a session with no worktree sends no inspect/remove frames.

    The cleanup gate keys off ``git_branch IS NOT NULL`` (plus workspace
    and host); without it a plain archive would ask the host to inspect
    a directory that isn't a session worktree at all.
    """
    captured = await _register_fake_host(app, db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(
        agent_id, name=f"test-agent-{agent_id[:8]}", bundle_location="test:///bundle"
    )
    conv = conv_store.create_conversation(agent_id=agent_id)

    resp = await client.patch(f"/v1/sessions/{conv.id}", json={"archived": True})
    assert resp.status_code == 200

    # Give the detached archive teardown a chance to (wrongly) fire.
    await asyncio.sleep(0.5)
    assert captured.inspect == []
    assert captured.remove == []
    assert _kept_label(db_uri, conv.id) is None
