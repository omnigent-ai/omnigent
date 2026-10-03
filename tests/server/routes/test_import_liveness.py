"""Host liveness during a local-session import: offline, unreachable, stalls, deadline."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, cast

import pytest

from omnigent.errors import ErrorCode
from omnigent.host.frames import (
    HostImportedLocalSession,
    HostImportLocalCancelFrame,
    HostImportLocalDoneFrame,
    HostImportLocalFrame,
    HostImportLocalProgressFrame,
    HostImportLocalSessionChunkFrame,
    HostImportLocalSessionFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes import _host_launch, host_tunnel
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.errors import ImportErrorCode, LocalImportError
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    client,
    error_event,
    expire_deadline_after_first_append,
    host_record,
    imports_app,
    local_import_body,
    local_session,
    post_stream,
    register_host,
    serve_local_sessions,
    wait_until,
)


async def _drain(registry: HostRegistry, conn: HostConnection, **kwargs: Any) -> list[Any]:
    """Run the stream consumer to completion and return what it yielded."""
    return [
        item
        async for item in imports_module._stream_local_sessions_from_host(
            host_registry=registry, host_conn=conn, source="all", limit=5, **kwargs
        )
    ]


async def _push_after_request(conn: HostConnection, *events: tuple[str, dict[str, Any]]) -> None:
    """Act as the tunnel: wait for the request, then queue ``events`` for it."""
    await conn.outbound_queue.get()
    (queue,) = conn.pending_import_local.values()
    for event in events:
        queue.put_nowait(event)


@pytest.mark.parametrize(
    ("age_s", "expected"),
    [
        (600, "is offline (last seen 10 min ago)"),
        # 30 or 31 s, depending on when the clock ticks.
        (30, "isn't connected right now (last seen 3"),
        (1, "isn't connected right now (last seen just now)"),
    ],
)
async def test_offline_host_409_names_machine_and_last_seen(age_s: int, expected: str) -> None:
    """A host with no tunnel here gets a 409 naming it and when it was last seen."""
    app = imports_app(
        FakeConversationStore(),
        host_registry=HostRegistry(),
        host=host_record(name="studio-mac", age_s=age_s),
    )
    async with client(app) as http:
        response = await http.post("/v1/imports/local/stream", json=local_import_body())
    assert response.status_code == 409
    error = response.json()["error"]
    # Old clients key on the global code; new ones on import_code.
    assert error["code"] == ErrorCode.CONFLICT
    assert error["import_code"] == ImportErrorCode.HOST_OFFLINE
    assert error["retryable"] is True
    assert error["host_name"] == "studio-mac"
    assert abs(error["last_seen_seconds"] - age_s) <= 2
    assert "studio-mac" in error["message"]
    assert expected in error["message"]


async def test_live_host_on_another_replica_stays_wrong_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live host on another replica is wrong_replica with a readable host_unreachable body."""
    monkeypatch.setattr(_host_launch, "_deployment_is_sharded", lambda: True)
    app = imports_app(
        FakeConversationStore(),
        host_registry=HostRegistry(),
        host=host_record(name="studio-mac", age_s=5),
    )
    async with client(app) as http:
        response = await http.post("/v1/imports/local/stream", json=local_import_body())
    assert response.status_code == 400
    error = response.json()["error"]
    # The client's keyless re-address keys on this code.
    assert error["code"] == ErrorCode.WRONG_REPLICA
    assert (error["import_code"], error["retryable"]) == (ImportErrorCode.HOST_UNREACHABLE, True)
    assert error["message"] == (
        "Couldn't reach “studio-mac”'s connection. Try again in a few seconds."
    )
    assert error["host_name"] == "studio-mac"


def test_wrong_replica_message_for_an_unnamed_host() -> None:
    """A host without a name is called "your machine" and carries no host_name."""
    absent = _host_launch.host_absent_error(host_record(), sharded=True)
    error = imports_module._host_wrong_replica_error(host_record(name=" "), absent)
    assert error.code == ErrorCode.WRONG_REPLICA
    assert error.http_status == 400
    assert error.message == (
        "Couldn't reach your machine's connection. Try again in a few seconds."
    )
    assert "host_name" not in error.details


async def test_host_disconnect_mid_batch_reports_progress_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tunnel drop ends the stream with host_disconnected and the batch's progress."""
    store = FakeConversationStore()
    pair = TunnelPair(host_name="studio-mac")
    app = imports_app(store, host_registry=pair.registry, host=host_record(name="studio-mac"))

    def on_append(_conversation_id: str, _items: list[Any]) -> None:
        pair.registry.deregister(HOST_ID, workspace_id=0, conn=pair.conn)

    store.on_append = on_append
    # Host order is oldest first (s2, s1, s0); the reads after the first wait.
    release = threading.Event()
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(3)}
    serve_local_sessions(monkeypatch, sessions, held={"s1", "s0"}, release=release)
    try:
        async with pair:
            events = await post_stream(app)
    finally:
        release.set()

    # Waiting out the per-frame timeout instead would read as host_unresponsive.
    error = error_event(events)
    assert error["code"] == ImportErrorCode.HOST_DISCONNECTED
    assert error["retryable"] is True
    assert [e["event"] for e in events].count("session") == 1
    assert "studio-mac" in error["message"]
    assert "disconnected after 1 of 3 sessions" in error["message"]
    assert (error["host_name"], error["processed"], error["imported"], error["total"]) == (
        "studio-mac",
        1,
        1,
        3,
    )
    assert (events[-1]["imported"], events[-1]["complete"]) == (1, False)


async def test_silent_tunnel_is_unreachable_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered tunnel silent for over 1.5 ping intervals fails fast as host_unreachable."""
    monkeypatch.setattr(imports_module, "PING_INTERVAL_S", 60.0)
    pair = TunnelPair()
    pair.conn.last_frame_at = time.time() - 200
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    events = await post_stream(app)
    error = error_event(events)
    assert error["code"] == ImportErrorCode.HOST_UNREACHABLE
    assert error["retryable"] is True
    assert "isn't responding" in error["message"]
    assert error["silent_seconds"] >= 199
    # No import request was sent to the host.
    assert pair.conn.outbound_queue.empty()


async def test_recently_heard_tunnel_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tunnel heard from within 1.5 ping intervals imports normally."""
    monkeypatch.setattr(imports_module, "PING_INTERVAL_S", 60.0)
    pair = TunnelPair()
    pair.conn.last_frame_at = time.time() - 70
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    async with pair:
        events = await post_stream(app)
    assert events[-1]["event"] == "done"
    assert (events[-1]["imported"], events[-1]["failed"], events[-1]["complete"]) == (1, 0, True)


@pytest.mark.parametrize("legacy_host", [False, True], ids=["current", "legacy"])
async def test_progress_events_report_done_of_total(
    monkeypatch: pytest.MonkeyPatch, legacy_host: bool
) -> None:
    """Progress events count up to the batch total, with or without host heartbeats."""
    pair = TunnelPair(legacy_host=legacy_host)
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {f"s{i}": local_session(f"s{i}") for i in range(3)})
    async with pair:
        events = await post_stream(app)
    progress = [e for e in events if e["event"] == "progress"]
    assert progress[-1] == {"event": "progress", "done": 3, "total": 3}
    assert [p["done"] for p in progress] == sorted(p["done"] for p in progress)
    assert events[-1]["imported"] == 3
    assert events[-1]["total"] == 3
    heartbeats = [f for f in pair.host_frames() if isinstance(f, HostImportLocalProgressFrame)]
    # Only a host that honors the request's progress flag sends heartbeats.
    assert bool(heartbeats) is (not legacy_host)


async def test_repeated_progress_is_sent_once() -> None:
    """Heartbeats that repeat the last (done, total) don't add progress events."""
    registry = HostRegistry()
    conn = register_host(registry)
    app = imports_app(FakeConversationStore(), host_registry=registry, host=host_record())
    host = asyncio.create_task(
        _push_after_request(
            conn,
            ("progress", {"done": 0, "total": None}),
            ("progress", {"done": 0, "total": 3}),
            ("progress", {"done": 0, "total": 3}),
            ("progress", {"done": 0, "total": 3}),
            ("progress", {"done": 2, "total": 3}),
            ("progress", {"done": 2, "total": 3}),
            ("done", {"status": "ok", "failed": 2}),
        )
    )
    events = await post_stream(app)
    await host
    progress = [(e["done"], e["total"]) for e in events if e["event"] == "progress"]
    assert progress == [(0, None), (0, 3), (2, 3)]


async def test_heartbeat_does_not_shorten_the_frame_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session frame landing after a heartbeat, within the per-frame timeout, imports.

    Scaled from 60 s: the frame lands at 70%, past the 50% a heartbeat once shortened it to.
    """
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_TIMEOUT_S", 1.0)
    registry = HostRegistry()
    conn = register_host(registry)
    session = {"external_session_id": "s0", "items": [], "total": 1}

    async def host() -> None:
        await _push_after_request(conn, ("progress", {"done": 0, "total": 1}))
        await asyncio.sleep(0.7)  # a large session frame still in transit
        (queue,) = conn.pending_import_local.values()
        queue.put_nowait(("session", session))
        queue.put_nowait(("done", {"status": "ok"}))

    task = asyncio.create_task(host())
    yielded = await _drain(registry, conn)
    await task
    assert [item for item in yielded if isinstance(item, dict)] == [session]


async def test_silent_host_after_heartbeat_is_unresponsive_after_the_frame_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host quiet after a heartbeat is host_unresponsive once the full timeout passes."""
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_TIMEOUT_S", 0.3)
    registry = HostRegistry()
    conn = register_host(registry)
    host = asyncio.create_task(_push_after_request(conn, ("progress", {"done": 0, "total": 5})))
    started = time.monotonic()
    with pytest.raises(LocalImportError) as raised:
        await _drain(registry, conn)
    await host
    assert raised.value.import_code == ImportErrorCode.HOST_UNRESPONSIVE
    assert time.monotonic() - started >= 0.25


async def test_legacy_host_stall_keeps_the_original_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host without heartbeats that goes quiet fails after the per-frame timeout."""
    monkeypatch.setattr(imports_module, "_HOST_IMPORT_TIMEOUT_S", 0.3)
    pair = TunnelPair(legacy_host=True)
    app = imports_app(
        FakeConversationStore(), host_registry=pair.registry, host=host_record(name="studio-mac")
    )
    sessions = {"s0": local_session("s0"), "s1": local_session("s1")}
    serve_local_sessions(monkeypatch, sessions, load_delay_s=1.0)
    async with pair:
        events = await post_stream(app)
    error = error_event(events)
    assert error["code"] == ImportErrorCode.HOST_UNRESPONSIVE
    assert "stopped responding" in error["message"]
    assert "studio-mac" in error["message"]


@pytest.mark.parametrize("legacy_host", [False, True], ids=["skips-known", "older-host"])
async def test_deadline_stops_host_and_says_how_to_continue(
    monkeypatch: pytest.MonkeyPatch, legacy_host: bool
) -> None:
    """The deadline cancels the host's read; the advice depends on whether a re-run can skip."""
    store = FakeConversationStore()
    expire_deadline_after_first_append(monkeypatch, store)
    pair = TunnelPair(legacy_host=legacy_host)
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    # The read after the first session stalls until the test ends.
    release = threading.Event()
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(20)}
    serve_local_sessions(monkeypatch, sessions, held={"s18"}, release=release)
    try:
        async with pair:
            events = await post_stream(app, limit=20)
            assert pair.cancel_frames()
            if not legacy_host:
                # The cancel frame stopped the stalled host import.
                await wait_until(lambda: not pair.host._import_tasks)
    finally:
        release.set()
    error = error_event(events)
    assert error["code"] == ImportErrorCode.TIME_LIMIT_REACHED
    assert error["retryable"] is True
    assert events[-1]["imported"] == 1
    assert error["message"].startswith("Imported 1 of 20 before the time limit")
    if legacy_host:
        assert "update Omnigent on that machine" in error["message"]
    else:
        assert "run it again to continue" in error["message"]


class _LaggingClock:
    """A ``time`` module stand-in whose monotonic clock reads behind real time.

    Like an event loop whose timers run on a coarser clock (uvloop's is in
    ms): a deadline-bound wait returns while this clock still reads just before it.
    """

    def __init__(self, lag_s: float, after_s: float) -> None:
        self._start = time.monotonic()
        self._lag_s = lag_s
        self._after_s = after_s
        self.time = time.time

    def monotonic(self) -> float:
        now = time.monotonic()
        return now - self._lag_s if now - self._start > self._after_s else now


async def test_deadline_bound_wait_is_the_time_limit_not_a_silent_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait ended by the deadline is time_limit_reached even if the clock fires a hair early."""
    monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_STREAM_DEADLINE_S", 0.3)
    monkeypatch.setattr(imports_module, "time", _LaggingClock(lag_s=0.05, after_s=0.1))
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    # One slow read and no heartbeat due yet: the deadline ends the wait.
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")}, load_delay_s=1.0)
    async with pair:
        events = await post_stream(app)
    error = error_event(events)
    assert error["code"] == ImportErrorCode.TIME_LIMIT_REACHED, error
    assert "silent_seconds" not in error


def test_time_limit_message_counts_sessions_already_there() -> None:
    """The time-limit message counts already-imported sessions as done."""
    exc = LocalImportError(
        "x", import_code=ImportErrorCode.TIME_LIMIT_REACHED, code=ErrorCode.INTERNAL_ERROR
    )
    restated = imports_module._interrupted_import_error(
        exc, host=None, processed=12, imported=2, already_imported=10, total=14
    )
    assert restated.message.startswith("Imported 12 of 14 before the time limit")
    assert restated.details["imported"] == 2
    assert restated.import_code == ImportErrorCode.TIME_LIMIT_REACHED


@pytest.mark.parametrize(
    ("host_skips_known", "message"),
    [
        (
            True,
            "Imported 3 of 9 before the time limit — run it again to continue; "
            "already imported sessions are skipped.",
        ),
        (
            False,
            "Imported 3 of 9 before the time limit. Import fewer sessions at a time, or "
            "update Omnigent on that machine so a re-run skips the ones already imported.",
        ),
    ],
    ids=["skips-known", "older-host"],
)
def test_time_limit_advice_depends_on_the_hosts_skip_support(
    host_skips_known: bool, message: str
) -> None:
    """A host that re-reads everything on a re-run gets advice that can actually help."""
    exc = LocalImportError(
        "x", import_code=ImportErrorCode.TIME_LIMIT_REACHED, code=ErrorCode.INTERNAL_ERROR
    )
    restated = imports_module._interrupted_import_error(
        exc, host=None, processed=3, imported=3, total=9, host_skips_known=host_skips_known
    )
    assert restated.message == message


async def test_buffered_route_reports_time_limit_as_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """The buffered route turns the deadline into a 503 with the partial tally."""
    store = FakeConversationStore()
    expire_deadline_after_first_append(monkeypatch, store)
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    release = threading.Event()
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(20)}
    serve_local_sessions(monkeypatch, sessions, held={"s18"}, release=release)
    try:
        async with pair, client(app) as http:
            response = await http.post("/v1/imports/local", json=local_import_body(limit=20))
    finally:
        release.set()
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == ErrorCode.INTERNAL_ERROR
    assert error["import_code"] == ImportErrorCode.TIME_LIMIT_REACHED
    assert error["error_id"].startswith("err_")
    assert (error["imported"], error["total"]) == (1, 20)


async def test_closing_the_stream_early_cancels_the_host_import() -> None:
    """A consumer that stops reading sends the host a cancel frame and drops its queue."""
    registry = HostRegistry()
    conn = register_host(registry)
    host = asyncio.create_task(
        _push_after_request(
            conn, ("session", {"external_session_id": "s0", "items": [], "total": 9})
        )
    )
    stream = imports_module._stream_local_sessions_from_host(
        host_registry=registry, host_conn=conn, source="all", limit=9
    )
    await anext(stream)
    await cast(Any, stream).aclose()  # what Starlette does when the client goes away
    await host
    sent = conn.outbound_queue.get_nowait()
    assert sent is not None
    assert isinstance(decode_host_frame(sent), HostImportLocalCancelFrame)
    assert conn.pending_import_local == {}


async def test_finished_stream_sends_no_cancel() -> None:
    """A stream that reached the host's done frame leaves the host alone."""
    registry = HostRegistry()
    conn = register_host(registry)
    host = asyncio.create_task(_push_after_request(conn, ("done", {"status": "ok"})))
    assert await _drain(registry, conn) == []
    await host
    assert conn.outbound_queue.empty()


async def test_failed_chunked_session_does_not_reset_the_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunked session cut off by the done frame fails alone and keeps the batch total."""
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())

    async def scripted_host() -> None:
        request = decode_host_frame(await pair.conn.outbound_queue.get() or "")
        assert isinstance(request, HostImportLocalFrame)
        request_id = request.request_id
        session = HostImportedLocalSession(
            external_session_id="s0",
            workspace="/repo",
            items=[
                {
                    "type": "message",
                    "response_id": "r1",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                }
            ],
            source="claude",
        )
        frames = [
            HostImportLocalSessionFrame(request_id=request_id, total=2, session=session),
            # The second session's first slice, never followed by its last.
            HostImportLocalSessionChunkFrame(
                request_id=request_id, total=2, seq=0, last=False, data='{"external_sess'
            ),
            HostImportLocalDoneFrame(request_id=request_id, status="ok"),
        ]
        for frame in frames:
            await pair.server_ws.inbound.put(encode_host_frame(frame))

    receive = asyncio.create_task(
        host_tunnel._receive_loop(
            cast(Any, pair.server_ws),
            pair.conn,
            HOST_ID,
            cast(Any, None),
            pair.registry,
            None,
            None,
            None,
        )
    )
    host = asyncio.create_task(scripted_host())
    try:
        events = await post_stream(app)
    finally:
        receive.cancel()
        host.cancel()
    done = events[-1]
    assert (done["imported"], done["failed"], done["total"], done["complete"]) == (1, 1, 2, True)
    assert [p["total"] for p in events if p["event"] == "progress"] == [2, 2]
