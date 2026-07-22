"""Tests for ``_query_host_runner_status`` — the host-owned liveness query.

The host owns runner-process liveness (it holds the ``Popen``). Before the
message-dispatch connect grace, the server asks the host whether an
absent-from-the-tunnel runner is still coming (``alive``) or gone for good
(``dead`` / ``unknown``). A verdict of ``None`` means "no authoritative
answer" (host too old, slow, or the connection dropped), and the caller
falls back to the plain grace wait — so the query can only ever speed up
the cold path, never slow it down.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.host.frames import (
    HostRunnerStatusFrame,
    HostRunnerStatusResultFrame,
    decode_host_frame,
)
from omnigent.server.routes.sessions import _query_host_runner_status

pytestmark = pytest.mark.asyncio


class _FakeHostConn:
    """Minimal ``HostConnection`` stand-in for the runner-status query.

    :param host_id: Host id, only used in error paths / logging.
    """

    def __init__(self, host_id: str = "host_test") -> None:
        """Initialize with an empty pending-query map."""
        self.host_id = host_id
        self.pending_runner_status: dict[str, asyncio.Future[dict[str, str | None]]] = {}


class _ReplyingRegistry:
    """Registry stand-in that replies to the query with a fixed status.

    On ``send_text`` it decodes the outbound frame, finds the matching
    pending future on the connection, and resolves it with ``reply`` — the
    same round-trip the real host tunnel performs, without a socket.

    :param reply: Status to answer with (``"alive"`` / ``"dead"`` /
        ``"unknown"``).
    """

    def __init__(self, conn: _FakeHostConn, reply: str) -> None:
        """Record the connection to resolve against and the canned reply."""
        self._conn = conn
        self._reply = reply
        self.sent: list[str] = []

    def send_text(self, conn: _FakeHostConn, data: str) -> None:
        """Decode the query and immediately resolve its pending future.

        :param conn: Host connection the frame is bound for.
        :param data: Encoded ``host.runner_status`` frame.
        """
        self.sent.append(data)
        frame = decode_host_frame(data)
        assert isinstance(frame, HostRunnerStatusFrame)
        future = conn.pending_runner_status.get(frame.request_id)
        if future is not None and not future.done():
            future.set_result({"status": self._reply})


class _SilentRegistry:
    """Registry stand-in that sends but never replies (forces a timeout)."""

    def __init__(self) -> None:
        """Initialize with an empty send log."""
        self.sent: list[str] = []

    def send_text(self, conn: _FakeHostConn, data: str) -> None:
        """Record the frame but leave the pending future unresolved."""
        self.sent.append(data)


class _BrokenRegistry:
    """Registry stand-in whose ``send_text`` raises ``ConnectionError``."""

    def send_text(self, conn: _FakeHostConn, data: str) -> None:
        """Simulate a host connection that dropped before the send landed."""
        raise ConnectionError("host connection lost")


class _FaultingRegistry:
    """Registry stand-in that resolves the pending future with an exception.

    Models a receive loop that somehow completed the future with an error
    rather than a status dict — the defensive path must map this to ``None``
    rather than let it break the message POST.
    """

    def send_text(self, conn: _FakeHostConn, data: str) -> None:
        """Resolve the query's pending future with an exception."""
        frame = decode_host_frame(data)
        assert isinstance(frame, HostRunnerStatusFrame)
        future = conn.pending_runner_status.get(frame.request_id)
        if future is not None and not future.done():
            future.set_exception(RuntimeError("receive loop blew up"))


@pytest.mark.parametrize("verdict", ["alive", "dead", "unknown"])
async def test_query_returns_host_verdict(verdict: str) -> None:
    """Each host verdict is returned verbatim to the caller.

    ``alive`` drives "wait for the connect", ``dead`` / ``unknown`` drive
    "relaunch now" — the dispatch gate depends on these passing through
    unchanged.
    """
    conn = _FakeHostConn()
    registry = _ReplyingRegistry(conn, verdict)

    result = await _query_host_runner_status(conn, registry, "runner_x")  # type: ignore[arg-type]

    assert result == verdict
    # The outbound frame targeted the queried runner.
    assert len(registry.sent) == 1
    frame = decode_host_frame(registry.sent[0])
    assert isinstance(frame, HostRunnerStatusFrame)
    assert frame.runner_id == "runner_x"
    # The pending entry is cleaned up on the reply path.
    assert conn.pending_runner_status == {}


async def test_query_times_out_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that never replies yields ``None`` (fall back to the grace).

    ``None`` must not be read as "dead" — a slow or too-old host should
    still get the benefit of the connect grace, so the query returning
    ``None`` preserves the prior blind-wait behavior.
    """
    monkeypatch.setattr("omnigent.server.routes.sessions._HOST_RUNNER_STATUS_TIMEOUT_S", 0.05)
    conn = _FakeHostConn()
    registry = _SilentRegistry()

    result = await _query_host_runner_status(conn, registry, "runner_slow")  # type: ignore[arg-type]

    assert result is None
    # The pending entry is cleaned up even on the timeout path.
    assert conn.pending_runner_status == {}


async def test_query_connection_error_returns_none() -> None:
    """A dropped host connection yields ``None`` rather than raising.

    The dispatch path treats ``None`` as "no verdict" and falls back to
    the grace/relaunch flow; a raised error here would surface as a 500
    on the message POST instead.
    """
    conn = _FakeHostConn()
    registry = _BrokenRegistry()

    result = await _query_host_runner_status(conn, registry, "runner_x")  # type: ignore[arg-type]

    assert result is None
    assert conn.pending_runner_status == {}


async def test_query_future_exception_returns_none() -> None:
    """A future resolved with an exception degrades to ``None``, not a raise.

    Defensive contract: the query only ever speeds up the connect grace, so
    an unexpected failure must fall back to the wait rather than surface as
    a 500 on the message POST.
    """
    conn = _FakeHostConn()
    registry = _FaultingRegistry()

    result = await _query_host_runner_status(conn, registry, "runner_x")  # type: ignore[arg-type]

    assert result is None
    assert conn.pending_runner_status == {}


async def test_runner_status_result_field_shape() -> None:
    """The result frame carries exactly the ``status`` the gate reads.

    A sanity check on the wire contract the query helper relies on: the
    host answers with a single ``status`` string.
    """
    frame = HostRunnerStatusResultFrame(request_id="r", status="alive")
    assert frame.status == "alive"
