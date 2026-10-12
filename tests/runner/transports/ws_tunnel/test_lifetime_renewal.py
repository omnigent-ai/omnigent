"""Runner tunnel renewal ahead of an intermediary's absolute connection lifetime.

The peer below stands in for a server reached through an intermediary that
severs every WebSocket a fixed time after it was accepted, without a close
frame (the runner sees close code 1006). The runner is expected to renew its
tunnel before that boundary — resolving credentials and opening the
replacement connection while the current socket still serves — so the server
never observes the runner offline and a slow credential refresh never
lengthens an outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
from tests._helpers.live_server import find_free_port

_PEER_LIFETIME_S = 6.0
_RENEWAL_INTERVAL_S = 2.0
_RENEWAL_INTERVAL_ENV = "OMNIGENT_RUNNER_TUNNEL_RENEWAL_INTERVAL_S"


def _configure_short_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_RENEWAL_INTERVAL_ENV, str(_RENEWAL_INTERVAL_S))


@dataclass
class _Timeline:
    started: float
    ended: float | None = None
    upgrades: list[float] = field(default_factory=list)
    aborts: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    factory_calls: list[float] = field(default_factory=list)
    spans: list[list[float | None]] = field(default_factory=list)

    def rel(self, stamps: list[float]) -> list[float]:
        return [round(t - self.started, 2) for t in stamps]

    def lifetime_boundary(self) -> float:
        """When the intermediary's absolute lifetime elapses for the first socket.

        Anchored on the first accept, not on an observed sever: make-before-break
        closes the old socket itself once the replacement is serving, so the
        intermediary may never sever and no abort would be recorded.
        """
        assert self.upgrades, "the tunnel was never accepted; the scenario did not run"
        return min(self.upgrades) + _PEER_LIFETIME_S

    def longest_offline_gap(self) -> float:
        """Longest span after the first accept with no accepted connection open.

        Union of the per-connection [open, close] spans; overlapping
        (make-before-break) connections leave no gap, while a reconnect that
        only starts after the socket died shows the real outage.
        """
        spans = sorted((s[0], s[1]) for s in self.spans if s[1] is not None)
        if not spans:
            return float("inf")
        gap = 0.0
        cur_end = spans[0][1]
        for start, end in spans[1:]:
            if start > cur_end:
                gap = max(gap, start - cur_end)
                cur_end = end
            else:
                cur_end = max(cur_end, end)
        # A socket that closed with no replacement before the recording ended
        # leaves a trailing outage up to that end time.
        if self.ended is not None:
            gap = max(gap, self.ended - cur_end)
        return gap


class _RecordingCredentials:
    def __init__(
        self, timeline: _Timeline, *, delay_s: float = 0.0, fail_after: int | None = None
    ):
        self._timeline = timeline
        self._delay_s = delay_s
        self._fail_after = fail_after
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        self._timeline.factory_calls.append(time.monotonic())
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._fail_after is not None and self.calls > self._fail_after:
            raise OSError("credential provider unavailable")
        return f"token-{self.calls}"


class _NoAuthCredentials:
    """A no-auth deployment's factory: every mint legitimately returns None."""

    def __init__(self, timeline: _Timeline):
        self._timeline = timeline
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        self._timeline.factory_calls.append(time.monotonic())


async def _noop_app(scope: object, receive: object, send: object) -> None:
    del scope, receive, send


async def _drive(creds_factory, *, run_for_s: float) -> _Timeline:
    timeline = _Timeline(started=time.monotonic())
    creds = creds_factory(timeline)
    port = find_free_port()

    async def handler(connection) -> None:
        opened = time.monotonic()
        timeline.upgrades.append(opened)
        span: list[float | None] = [opened, None]
        timeline.spans.append(span)
        loop = asyncio.get_running_loop()

        def _sever() -> None:
            timeline.aborts.append(time.monotonic())
            connection.transport.abort()

        handle = loop.call_later(_PEER_LIFETIME_S, _sever)
        try:
            async for _frame in connection:
                pass
        except ConnectionClosed:
            pass
        finally:
            handle.cancel()
            closed = time.monotonic()
            span[1] = closed
            timeline.closes.append(closed)

    async with serve(handler, "127.0.0.1", port):
        tunnel = asyncio.create_task(
            serve_tunnel(
                _noop_app,
                server_url=f"http://127.0.0.1:{port}",
                runner_id="runner_lifetime_renewal",
                runner_version="0.0.0-test",
                auth_token_factory=creds,
            )
        )
        try:
            await asyncio.sleep(run_for_s)
            assert not tunnel.done(), f"tunnel loop exited: {tunnel.exception()!r}"
        finally:
            ended = time.monotonic()
            tunnel.cancel()
            with contextlib.suppress(BaseException):
                await tunnel
    timeline.ended = ended
    timeline.closes = [t for t in timeline.closes if t < ended]
    clamped: list[list[float | None]] = []
    for start, end in timeline.spans:
        if start is None or start >= ended:
            continue
        clamped.append([start, ended if end is None or end > ended else end])
    timeline.spans = clamped
    return timeline


async def test_replacement_tunnel_opens_before_the_lifetime_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_short_renewal(monkeypatch)
    timeline = await _drive(_RecordingCredentials, run_for_s=_PEER_LIFETIME_S + 3)
    boundary = timeline.lifetime_boundary()
    upgrades_before = [t for t in timeline.upgrades if t < boundary]
    calls_before = [t for t in timeline.factory_calls if t < boundary]
    assert len(upgrades_before) >= 2, (
        f"no replacement tunnel was accepted before the first socket's {_PEER_LIFETIME_S}s "
        f"lifetime boundary: upgrades={timeline.rel(timeline.upgrades)} "
        f"aborts={timeline.rel(timeline.aborts)}"
    )
    assert len(calls_before) >= 2, (
        f"credentials were resolved only once (at connect) before the first socket's "
        f"{_PEER_LIFETIME_S}s lifetime boundary: calls={timeline.rel(timeline.factory_calls)} "
        f"aborts={timeline.rel(timeline.aborts)}"
    )


async def test_slow_credential_refresh_does_not_leave_the_runner_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_short_renewal(monkeypatch)
    delay_s = 3.0
    timeline = await _drive(
        lambda tl: _RecordingCredentials(tl, delay_s=delay_s),
        run_for_s=_PEER_LIFETIME_S + delay_s + 4,
    )
    gap = timeline.longest_offline_gap()
    assert gap < 1.0, (
        f"the runner had no open tunnel for {gap:.2f}s (credential refresh took "
        f"{delay_s}s and was only started after the socket was severed): "
        f"upgrades={timeline.rel(timeline.upgrades)} "
        f"closes={timeline.rel(timeline.closes)} aborts={timeline.rel(timeline.aborts)}"
    )


async def test_failed_renewal_keeps_the_existing_tunnel_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_short_renewal(monkeypatch)
    timeline = await _drive(
        lambda tl: _RecordingCredentials(tl, fail_after=1),
        run_for_s=_PEER_LIFETIME_S - 1,
    )
    assert len(timeline.factory_calls) >= 2, (
        "the renewal watcher never attempted a mint, so this test would also pass with "
        f"renewal disabled: factory_calls={timeline.rel(timeline.factory_calls)}"
    )
    assert len(timeline.upgrades) == 1 and not timeline.closes, (
        "a failed credential refresh must keep the working socket open and retry later: "
        f"upgrades={timeline.rel(timeline.upgrades)} closes={timeline.rel(timeline.closes)}"
    )


async def test_renewal_reuses_current_credentials_when_factory_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-auth factory returns None on every mint; renewal must still open the
    replacement using the existing credential instead of stalling until the
    intermediary severs the socket at its lifetime boundary."""
    _configure_short_renewal(monkeypatch)
    timeline = await _drive(_NoAuthCredentials, run_for_s=_PEER_LIFETIME_S + 3)
    boundary = timeline.lifetime_boundary()
    upgrades_before = [t for t in timeline.upgrades if t < boundary]
    assert len(upgrades_before) >= 2, (
        "renewal stalled on a None-minting (no-auth) factory instead of reusing the "
        f"current credential: upgrades={timeline.rel(timeline.upgrades)} "
        f"factory_calls={timeline.rel(timeline.factory_calls)} "
        f"aborts={timeline.rel(timeline.aborts)}"
    )
