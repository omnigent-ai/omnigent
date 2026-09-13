"""
Tests for ``omnigent.server.routes._host_filesystem``.

Drives the workspace-read proxy with a fake host that auto-replies to the
outbound ``host.fs_request`` frame — verifies the request_id/future
plumbing, success payload unpacking, error surfacing (reproducing the
runner's HTTP status), and the connection-lost path. Mirrors
``test_host_worktree.py``; no live host process is involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.host.frames import HostFsRequestFrame, HostHelloFrame, decode_host_frame
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_filesystem import (
    HostFsError,
    HostFsUnavailableError,
    read_workspace_from_host,
)

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_fs_test"


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
    return HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="fs-host")


@pytest.fixture()
async def host_setup() -> AsyncIterator[HostRegistry]:
    """Register a host plus a background auto-replier for fs frames.

    Tests set ``registry._fs_reply_for_test`` before calling the proxy;
    the drain task resolves the matching pending future with that reply,
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

    fs_reply: dict[str, Any] = {}
    sent_frames: list[Any] = []

    async def _drain() -> None:
        """Read outbound frames, record them, and resolve the future."""
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            sent_frames.append(frame)
            if isinstance(frame, HostFsRequestFrame):
                fut = conn.pending_fs_requests.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(dict(fs_reply))

    drain_task = asyncio.create_task(_drain())
    registry._fs_reply_for_test = fs_reply  # type: ignore[attr-defined]
    registry._sent_frames_for_test = sent_frames  # type: ignore[attr-defined]

    try:
        yield registry
    finally:
        conn.outbound_queue.put_nowait(None)
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


async def test_read_success_returns_payload(host_setup: HostRegistry) -> None:
    """A successful host reply returns the runner-shaped payload verbatim.

    The payload is what the panel renders; it must thread through unchanged
    so the host-served response is indistinguishable from the runner's.
    """
    registry = host_setup
    payload = {"object": "list", "data": [{"path": "a.txt"}], "has_more": False}
    registry._fs_reply_for_test.update({"status": "ok", "payload": payload})  # type: ignore[attr-defined]
    conn = registry.get(_HOST_ID)
    assert conn is not None

    result = await read_workspace_from_host(
        host_registry=registry,
        host_conn=conn,
        op="list_or_read",
        workspace="/Users/alice/project",
        session_id="conv_x",
        params={"path": "", "limit": 100},
    )

    assert result == payload
    # The frame the proxy sent carries the op + workspace + params.
    sent = registry._sent_frames_for_test[-1]  # type: ignore[attr-defined]
    assert isinstance(sent, HostFsRequestFrame)
    assert sent.op == "list_or_read"
    assert sent.workspace == "/Users/alice/project"
    assert sent.params == {"path": "", "limit": 100}


async def test_read_error_reproduces_runner_status(host_setup: HostRegistry) -> None:
    """A host error reply raises ``HostFsError`` with the runner's status/code.

    The server maps this back to the same HTTP response a live runner would
    have returned, so a 404 stays a 404 rather than collapsing to a 500.
    """
    registry = host_setup
    registry._fs_reply_for_test.update(  # type: ignore[attr-defined]
        {
            "status": "error",
            "payload": None,
            "error_status": 404,
            "error_code": "not_found",
            "error": "Path 'nope.py' not found",
        }
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None

    with pytest.raises(HostFsError) as excinfo:
        await read_workspace_from_host(
            host_registry=registry,
            host_conn=conn,
            op="list_or_read",
            workspace="/w",
            session_id="conv_x",
            params={"path": "nope.py"},
        )
    assert excinfo.value.status == 404
    assert excinfo.value.code == "not_found"


async def test_read_incomplete_ok_result_rejected(host_setup: HostRegistry) -> None:
    """An ``ok`` reply with no payload is treated as unavailable, not empty.

    A missing payload means the host couldn't actually answer; surfacing it
    as unavailable lets the caller fall through to 503 rather than rendering
    a blank panel as if the workspace were empty.
    """
    registry = host_setup
    registry._fs_reply_for_test.update({"status": "ok", "payload": None})  # type: ignore[attr-defined]
    conn = registry.get(_HOST_ID)
    assert conn is not None

    with pytest.raises(HostFsUnavailableError):
        await read_workspace_from_host(
            host_registry=registry,
            host_conn=conn,
            op="changes",
            workspace="/w",
            session_id="conv_x",
            params={},
        )


async def test_read_connection_lost_raises_unavailable() -> None:
    """A send failure raises ``HostFsUnavailableError`` (host went away).

    The registry raises ``ConnectionError`` when the queue is closed; the
    proxy must translate that into the unavailable signal the resolver
    treats like an offline runner.
    """
    registry = HostRegistry()
    ws = _FakeWebSocket()
    conn = registry.register(
        host_id=_HOST_ID,
        ws=ws,  # type: ignore[arg-type]
        hello=_hello_frame(),
        owner=None,
    )
    # Close the outbound queue so send_text fails like a dropped tunnel.
    conn.outbound_queue.put_nowait(None)
    registry.deregister(_HOST_ID)

    with pytest.raises(HostFsUnavailableError):
        await read_workspace_from_host(
            host_registry=registry,
            host_conn=conn,
            op="changes",
            workspace="/w",
            session_id="conv_x",
            params={},
        )
