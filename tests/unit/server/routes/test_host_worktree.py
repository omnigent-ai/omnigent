"""
Tests for ``omnigent.server.routes._host_worktree``.

Drives the create/remove worktree proxies with a fake host that
auto-replies to the outbound frames — verifies the request_id/future
plumbing, success unpacking, failure surfacing, and offline handling
without a live host process. Mirrors ``test_workspace_validation.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostHelloFrame,
    HostRemoveWorktreeFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_worktree import (
    WorktreeHostUnavailableError,
    WorktreeProxyError,
    create_worktree_on_host,
    remove_worktree_on_host,
)

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_wt_test"


class _FakeWebSocket:
    """Minimal WebSocket stand-in capturing outbound frames."""

    def __init__(self) -> None:
        """Initialize with an empty outbound capture."""
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        """Capture an outbound frame.

        :param data: JSON-encoded frame text.
        """
        self.sent.append(data)


def _hello_frame() -> HostHelloFrame:
    """Construct a hello frame for registry registration.

    :returns: Hello frame with default version + empty runners.
    """
    return HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host")


@pytest.fixture()
async def host_setup() -> AsyncIterator[HostRegistry]:
    """Register a host plus a background auto-replier for worktree frames.

    Tests set ``registry._create_reply_for_test`` /
    ``registry._remove_reply_for_test`` before calling the proxy; the
    drain task resolves the matching pending future with that reply,
    mimicking ``host_tunnel.py``'s receive loop.

    :returns: Async iterator yielding the registry.
    """
    registry = HostRegistry()
    ws = _FakeWebSocket()
    conn = registry.register(
        host_id=_HOST_ID,
        ws=ws,  # type: ignore[arg-type] — duck-typed
        hello=_hello_frame(),
        owner=None,
    )

    create_reply: dict[str, Any] = {}
    remove_reply: dict[str, Any] = {}
    # Frames the proxy sent, captured here (registry.send_text enqueues
    # to outbound_queue rather than ws.send_text, so we record from the
    # drain task instead of from the fake ws).
    sent_frames: list[Any] = []

    async def _drain() -> None:
        """Read outbound frames, record them, and resolve the future."""
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            sent_frames.append(frame)
            if isinstance(frame, HostCreateWorktreeFrame):
                fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(create_reply)
            elif isinstance(frame, HostRemoveWorktreeFrame):
                fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(remove_reply)

    drain_task = asyncio.create_task(_drain())
    registry._create_reply_for_test = create_reply  # type: ignore[attr-defined]
    registry._remove_reply_for_test = remove_reply  # type: ignore[attr-defined]
    registry._sent_frames_for_test = sent_frames  # type: ignore[attr-defined]

    try:
        yield registry
    finally:
        conn.outbound_queue.put_nowait(None)
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


async def test_create_worktree_success_returns_path_and_branch(
    host_setup: HostRegistry,
) -> None:
    """A successful host reply is unpacked into the worktree path + branch."""
    registry = host_setup
    registry._create_reply_for_test.update(  # type: ignore[attr-defined]
        {
            "status": "ok",
            "worktree_path": "/Users/alice/myrepo-worktrees/feature-login",
            "branch": "feature/login",
            "error": None,
        }
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    result = await create_worktree_on_host(
        host_registry=registry,
        host_conn=conn,
        repo_path="/Users/alice/myrepo",
        branch_name="feature/login",
        base_branch="main",
    )
    # Both fields must thread through; a regression in the result
    # routing or unpacking would drop one and break session create.
    assert result.worktree_path == "/Users/alice/myrepo-worktrees/feature-login"
    assert result.branch == "feature/login"
    # The frame the proxy actually sent carries the request params.
    sent = registry._sent_frames_for_test[-1]  # type: ignore[attr-defined]
    assert isinstance(sent, HostCreateWorktreeFrame)
    assert sent.repo_path == "/Users/alice/myrepo"
    assert sent.branch_name == "feature/login"
    assert sent.base_branch == "main"


async def test_create_worktree_failure_surfaced(host_setup: HostRegistry) -> None:
    """A host ``status: failed`` reply raises with the host's error message."""
    registry = host_setup
    registry._create_reply_for_test.update(  # type: ignore[attr-defined]
        {
            "status": "failed",
            "worktree_path": None,
            "branch": None,
            "error": "not a git repository",
        }
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    with pytest.raises(WorktreeProxyError) as exc:
        await create_worktree_on_host(
            host_registry=registry,
            host_conn=conn,
            repo_path="/tmp/plain",
            branch_name="x",
            base_branch=None,
        )
    # Silent treat-failure-as-success would persist a session with a
    # bogus workspace; the error must propagate.
    assert "not a git repository" in exc.value.message


async def test_create_worktree_incomplete_result_rejected(host_setup: HostRegistry) -> None:
    """An ``ok`` reply missing the worktree_path is rejected, not silently accepted."""
    registry = host_setup
    registry._create_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "ok", "worktree_path": None, "branch": None, "error": None}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    with pytest.raises(WorktreeProxyError) as exc:
        await create_worktree_on_host(
            host_registry=registry,
            host_conn=conn,
            repo_path="/repo",
            branch_name="x",
            base_branch=None,
        )
    assert "incomplete worktree result" in exc.value.message


async def test_remove_worktree_success(host_setup: HostRegistry) -> None:
    """A successful remove reply completes without raising and sends the flag."""
    registry = host_setup
    registry._remove_reply_for_test.update({"status": "ok", "error": None})  # type: ignore[attr-defined]
    conn = registry.get(_HOST_ID)
    assert conn is not None
    await remove_worktree_on_host(
        host_registry=registry,
        host_conn=conn,
        worktree_path="/Users/alice/myrepo-worktrees/feature-login",
        branch="feature/login",
        delete_branch=True,
    )
    sent = registry._sent_frames_for_test[-1]  # type: ignore[attr-defined]
    assert isinstance(sent, HostRemoveWorktreeFrame)
    # delete_branch must thread through unchanged — it controls whether
    # the branch is destroyed.
    assert sent.delete_branch is True
    assert sent.worktree_path == "/Users/alice/myrepo-worktrees/feature-login"
    assert sent.branch == "feature/login"


async def test_remove_worktree_failure_surfaced(host_setup: HostRegistry) -> None:
    """A host ``status: failed`` remove reply raises with the error."""
    registry = host_setup
    registry._remove_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "failed", "error": "worktree path does not exist"}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    with pytest.raises(WorktreeProxyError) as exc:
        await remove_worktree_on_host(
            host_registry=registry,
            host_conn=conn,
            worktree_path="/ghost",
            branch=None,
            delete_branch=False,
        )
    assert "worktree path does not exist" in exc.value.message


async def test_create_worktree_connection_lost_raises_unavailable(
    host_setup: HostRegistry,
) -> None:
    """A dropped host connection raises WorktreeHostUnavailableError.

    Distinct from a host-reported failure: send_text raises
    ConnectionError when the conn was replaced/deregistered, and the
    proxy must classify that as host-unavailable (mapped to 409 by the
    route), not a user-input WorktreeProxyError (400).
    """
    registry = host_setup
    conn = registry.get(_HOST_ID)
    assert conn is not None
    # Deregister so the registry no longer recognizes this conn ->
    # send_text raises ConnectionError on the next send.
    registry.deregister(_HOST_ID)

    with pytest.raises(WorktreeHostUnavailableError) as exc:
        await create_worktree_on_host(
            host_registry=registry,
            host_conn=conn,
            repo_path="/repo",
            branch_name="x",
            base_branch=None,
        )
    assert "connection lost" in exc.value.message
    # It IS a WorktreeProxyError subclass (so best-effort callers still
    # catch it) but the specific type drives the 409 mapping.
    assert isinstance(exc.value, WorktreeProxyError)


async def test_create_worktree_timeout_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host reply within the timeout raises WorktreeHostUnavailableError.

    Uses a host with no auto-replier so the pending future never
    resolves; a tiny patched timeout keeps the test fast.
    """
    import omnigent.server.routes._host_worktree as hw_mod

    monkeypatch.setattr(hw_mod, "_WORKTREE_TIMEOUT_S", 0.05)
    registry = HostRegistry()
    registry.register(
        host_id="host_silent",
        ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=_hello_frame(),
        owner=None,
    )
    conn = registry.get("host_silent")
    assert conn is not None

    with pytest.raises(WorktreeHostUnavailableError) as exc:
        await create_worktree_on_host(
            host_registry=registry,
            host_conn=conn,
            repo_path="/repo",
            branch_name="x",
            base_branch=None,
        )
    assert "did not respond" in exc.value.message
