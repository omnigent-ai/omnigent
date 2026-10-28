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
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from omnigent.runner.transports.ws_tunnel.event_delivery import RunnerEventDispatcher
from omnigent.runner.transports.ws_tunnel.frames import (
    TUNNEL_RENEWAL_CAPABILITY,
    EventAckFrame,
    HelloFrame,
    RenewalReadyFrame,
    RequestFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.live_server import find_free_port

_PEER_LIFETIME_S = 6.0
_RENEWAL_INTERVAL_S = 2.0
_RENEWAL_INTERVAL_ENV = "OMNIGENT_RUNNER_TUNNEL_RENEWAL_INTERVAL_S"

_RUNNER_PROCESS = """
import asyncio, os, signal, sys
from pathlib import Path
from fastapi import FastAPI
from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
app = FastAPI()
@app.post('/command')
async def command():
    marker = Path(sys.argv[2])
    marker.write_text('accepted')
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        marker.write_text('cancelled')
        raise
async def main():
    shutdown = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, shutdown.set)
    await serve_tunnel(app, server_url=sys.argv[1], runner_id='runner_process',
                       runner_version='test', shutdown_event=shutdown)
asyncio.run(main())
"""


@asynccontextmanager
async def _runner_process(url: str, root: Path, interval: str):
    env = dict(os.environ, OMNIGENT_RUNNER_TUNNEL_RENEWAL_INTERVAL_S=interval)
    marker = root.with_suffix(".marker")
    with root.with_suffix(".log").open("w") as log:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _RUNNER_PROCESS,
            url,
            str(marker),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield proc, marker
        finally:
            if proc.returncode is None:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()


@asynccontextmanager
async def _registered_server(app: FastAPI):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="off", access_log=False)
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize(
    "destination",
    [
        "current",
        "wrong_replica",
        "legacy_replica",
        "predecessor_loss",
        "current_loss",
        "current_loss_no_reconnect",
    ],
)
async def test_planned_renewal_preserves_an_accepted_pending_command(
    destination: str,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="omnigent.runner.transports.ws_tunnel.serve")
    monkeypatch.setenv(_RENEWAL_INTERVAL_ENV, "1")
    registry = TunnelRegistry()
    other_registry = TunnelRegistry()
    store = SqlAlchemyConversationStore(db_uri)
    server_app = FastAPI()
    server_app.include_router(
        create_runner_tunnel_router(registry, conversation_store=store), prefix="/v1"
    )
    ingested: list[str] = []
    event_received = asyncio.Event()
    release_event_ack = asyncio.Event()

    async def ingest(*, batch, **kwargs):
        ingested.append(batch.id)
        if batch.events[0]["data"]["source_id"] == "before-renewal":
            event_received.set()
            await release_event_ack.wait()
        return EventAckFrame(batch.id, len(batch.events))

    server_app.state.runner_event_ingest = ingest
    other_app = FastAPI()
    other_router = create_runner_tunnel_router(other_registry, conversation_store=store)
    if destination == "legacy_replica":
        other_router.routes[:] = [
            route for route in other_router.routes if not route.path.endswith("/renewal")
        ]
    other_app.include_router(other_router, prefix="/v1")
    accepted = asyncio.Event()
    release = asyncio.Event()
    outcomes: list[str] = []
    runner_app = FastAPI()
    dispatcher = RunnerEventDispatcher()
    invalidations: list[bool] = []
    sign_in_reads: list[bool] = []

    class Credentials:
        def __call__(self) -> str:
            return "synthetic-bearer"

        def invalidate(self) -> None:
            invalidations.append(True)

        @property
        def sign_in_pending(self) -> bool:
            sign_in_reads.append(True)
            return False

    @runner_app.post("/command")
    async def command() -> dict[str, str]:
        outcomes.append("accepted")
        accepted.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            outcomes.append("cancelled")
            raise
        outcomes.append("completed")
        return {"result": "one-command"}

    async with _registered_server(server_app) as url, _registered_server(other_app) as other:
        original_connect = websockets.connect
        reject_reconnect = False

        def routed_connect(target: str, **kwargs):
            if reject_reconnect and not target.endswith("/renewal"):
                raise OSError("reconnect path unavailable")
            if destination in {"wrong_replica", "legacy_replica"} and target.endswith("/renewal"):
                target = target.replace(
                    url.removeprefix("http://"), other.removeprefix("http://"), 1
                )
            return original_connect(target, **kwargs)

        monkeypatch.setattr(websockets, "connect", routed_connect)
        runner = asyncio.create_task(
            serve_tunnel(
                runner_app,
                server_url=url,
                runner_id="runner_pending",
                runner_version="test",
                auth_token_factory=Credentials(),
                event_dispatcher=dispatcher,
            )
        )
        try:
            first = await registry.wait_for_runner("runner_pending", timeout_s=5)
            assert first is not None
            async with httpx.AsyncClient(
                transport=WSTunnelTransport(registry, "runner_pending"), base_url="http://runner"
            ) as client:
                request = asyncio.create_task(client.post("/command"))
                pending_event = None
                try:
                    await asyncio.wait_for(accepted.wait(), 5)
                    pending_event = asyncio.create_task(
                        dispatcher.submit(
                            "event-session",
                            [
                                {
                                    "type": "external_conversation_item",
                                    "data": {"source_id": "before-renewal"},
                                }
                            ],
                        )
                    )
                    await asyncio.wait_for(event_received.wait(), 2)
                    await asyncio.sleep(2.5)
                    current = registry.get("runner_pending")
                    assert current is not None
                    authority = store.get_runner_connection("runner_pending")
                    assert authority is not None and authority.online
                    assert authority.epoch == current.connection_epoch
                    if current is not first:
                        assert not store.heartbeat_runner_connection(
                            "runner_pending", first.connection_epoch
                        )
                        assert not store.close_runner_connection(
                            "runner_pending", first.connection_epoch
                        )
                    assert not request.done()
                    assert outcomes == ["accepted"]
                    assert not pending_event.done()
                    release_event_ack.set()
                    assert (await asyncio.wait_for(pending_event, 2)).applied == 1
                    ack = await asyncio.wait_for(
                        dispatcher.submit(
                            "event-session",
                            [
                                {
                                    "type": "external_conversation_item",
                                    "data": {"source_id": "event-once"},
                                }
                            ],
                        ),
                        2,
                    )
                    assert ack.applied == 1
                    assert len(ingested) == 2
                    if destination in {
                        "current",
                        "predecessor_loss",
                        "current_loss",
                        "current_loss_no_reconnect",
                    }:
                        assert current is not first
                        assert current.renewal_permit is None
                    else:
                        assert current is first
                        assert other_registry.get("runner_pending") is None
                    if destination == "current_loss_no_reconnect":
                        reject_reconnect = True
                        await current.ws.close(code=1011, reason="current transport loss")
                        async with asyncio.timeout(5):
                            while registry.get("runner_pending") is not None:
                                await asyncio.sleep(0.01)
                        assert registry.has_live_predecessor("runner_pending")
                        assert not request.done()
                        ack = await asyncio.wait_for(
                            dispatcher.submit(
                                "event-session",
                                [
                                    {
                                        "type": "external_conversation_item",
                                        "data": {"source_id": "after-current-loss"},
                                    }
                                ],
                            ),
                            2,
                        )
                        assert ack.applied == 1
                        assert len(ingested) == 3
                    elif destination == "current_loss":
                        await current.ws.close(code=1011, reason="current transport loss")
                        async with asyncio.timeout(5):
                            while registry.get("runner_pending") in (None, current):
                                await asyncio.sleep(0.01)
                        current = registry.get("runner_pending")
                        assert current is not None and current is not first
                        assert not request.done()
                    if destination == "predecessor_loss":
                        await first.ws.close(code=1011, reason="transport loss")
                        with pytest.raises(ConnectionError):
                            await asyncio.wait_for(request, 5)
                        async with asyncio.timeout(5):
                            while outcomes != ["accepted", "cancelled"]:
                                await asyncio.sleep(0.01)
                        assert registry.get("runner_pending") is current
                    else:
                        release.set()
                        response = await asyncio.wait_for(request, 5)
                        assert response.status_code == 200
                        assert response.json() == {"result": "one-command"}
                        assert outcomes == ["accepted", "completed"]
                        if destination == "current_loss_no_reconnect":
                            assert registry.get("runner_pending") is None
                            assert registry.has_live_predecessor("runner_pending")
                            reject_reconnect = False
                            async with asyncio.timeout(5):
                                while registry.get("runner_pending") is None:
                                    await asyncio.sleep(0.01)
                            assert not registry.has_live_predecessor("runner_pending")
                    assert invalidations == []
                    assert len(sign_in_reads) == 1
                    if destination in {"current", "current_loss", "predecessor_loss"}:

                        def predecessor_rows():
                            return [
                                record
                                for record in caplog.records
                                if getattr(record, "event_name", None)
                                == "runner_tunnel_predecessor_drained"
                                and record.attributes["connection_id"] == first.hello.connection_id
                            ]

                        async with asyncio.timeout(5):
                            while not predecessor_rows():
                                await asyncio.sleep(0.01)
                        assert predecessor_rows()[0].attributes["retirement_expected"] is (
                            destination != "predecessor_loss"
                        )
                finally:
                    release_event_ack.set()
                    if pending_event is not None and not pending_event.done():
                        pending_event.cancel()
                        await asyncio.gather(pending_event, return_exceptions=True)
                    release.set()
                    if not request.done():
                        request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            for worker in dispatcher._workers:
                worker.cancel()
            await asyncio.gather(*dispatcher._workers, return_exceptions=True)


async def test_a_different_runner_process_still_replaces_both_generations(
    tmp_path: Path, db_uri: str
) -> None:
    registry = TunnelRegistry()
    app = FastAPI()
    app.include_router(
        create_runner_tunnel_router(
            registry, conversation_store=SqlAlchemyConversationStore(db_uri)
        ),
        prefix="/v1",
    )
    async with (
        _registered_server(app) as url,
        _runner_process(url, tmp_path / "first", "1") as (first_proc, marker),
    ):
        first = await registry.wait_for_runner("runner_process", timeout_s=5)
        assert first is not None
        async with httpx.AsyncClient(
            transport=WSTunnelTransport(registry, "runner_process"), base_url="http://runner"
        ) as client:
            request = asyncio.create_task(client.post("/command"))
            try:
                async with asyncio.timeout(5):
                    while not marker.exists() or registry.get("runner_process") is first:
                        await asyncio.sleep(0.01)
                renewed = registry.get("runner_process")
                assert renewed is not None and renewed.renewal_permit is None
                async with _runner_process(url, tmp_path / "second", "0"):
                    with pytest.raises(ConnectionError, match="replaced"):
                        await asyncio.wait_for(request, 5)
                    async with asyncio.timeout(5):
                        while marker.read_text() != "cancelled":
                            await asyncio.sleep(0.01)
                    first_proc.terminate()
                    await asyncio.wait_for(first_proc.wait(), 5)
                    current = registry.get("runner_process")
                    assert current is not None and current is not renewed
                    assert current.continuity_token is None
            finally:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)


async def test_legacy_client_gets_no_renewal_frames_and_keeps_serving(db_uri: str) -> None:
    registry = TunnelRegistry()
    app = FastAPI()
    app.include_router(
        create_runner_tunnel_router(
            registry, conversation_store=SqlAlchemyConversationStore(db_uri)
        ),
        prefix="/v1",
    )
    async with _registered_server(app) as url:
        async with websockets.connect(
            url.replace("http://", "ws://") + "/v1/runners/legacy/tunnel"
        ) as ws:
            await ws.send(encode_frame(HelloFrame(runner_version="old", frame_protocol_version=1)))
            assert await registry.wait_for_runner("legacy", timeout_s=5) is not None
            async with httpx.AsyncClient(
                transport=WSTunnelTransport(registry, "legacy"), base_url="http://runner"
            ) as client:
                request = asyncio.create_task(client.get("/health"))
                try:
                    raw = await asyncio.wait_for(ws.recv(), 5)
                    frame = decode_frame(raw)
                    assert isinstance(frame, RequestFrame)
                    await ws.send(encode_frame(ResponseHeadFrame(frame.id, 200)))
                    await ws.send(encode_frame(ResponseEndFrame(frame.id)))
                    assert (await asyncio.wait_for(request, 5)).status_code == 200
                    session = registry.get("legacy")
                    assert session is not None and session.renewal_ready is None
                finally:
                    if not request.done():
                        request.cancel()
                    await asyncio.gather(request, return_exceptions=True)


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


async def _drive(creds_factory, *, run_for_s: float, supports_renewal: bool = True) -> _Timeline:
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
            async for raw in connection:
                frame = decode_frame(raw)
                if (
                    supports_renewal
                    and isinstance(frame, HelloFrame)
                    and TUNNEL_RENEWAL_CAPABILITY in frame.capabilities
                ):
                    await connection.send(
                        encode_frame(RenewalReadyFrame("test-permit", "test-process"))
                    )
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


async def test_old_peer_without_renewal_support_keeps_its_original_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_short_renewal(monkeypatch)
    timeline = await _drive(_RecordingCredentials, run_for_s=3, supports_renewal=False)
    assert len(timeline.upgrades) == 1
    assert len(timeline.factory_calls) == 1


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
