"""
Tests for ``omnigent.server.routes._host_session_import``.

Drives the list/load local-session proxies with a fake host that
auto-replies to the outbound frames — verifies the request_id/future
plumbing, success unpacking, failure surfacing, and offline/timeout
handling without a live host process. Mirrors ``test_host_worktree.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.host.frames import (
    HostHelloFrame,
    HostListLocalSessionsFrame,
    HostLoadLocalSessionFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_session_import import (
    LocalSessionHostUnavailableError,
    LocalSessionNotFoundError,
    LocalSessionProxyError,
    list_local_sessions_on_host,
    load_local_session_on_host,
)

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_ls_test"


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
    return HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="ls-host")


@pytest.fixture()
async def host_setup() -> AsyncIterator[HostRegistry]:
    """Register a host plus a background auto-replier for local-session frames.

    Tests set ``registry._list_reply_for_test`` /
    ``registry._load_reply_for_test`` before calling the proxy; the
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

    list_reply: dict[str, Any] = {}
    load_reply: dict[str, Any] = {}
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
            if isinstance(frame, HostListLocalSessionsFrame):
                fut = conn.pending_list_local_sessions.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(list_reply)
            elif isinstance(frame, HostLoadLocalSessionFrame):
                fut = conn.pending_load_local_session.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(load_reply)

    drain_task = asyncio.create_task(_drain())
    registry._list_reply_for_test = list_reply  # type: ignore[attr-defined]
    registry._load_reply_for_test = load_reply  # type: ignore[attr-defined]
    registry._sent_frames_for_test = sent_frames  # type: ignore[attr-defined]

    try:
        yield registry
    finally:
        conn.outbound_queue.put_nowait(None)
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


async def test_list_local_sessions_success_returns_sessions(host_setup: HostRegistry) -> None:
    """A successful host reply is returned as the session list."""
    registry = host_setup
    sessions = [{"source": "claude", "external_session_id": "abc", "item_count": 3}]
    registry._list_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "ok", "sessions": sessions, "error": None}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None

    result = await list_local_sessions_on_host(
        host_registry=registry, host_conn=conn, source="claude", limit=10
    )

    assert result == sessions
    sent = registry._sent_frames_for_test[-1]  # type: ignore[attr-defined]
    assert isinstance(sent, HostListLocalSessionsFrame)
    assert sent.source == "claude"
    assert sent.limit == 10


async def test_list_local_sessions_failure_surfaced(host_setup: HostRegistry) -> None:
    """A host ``status: failed`` reply raises with the host's error message."""
    registry = host_setup
    registry._list_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "failed", "sessions": None, "error": "no sessions"}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None

    with pytest.raises(LocalSessionProxyError) as exc:
        await list_local_sessions_on_host(
            host_registry=registry, host_conn=conn, source="claude", limit=10
        )
    assert "no sessions" in exc.value.message


async def test_list_local_sessions_invalid_ok_result_rejected(host_setup: HostRegistry) -> None:
    """An ``ok`` reply missing the sessions list is rejected, not silently accepted."""
    registry = host_setup
    registry._list_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "ok", "sessions": None, "error": None}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None

    with pytest.raises(LocalSessionProxyError) as exc:
        await list_local_sessions_on_host(
            host_registry=registry, host_conn=conn, source="claude", limit=10
        )
    assert "invalid session list" in exc.value.message


async def test_load_local_session_success_returns_session(host_setup: HostRegistry) -> None:
    """A successful host reply is returned as the session payload."""
    registry = host_setup
    session = {
        "source": "claude",
        "external_session_id": "abc",
        "workspace": "/repo",
        "items": [{"type": "user", "response_id": None, "data": {}}],
    }
    registry._load_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "ok", "session": session, "error": None}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None

    result = await load_local_session_on_host(
        host_registry=registry, host_conn=conn, source="claude", external_session_id="abc"
    )

    assert result == session
    sent = registry._sent_frames_for_test[-1]  # type: ignore[attr-defined]
    assert isinstance(sent, HostLoadLocalSessionFrame)
    assert sent.source == "claude"
    assert sent.external_session_id == "abc"


async def test_load_local_session_not_found_surfaced(host_setup: HostRegistry) -> None:
    """A ``not_found`` host reply raises the not-found subclass, not the base error."""
    registry = host_setup
    registry._load_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "not_found", "session": None, "error": "gone"}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None

    with pytest.raises(LocalSessionNotFoundError) as exc:
        await load_local_session_on_host(
            host_registry=registry, host_conn=conn, source="claude", external_session_id="x"
        )
    assert "gone" in exc.value.message


async def test_load_local_session_failure_surfaced(host_setup: HostRegistry) -> None:
    """A host ``status: failed`` reply raises the base proxy error, not not-found."""
    registry = host_setup
    registry._load_reply_for_test.update(  # type: ignore[attr-defined]
        {"status": "failed", "session": None, "error": "unreadable transcript"}
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None

    with pytest.raises(LocalSessionProxyError) as exc:
        await load_local_session_on_host(
            host_registry=registry, host_conn=conn, source="claude", external_session_id="x"
        )
    assert "unreadable transcript" in exc.value.message
    assert not isinstance(exc.value, LocalSessionNotFoundError)


async def test_list_local_sessions_connection_lost_raises_unavailable(
    host_setup: HostRegistry,
) -> None:
    """A dropped host connection raises LocalSessionHostUnavailableError.

    Distinct from a host-reported failure: send_text raises
    ConnectionError when the conn was replaced/deregistered, and the
    proxy must classify that as host-unavailable (mapped to 409 by the
    route), not a user-input LocalSessionProxyError (400).
    """
    registry = host_setup
    conn = registry.get(_HOST_ID)
    assert conn is not None
    # Deregister so the registry no longer recognizes this conn ->
    # send_text raises ConnectionError on the next send.
    registry.deregister(_HOST_ID)

    with pytest.raises(LocalSessionHostUnavailableError) as exc:
        await list_local_sessions_on_host(
            host_registry=registry, host_conn=conn, source="claude", limit=10
        )
    assert "connection lost" in exc.value.message
    # It IS a LocalSessionProxyError subclass (so best-effort callers still
    # catch it) but the specific type drives the 409 mapping.
    assert isinstance(exc.value, LocalSessionProxyError)


async def test_load_local_session_timeout_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host reply within the timeout raises LocalSessionHostUnavailableError.

    Uses a host with no auto-replier so the pending future never
    resolves; a tiny patched timeout keeps the test fast (well under
    the real 60s budget).
    """
    import omnigent.server.routes._host_session_import as proxy

    monkeypatch.setattr(proxy, "_LOCAL_SESSION_TIMEOUT_S", 0.05)
    registry = HostRegistry()
    registry.register(
        host_id="host_silent",
        ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=_hello_frame(),
        owner=None,
    )
    conn = registry.get("host_silent")
    assert conn is not None

    with pytest.raises(LocalSessionHostUnavailableError) as exc:
        await load_local_session_on_host(
            host_registry=registry, host_conn=conn, source="claude", external_session_id="x"
        )
    assert "did not respond" in exc.value.message
    # The finally-block cleanup must run even on the timeout path, or the
    # pending-future map leaks an entry per silent host.
    assert conn.pending_load_local_session == {}
