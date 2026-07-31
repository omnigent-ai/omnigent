"""
Tests for ``omnigent.server.routes._host_git_import``.

Drives the clone_and_bundle_on_host proxy with a fake host that
auto-replies to the outbound frames — verifies the request_id/future
plumbing, success unpacking, failure surfacing, and offline handling
without a live host process. Mirrors ``test_host_worktree.py``.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.host.frames import (
    HostCloneAndBundleFrame,
    HostHelloFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_git_import import (
    GitImportHostUnavailableError,
    GitImportProxyError,
    clone_and_bundle_on_host,
)

pytestmark = pytest.mark.asyncio

_HOST_ID = "host_clone_test"

# A small but valid tar.gz payload (just a few bytes) for the success path.
_BUNDLE_BYTES = b"\x1f\x8b\x08\x00\x00\x00\x00\x00"
_BUNDLE_B64 = base64.b64encode(_BUNDLE_BYTES).decode()


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
    return HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="clone-host")


@pytest.fixture()
async def host_setup() -> AsyncIterator[HostRegistry]:
    """Register a host plus a background auto-replier for clone frames.

    Tests set ``registry._clone_reply_for_test`` before calling the proxy;
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

    clone_reply: dict[str, Any] = {}
    sent_frames: list[Any] = []

    async def _drain() -> None:
        """Read outbound frames, record them, and resolve the future."""
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            sent_frames.append(frame)
            if isinstance(frame, HostCloneAndBundleFrame):
                fut = conn.pending_clone_bundles.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(clone_reply)

    drain_task = asyncio.create_task(_drain())
    registry._clone_reply_for_test = clone_reply  # type: ignore[attr-defined]
    registry._sent_frames_for_test = sent_frames  # type: ignore[attr-defined]

    try:
        yield registry
    finally:
        conn.outbound_queue.put_nowait(None)
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


async def test_clone_and_bundle_success_returns_decoded_bundle(
    host_setup: HostRegistry,
) -> None:
    """A successful host reply is decoded into bytes, sha, and ref."""
    registry = host_setup
    registry._clone_reply_for_test.update(  # type: ignore[attr-defined]
        {
            "status": "ok",
            "bundle_b64": _BUNDLE_B64,
            "commit_sha": "abc123def456",
            "resolved_ref": "main",
            "error": None,
        }
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    result = await clone_and_bundle_on_host(
        host_registry=registry,
        host_conn=conn,
        git_url="https://github.com/owner/repo.git",
        git_ref="main",
        git_subpath=None,
    )
    assert result.bundle_bytes == _BUNDLE_BYTES
    assert result.commit_sha == "abc123def456"
    assert result.resolved_ref == "main"
    # The frame the proxy actually sent carries the request params.
    sent = registry._sent_frames_for_test[-1]  # type: ignore[attr-defined]
    assert isinstance(sent, HostCloneAndBundleFrame)
    assert sent.git_url == "https://github.com/owner/repo.git"
    assert sent.git_ref == "main"
    assert sent.git_subpath is None


async def test_clone_and_bundle_with_subpath(host_setup: HostRegistry) -> None:
    """The git_subpath parameter threads through to the outbound frame."""
    registry = host_setup
    registry._clone_reply_for_test.update(  # type: ignore[attr-defined]
        {
            "status": "ok",
            "bundle_b64": _BUNDLE_B64,
            "commit_sha": "deadbeef",
            "resolved_ref": "v1.2.3",
            "error": None,
        }
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    result = await clone_and_bundle_on_host(
        host_registry=registry,
        host_conn=conn,
        git_url="https://github.com/owner/repo.git",
        git_ref="v1.2.3",
        git_subpath="packages/core",
    )
    assert result.resolved_ref == "v1.2.3"
    sent = registry._sent_frames_for_test[-1]  # type: ignore[attr-defined]
    assert isinstance(sent, HostCloneAndBundleFrame)
    assert sent.git_subpath == "packages/core"


async def test_clone_and_bundle_failure_surfaced(host_setup: HostRegistry) -> None:
    """A host ``status: failed`` reply raises GitImportProxyError with the message."""
    registry = host_setup
    registry._clone_reply_for_test.update(  # type: ignore[attr-defined]
        {
            "status": "failed",
            "bundle_b64": None,
            "commit_sha": None,
            "resolved_ref": None,
            "error": "repository not found",
        }
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    with pytest.raises(GitImportProxyError) as exc:
        await clone_and_bundle_on_host(
            host_registry=registry,
            host_conn=conn,
            git_url="https://github.com/owner/nonexistent.git",
            git_ref=None,
            git_subpath=None,
        )
    assert "repository not found" in exc.value.message


async def test_clone_and_bundle_incomplete_result_rejected(host_setup: HostRegistry) -> None:
    """An ``ok`` reply missing bundle_b64/commit_sha/resolved_ref is rejected."""
    registry = host_setup
    registry._clone_reply_for_test.update(  # type: ignore[attr-defined]
        {
            "status": "ok",
            "bundle_b64": None,
            "commit_sha": None,
            "resolved_ref": None,
            "error": None,
        }
    )
    conn = registry.get(_HOST_ID)
    assert conn is not None
    with pytest.raises(GitImportProxyError) as exc:
        await clone_and_bundle_on_host(
            host_registry=registry,
            host_conn=conn,
            git_url="https://github.com/owner/repo.git",
            git_ref=None,
            git_subpath=None,
        )
    assert "incomplete clone result" in exc.value.message


async def test_clone_and_bundle_connection_lost_raises_unavailable(
    host_setup: HostRegistry,
) -> None:
    """A dropped host connection raises GitImportHostUnavailableError.

    Distinct from a host-reported failure: send_text raises
    ConnectionError when the conn was replaced/deregistered, and the
    proxy must classify that as host-unavailable (mapped to 409 by the
    route), not a user-input GitImportProxyError (400).
    """
    registry = host_setup
    conn = registry.get(_HOST_ID)
    assert conn is not None
    # Deregister so the registry no longer recognizes this conn ->
    # send_text raises ConnectionError on the next send.
    registry.deregister(_HOST_ID)

    with pytest.raises(GitImportHostUnavailableError) as exc:
        await clone_and_bundle_on_host(
            host_registry=registry,
            host_conn=conn,
            git_url="https://github.com/owner/repo.git",
            git_ref=None,
            git_subpath=None,
        )
    assert "connection lost" in exc.value.message
    # It IS a GitImportProxyError subclass (so best-effort callers still
    # catch it) but the specific type drives the 409 mapping.
    assert isinstance(exc.value, GitImportProxyError)


async def test_clone_and_bundle_timeout_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host reply within the timeout raises GitImportHostUnavailableError.

    Uses a host with no auto-replier so the pending future never
    resolves; a tiny patched timeout keeps the test fast.
    """
    import omnigent.server.routes._host_git_import as gi_mod

    monkeypatch.setattr(gi_mod, "_CLONE_TIMEOUT_S", 0.05)
    registry = HostRegistry()
    registry.register(
        host_id="host_silent",
        ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=_hello_frame(),
        owner=None,
    )
    conn = registry.get("host_silent")
    assert conn is not None

    with pytest.raises(GitImportHostUnavailableError) as exc:
        await clone_and_bundle_on_host(
            host_registry=registry,
            host_conn=conn,
            git_url="https://github.com/owner/repo.git",
            git_ref=None,
            git_subpath=None,
        )
    assert "did not respond" in exc.value.message
