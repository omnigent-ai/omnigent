"""Concurrent saves on the host-streamed (UI) import.

``POST /v1/imports/local/stream`` used to save one session at a time, so a batch
took the sum of every session's store latency. It now keeps reading the host's
stream while up to ``app.state.local_import_concurrency()`` saves run (default
:data:`LOCAL_IMPORT_CONCURRENCY`), bounded by their estimated memory so two
large sessions never save at once, and a chunked (large) session waits in the
queue as JSON text until its save starts. These tests pin the memory bound
against the serial import, and what concurrency must not change: exact
tallies whatever the outcome mix, a host error that waits for running saves and
remembers only the saved sessions for the re-run, a deadline that rolls back
saves still running past its grace period, a client disconnect that rolls back
in-flight saves and stops the host, the serial order for ``1``, and the buffered
``/v1/imports/local`` route staying serial.

The store is a slow in-memory double (persistence is not under test); the host
is either scripted (exact frames and timing) or the real ``HostProcess`` over
the real tunnel receive loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import tracemalloc
from collections import defaultdict
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from fastapi import FastAPI

from omnigent.host import frames as host_frames
from omnigent.host.frames import (
    HOST_CAPABILITIES,
    HostHelloFrame,
    HostImportLocalCancelFrame,
    HostImportLocalFrame,
    HostImportLocalSessionChunkFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import imports as imports_module
from omnigent.server.routes.host_tunnel import LazyImportSessionPayload
from omnigent.session_import.errors import ImportErrorCode
from omnigent.session_import.models import SessionImportEmptyError
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    host_record,
    imports_app,
    local_session,
    post_stream,
    serve_local_sessions,
    wait_until,
)


def _cid(external_session_id: str) -> str:
    return imports_module._import_conversation_id("claude", external_session_id)


def _raw_items(session_id: str, count: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "type": "message",
            "response_id": f"r{i}",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": f"{session_id} {i}"}],
            },
        }
        for i in range(count)
    ]


def _cost(items: int) -> int:
    return imports_module._session_save_cost(_raw_items("s", items))


def _done(**fields: Any) -> tuple[str, dict[str, Any]]:
    return ("done", {"status": "ok", "failed": 0, "skipped": 0, "failures": [], **fields})


@pytest.fixture(autouse=True)
async def _wide_default_executor() -> AsyncIterator[None]:
    """A worker pool wide enough that only the import's own cap binds.

    Each save holds a default-executor thread, and Python sizes that pool from
    the CPU count, so on a small runner it, not the import, would cap overlap.
    """
    executor = ThreadPoolExecutor(max_workers=64)
    asyncio.get_running_loop().set_default_executor(executor)
    yield
    executor.shutdown(wait=False)


class SlowStore(FakeConversationStore):
    """Appends take ``delay`` seconds in their worker thread; tracks overlap."""

    def __init__(self, *, delay: float = 0.0, delays: dict[str, float] | None = None) -> None:
        super().__init__()
        self.delay = delay
        self.delays = {_cid(sid): value for sid, value in (delays or {}).items()}
        # A conversation id here blocks its append until the event is set.
        self.gates: dict[str, threading.Event] = {}
        self.started: defaultdict[str, threading.Event] = defaultdict(threading.Event)
        self.active = 0
        self.max_active = 0
        # Every set of conversations appending at once, as appends start.
        self.running: set[str] = set()
        self.overlaps: list[frozenset[str]] = []
        self._lock = threading.Lock()

    def append(self, conversation_id: str, items: list[Any]) -> list[Any]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.running.add(conversation_id)
            self.overlaps.append(frozenset(self.running))
        try:
            self.started[conversation_id].set()
            gate = self.gates.get(conversation_id)
            if gate is not None:
                gate.wait(10)
            time.sleep(self.delays.get(conversation_id, self.delay))
            stored = super().append(conversation_id, items)
        finally:
            with self._lock:
                self.active -= 1
                self.running.discard(conversation_id)
        return stored


class ForgetfulStore(SlowStore):
    """Keeps no items, so traced memory is only what a save holds while it runs.

    Each append waits (up to 10 s) until ``hold`` appends run at once, so a run
    that admits that many saves reliably holds them all together.
    """

    def __init__(self, *, hold: int) -> None:
        super().__init__(delay=0.3)
        self.hold = hold
        self._arrived = threading.Condition()
        self._count = 0

    def append(self, conversation_id: str, items: list[Any]) -> list[Any]:
        with self._arrived:
            self._count += 1
            self._arrived.notify_all()
            self._arrived.wait_for(lambda: self._count >= self.hold, timeout=10)
        super().append(conversation_id, items[:1])
        return []


class ScriptedHost:
    """A registered tunnel whose frames the test enqueues directly."""

    def __init__(self) -> None:
        self.registry = HostRegistry()
        self.conn = self.registry.register(
            HOST_ID,
            ws=cast(Any, None),
            hello=HostHelloFrame(
                version="0",
                frame_protocol_version=1,
                name="laptop",
                capabilities=list(HOST_CAPABILITIES),
            ),
            owner=None,
            workspace_id=0,
        )
        self.sent: list[Any] = []

    async def serve(self, script: list[tuple[str, Any]]) -> None:
        """Answer the import request with ``script`` (``("sleep", s)`` pauses)."""
        text = await self.conn.outbound_queue.get()
        request = decode_host_frame(text or "")
        assert isinstance(request, HostImportLocalFrame)
        self.sent.append(request)
        queue = self.conn.pending_import_local[request.request_id]
        for kind, data in script:
            if kind == "sleep":
                await asyncio.sleep(data)
                continue
            queue.put_nowait((kind, data))

    def cancel_frames(self) -> list[HostImportLocalCancelFrame]:
        while not self.conn.outbound_queue.empty():
            text = self.conn.outbound_queue.get_nowait()
            if text is not None:
                self.sent.append(decode_host_frame(text))
        return [frame for frame in self.sent if isinstance(frame, HostImportLocalCancelFrame)]


def _app(
    store: FakeConversationStore, host: ScriptedHost | TunnelPair, concurrency: Any = None
) -> FastAPI:
    app = imports_app(store, host_registry=host.registry, host=host_record())
    if concurrency is not None:
        app.state.local_import_concurrency = (
            concurrency if callable(concurrency) else (lambda: concurrency)
        )
    return app


async def _stream(
    app: FastAPI, host: ScriptedHost, script: list[tuple[str, Any]]
) -> list[dict[str, Any]]:
    feeder = asyncio.create_task(host.serve(script))
    try:
        response = await post_stream(app)
    finally:
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder
    return response


def _kinds(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["event"] == kind]


def test_default_is_four_and_capped_at_thirty_two() -> None:
    assert imports_module.LOCAL_IMPORT_CONCURRENCY == 4
    assert imports_module._LOCAL_IMPORT_MAX_CONCURRENCY == 32
    # 270 s stream budget + grace stays well under the ~300 s proxy timeout.
    assert (
        imports_module._LOCAL_IMPORT_STREAM_DEADLINE_S + imports_module._LOCAL_IMPORT_DRAIN_GRACE_S
        <= 285
    )


async def test_saves_overlap_up_to_the_concurrency() -> None:
    store = SlowStore(delay=0.25)
    host = ScriptedHost()
    app = _app(store, host, 8)
    script = [
        (
            "session",
            {
                "total": 16,
                "external_session_id": f"s{i}",
                "workspace": "/repo",
                "items": _raw_items(f"s{i}", 1),
                "title": None,
                "source": "claude",
            },
        )
        for i in range(16)
    ] + [_done()]
    started = time.monotonic()
    events = await _stream(app, host, script)
    elapsed = time.monotonic() - started
    done = events[-1]
    assert (done["imported"], done["failed"], done["complete"]) == (16, 0, True)
    assert store.max_active == 8
    # Serial would be 16 x 0.25 = 4 s; two waves of eight is ~0.5 s.
    assert elapsed < 3.0, elapsed
    assert sorted(store.external) == sorted(f"s{i}" for i in range(16))


async def test_count_cap_bounds_saves_in_flight() -> None:
    store = SlowStore(delay=0.15)
    host = ScriptedHost()
    script = [
        (
            "session",
            {
                "total": 9,
                "external_session_id": f"s{i}",
                "workspace": "/repo",
                "items": _raw_items(f"s{i}", 1),
                "title": None,
                "source": "claude",
            },
        )
        for i in range(9)
    ] + [_done()]
    events = await _stream(_app(store, host, 3), host, script)
    assert events[-1]["imported"] == 9
    assert store.max_active == 3


async def test_two_large_sessions_never_save_at_once_while_small_ones_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Room for three one-item saves beside the most expensive one.
    monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_IN_FLIGHT_EXTRA_COST", 3 * _cost(1))
    large = {_cid("big1"), _cid("big2"), _cid("big3")}

    store = SlowStore(delay=0.15)
    host = ScriptedHost()
    script = (
        [
            (
                "session",
                {
                    "total": 11,
                    "external_session_id": f"a{i}",
                    "workspace": "/repo",
                    "items": _raw_items(f"a{i}", 1),
                    "title": None,
                    "source": "claude",
                },
            )
            for i in range(3)
        ]
        + [
            (
                "session",
                {
                    "total": 11,
                    "external_session_id": "big1",
                    "workspace": "/repo",
                    "items": _raw_items("big1", 400),
                    "title": None,
                    "source": "claude",
                },
            ),
            (
                "session",
                LazyImportSessionPayload(
                    11,
                    json.dumps(
                        {
                            "external_session_id": "big2",
                            "workspace": "/repo",
                            "items": _raw_items("big2", 400),
                            "title": None,
                            "source": "claude",
                        }
                    ),
                    HOST_ID,
                ),
            ),
        ]
        + [
            (
                "session",
                {
                    "total": 11,
                    "external_session_id": f"b{i}",
                    "workspace": "/repo",
                    "items": _raw_items(f"b{i}", 1),
                    "title": None,
                    "source": "claude",
                },
            )
            for i in range(3)
        ]
        + [
            (
                "session",
                {
                    "total": 11,
                    "external_session_id": "big3",
                    "workspace": "/repo",
                    "items": _raw_items("big3", 400),
                    "title": None,
                    "source": "claude",
                },
            ),
        ]
        + [
            (
                "session",
                {
                    "total": 11,
                    "external_session_id": f"c{i}",
                    "workspace": "/repo",
                    "items": _raw_items(f"c{i}", 1),
                    "title": None,
                    "source": "claude",
                },
            )
            for i in range(2)
        ]
        + [_done()]
    )
    events = await _stream(_app(store, host, 4), host, script)
    assert (events[-1]["imported"], events[-1]["failed"]) == (11, 0)
    assert len(store.items[_cid("big2")]) == 400
    assert max(len(running & large) for running in store.overlaps) == 1
    # Small sessions still overlap, also beside a large one.
    assert max(len(running - large) for running in store.overlaps) >= 3
    assert any(len(running & large) == 1 and len(running) > 1 for running in store.overlaps)


async def test_concurrent_peak_memory_stays_near_the_serial_peak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Four sessions each over the budget: the serial build holds one at a
    # time, and so must the concurrent one. The unbounded run shows the
    # measurement would catch four at once.
    items = 6000

    async def peak(concurrency: int, extra_cost: int) -> int:
        monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_IN_FLIGHT_EXTRA_COST", extra_cost)

        async def scenario() -> int:
            store = ForgetfulStore(hold=4 if extra_cost > 4 * _cost(items) else 1)
            host = ScriptedHost()
            app = _app(store, host, concurrency)
            script = [
                (
                    "session",
                    LazyImportSessionPayload(
                        4,
                        json.dumps(
                            {
                                "external_session_id": f"big{i}",
                                "workspace": "/repo",
                                "items": _raw_items(f"big{i}", items),
                                "title": None,
                                "source": "claude",
                            }
                        ),
                        HOST_ID,
                    ),
                )
                for i in range(4)
            ] + [_done()]
            tracemalloc.start()
            try:
                start, _peak = tracemalloc.get_traced_memory()
                events = await _stream(app, host, script)
                _current, traced_peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            assert events[-1]["imported"] == 4
            assert store.max_active == (4 if extra_cost > 4 * _cost(items) else 1), (
                store.max_active
            )
            return traced_peak - start

        return await scenario()

    await peak(1, _cost(1))  # warm-up: one-time allocations (validators, clients) would count once
    serial = await peak(1, _cost(1))
    bounded = await peak(4, _cost(1))
    unbounded = await peak(4, 100 * _cost(items))
    assert bounded < 1.2 * serial, (bounded, serial)
    assert unbounded > 3 * serial, (unbounded, serial)


def test_default_budget_keeps_typical_sessions_four_at_a_time() -> None:
    # A real last-100's largest session: ~1,600 items, ~2 MiB.
    typical = _cost(1600) + imports_module._IMPORT_SAVE_COST_PER_BYTE * 2 * 1024 * 1024
    assert imports_module._admits_save([typical] * 3, typical)
    # A 100,000-item session never saves beside another one.
    huge = _cost(100_000)
    assert not imports_module._admits_save([huge], huge)
    assert imports_module._admits_save([huge, typical, typical], typical)
    # Undecoded (chunked) sessions count as the most expensive.
    assert imports_module._admits_save([typical], None)
    assert not imports_module._admits_save([huge], None)
    assert imports_module._admits_save([], None)


async def test_chunked_sessions_wait_undecoded_until_their_save_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_frames, "IMPORT_SESSION_CHUNK_CHARS", 4096)

    store = SlowStore(delay=0.0)
    gate = threading.Event()
    store.gates[_cid("first")] = gate
    pair = TunnelPair()
    app = _app(store, pair, 1)
    # The host sends the newest (last) first.
    sessions = {
        "big1": local_session("big1", items=60, text="x" * 200),
        "big2": local_session("big2", items=60, text="y" * 200),
        "first": local_session("first"),
    }
    serve_local_sessions(monkeypatch, sessions)
    async with pair:
        request = asyncio.create_task(post_stream(app))
        await asyncio.to_thread(store.started[_cid("first")].wait, 5)
        (queue,) = pair.conn.pending_import_local.values()

        def queued() -> list[Any]:
            # Nothing awaits in between, so the queue is put back as it was.
            entries = [queue.get_nowait() for _ in range(queue.qsize())]
            for entry in entries:
                queue.put_nowait(entry)
            return [data for kind, data in entries if kind == "session"]

        await wait_until(lambda: len(queued()) == 2)
        assert all(isinstance(s, LazyImportSessionPayload) and not s.decoded for s in queued())
        gate.set()
        events = await request
    done = events[-1]
    assert (done["imported"], done["failed"], done["total"]) == (3, 0, 3)
    assert [len(store.items[_cid(sid)]) for sid in ("big1", "big2")] == [60, 60]


async def test_undecodable_chunked_session_counts_as_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for concurrency in (1, 4):
        store = SlowStore(delay=0.05)
        host = ScriptedHost()
        not_a_session = json.dumps({"external_session_id": "bad-items", "items": "nope"})
        script = [
            (
                "session",
                LazyImportSessionPayload(
                    4,
                    json.dumps(
                        {
                            "external_session_id": "ok1",
                            "workspace": "/repo",
                            "items": _raw_items("ok1", 3),
                            "title": None,
                            "source": "claude",
                        }
                    ),
                    HOST_ID,
                ),
            ),
            ("session", LazyImportSessionPayload(4, '{"external_session_id": "cut', HOST_ID)),
            ("session", LazyImportSessionPayload(4, not_a_session, HOST_ID)),
            (
                "session",
                {
                    "total": 4,
                    "external_session_id": "ok2",
                    "workspace": "/repo",
                    "items": _raw_items("ok2", 1),
                    "title": None,
                    "source": "claude",
                },
            ),
            _done(),
        ]
        events = await _stream(_app(store, host, concurrency), host, script)
        done = events[-1]
        assert (done["imported"], done["failed"], done["total"]) == (2, 2, 4)
        # BadChunked sessions should be logged as failures
        assert sorted(store.external) == ["ok1", "ok2"]


async def test_mixed_outcomes_keep_exact_counts_at_any_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_once(concurrency: int) -> dict[str, Any]:
        async def scenario() -> dict[str, Any]:
            store = SlowStore(delay=0.05, delays={"ok1": 0.3})
            # Already imported by an earlier run.
            store.create_conversation(conversation_id=_cid("dup"), title="dup")
            store.set_external_session_id(_cid("dup"), "dup")

            def on_append(conversation_id: str, _items: list[Any]) -> None:
                if conversation_id == _cid("broken-store"):
                    raise RuntimeError("store down")

            store.on_append = on_append
            host = ScriptedHost()
            no_items = (
                "session",
                {
                    "total": 9,
                    "external_session_id": "no-items",
                    "workspace": "/repo",
                    "items": None,
                    "title": None,
                    "source": "claude",
                },
            )
            script = [
                (
                    "session",
                    {
                        "total": 9,
                        "external_session_id": "ok1",
                        "workspace": "/repo",
                        "items": _raw_items("ok1", 1),
                        "title": None,
                        "source": "claude",
                    },
                ),
                ("progress", {"done": 1, "total": 9, "skipped": 1}),  # host skipped one unread
                (
                    "session",
                    {
                        "total": 9,
                        "external_session_id": "dup",
                        "workspace": "/repo",
                        "items": _raw_items("dup", 1),
                        "title": None,
                        "source": "claude",
                    },
                ),
                no_items,
                ("session", {"total": 0}),  # a chunked session that never completed
                (
                    "session",
                    {
                        "total": 9,
                        "external_session_id": "broken-store",
                        "workspace": "/repo",
                        "items": _raw_items("broken-store", 1),
                        "title": None,
                        "source": "claude",
                    },
                ),
                (
                    "session",
                    {
                        "total": 9,
                        "external_session_id": "ok2",
                        "workspace": "/repo",
                        "items": _raw_items("ok2", 1),
                        "title": None,
                        "source": "claude",
                    },
                ),
                _done(
                    failed=2,
                    failures=[
                        {
                            "external_session_id": "empty",
                            "source": "claude",
                            "reason": "has no importable history",
                            "code": ImportErrorCode.SESSION_EMPTY,
                        },
                        {
                            "external_session_id": "unreadable",
                            "source": "claude",
                            "reason": "bad bytes",
                        },
                    ],
                ),
            ]
            events = await _stream(_app(store, host, concurrency), host, script)
            done = events[-1]
            assert done["event"] == "done" and done["complete"] is True
            assert len(done["failures"]) == done["failed"] == len(_kinds(events, "failed"))
            assert (
                len(done["skipped_sessions"]) == done["skipped"] == len(_kinds(events, "skipped"))
            )
            assert len(_kinds(events, "session")) == done["imported"]
            return {
                "counts": (
                    done["imported"],
                    done["already_imported"],
                    done["failed"],
                    done["skipped"],
                ),
                "total": done["total"],
                "failed": sorted(str(f["external_session_id"]) for f in done["failures"]),
                "codes": sorted(f["code"] for f in done["failures"]),
                "stored": sorted(store.external),
                "last_progress": _kinds(events, "progress")[-1]["done"],
            }

        return await scenario()

    serial = await run_once(1)
    assert serial["counts"] == (2, 2, 4, 1)
    assert serial["total"] == 9
    assert serial["failed"] == ["None", "broken-store", "no-items", "unreadable"]
    assert serial["stored"] == ["dup", "ok1", "ok2"]
    # Every session frame processed plus the host's unread skip; the host's
    # own failures fold in at done.
    assert serial["last_progress"] == 7
    assert await run_once(4) == serial


async def test_real_tunnel_with_chunked_empty_and_unreadable_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_frames, "IMPORT_SESSION_CHUNK_CHARS", 4096)

    store = SlowStore(delay=0.1)
    pair = TunnelPair()
    app = _app(store, pair, 4)
    sessions = {
        "big": local_session("big", items=60, text="x" * 200),
        "a": local_session("a"),
        "empty": SessionImportEmptyError("Claude Code session 'empty' has no importable history"),
        "b": local_session("b", items=3),
        "unreadable": ValueError("bad bytes"),
        "c": local_session("c"),
    }
    serve_local_sessions(monkeypatch, sessions)
    async with pair:
        events = await post_stream(app)
    done = events[-1]
    assert (done["imported"], done["already_imported"], done["failed"], done["skipped"]) == (
        4,
        0,
        1,
        1,
    )
    assert done["total"] == 6 and done["complete"] is True
    assert [s["external_session_id"] for s in done["skipped_sessions"]] == ["empty"]
    assert [f["external_session_id"] for f in done["failures"]] == ["unreadable"]
    assert any(isinstance(f, HostImportLocalSessionChunkFrame) for f in pair.host_frames())
    assert len(store.items[_cid("big")]) == 60
    assert store.max_active > 1


async def test_refs_come_out_as_saves_finish() -> None:
    store = SlowStore(delay=0.02, delays={"slow": 0.6})
    host = ScriptedHost()
    script = [
        (
            "session",
            {
                "total": 3,
                "external_session_id": sid,
                "workspace": "/repo",
                "items": _raw_items(sid, 1),
                "title": None,
                "source": "claude",
            },
        )
        for sid in ("slow", "fast1", "fast2")
    ] + [_done()]
    events = await _stream(_app(store, host, 4), host, script)
    order = [event["session_id"] for event in _kinds(events, "session")]
    assert order[-1] == _cid("slow")
    assert set(order) == {_cid("slow"), _cid("fast1"), _cid("fast2")}
    # Progress only counts up (two saves finishing together share one).
    progress = [event["done"] for event in _kinds(events, "progress")]
    assert progress == sorted(set(progress)) and progress[-1] == 3
    assert events[-1]["imported"] == 3


async def test_deadline_rolls_back_saves_still_running_after_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_STREAM_DEADLINE_S", 0.3)
    monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_DRAIN_GRACE_S", 0.3)

    store = SlowStore(delay=0.01)
    gate = threading.Event()
    store.gates[_cid("stuck")] = gate
    host = ScriptedHost()
    # No done frame: the host is still reading when the budget runs out.
    script = [
        (
            "session",
            {
                "total": 5,
                "external_session_id": "quick",
                "workspace": "/repo",
                "items": _raw_items("quick", 1),
                "title": None,
                "source": "claude",
            },
        ),
        (
            "session",
            {
                "total": 5,
                "external_session_id": "stuck",
                "workspace": "/repo",
                "items": _raw_items("stuck", 1),
                "title": None,
                "source": "claude",
            },
        ),
    ]
    started = time.monotonic()
    events = await _stream(_app(store, host, 4), host, script)
    elapsed = time.monotonic() - started
    # Ends at deadline + grace, not when the stuck save finishes (it never
    # does until the gate opens below).
    assert elapsed < 5.0, elapsed
    (error,) = _kinds(events, "error")
    assert error["code"] == ImportErrorCode.TIME_LIMIT_REACHED
    done = events[-1]
    assert (done["imported"], done["failed"], done["complete"]) == (1, 0, False)
    assert [event["session_id"] for event in _kinds(events, "session")] == [_cid("quick")]
    # Only the saved session is skipped on the re-run.
    assert imports_module._continue_skip_ids(None, HOST_ID) == ["quick"]
    assert host.cancel_frames()
    gate.set()
    # The cut save finishes its writes, then is rolled back: no partial.
    await wait_until(lambda: _cid("stuck") in store.deleted)
    assert _cid("stuck") not in store.conversations
    assert "stuck" not in store.external
    assert "quick" in store.external


async def test_host_disconnect_waits_for_running_saves_and_remembers_only_saved() -> None:
    store = SlowStore(delay=0.3)

    def on_append(conversation_id: str, _items: list[Any]) -> None:
        if conversation_id == _cid("broken"):
            raise RuntimeError("store down")

    store.on_append = on_append
    host = ScriptedHost()
    script = [
        (
            "session",
            {
                "total": 10,
                "external_session_id": sid,
                "workspace": "/repo",
                "items": _raw_items(sid, 1),
                "title": None,
                "source": "claude",
            },
        )
        for sid in ("s0", "s1", "broken", "s2")
    ]
    script.append(("disconnected", {}))
    events = await _stream(_app(store, host, 4), host, script)
    (error,) = _kinds(events, "error")
    assert error["code"] == ImportErrorCode.HOST_DISCONNECTED
    # Every save already running finished before the error.
    kinds = [event["event"] for event in events]
    assert kinds.index("error") > max(i for i, kind in enumerate(kinds) if kind == "session")
    done = events[-1]
    assert (done["imported"], done["failed"]) == (3, 1)
    assert "disconnected after 4 of 10 sessions" in error["message"]
    assert (error["processed"], error["imported"], error["total"]) == (4, 3, 10)
    assert sorted(store.external) == ["s0", "s1", "s2"]
    assert sorted(imports_module._continue_skip_ids(None, HOST_ID)) == ["s0", "s1", "s2"]


async def test_client_gone_rolls_back_saves_in_flight_and_stops_the_host() -> None:
    store = SlowStore(delay=0.01)
    gate = threading.Event()
    store.gates[_cid("held")] = gate
    host = ScriptedHost()
    app = _app(store, host, 4)
    feeder = asyncio.create_task(
        host.serve(
            [
                (
                    "session",
                    {
                        "total": 5,
                        "external_session_id": "done-first",
                        "workspace": "/repo",
                        "items": _raw_items("done-first", 1),
                        "title": None,
                        "source": "claude",
                    },
                ),
                (
                    "session",
                    {
                        "total": 5,
                        "external_session_id": "held",
                        "workspace": "/repo",
                        "items": _raw_items("held", 1),
                        "title": None,
                        "source": "claude",
                    },
                ),
            ]
        )
    )
    request = asyncio.create_task(post_stream(app))
    await asyncio.to_thread(store.started[_cid("held")].wait, 5)
    await wait_until(lambda: "done-first" in store.external)
    request.cancel()  # the client went away mid-stream
    with contextlib.suppress(asyncio.CancelledError):
        await request
    await feeder
    await wait_until(lambda: bool(host.cancel_frames()))
    gate.set()
    await wait_until(lambda: _cid("held") in store.deleted)
    assert _cid("held") not in store.conversations
    assert "held" not in store.external
    # A session committed before the disconnect stays (a re-run skips it).
    assert "done-first" in store.external


async def test_override_of_one_keeps_the_serial_event_order() -> None:
    store = SlowStore(delays={"s0": 0.2, "s1": 0.1, "s2": 0.0})
    host = ScriptedHost()
    script = [
        (
            "session",
            {
                "total": 3,
                "external_session_id": f"s{i}",
                "workspace": "/repo",
                "items": _raw_items(f"s{i}", 1),
                "title": None,
                "source": "claude",
            },
        )
        for i in range(3)
    ] + [_done()]
    events = await _stream(_app(store, host, 1), host, script)
    assert store.max_active == 1
    assert [(e["event"], e.get("session_id") or e.get("done")) for e in events] == [
        ("session", _cid("s0")),
        ("progress", 1),
        ("session", _cid("s1")),
        ("progress", 2),
        ("session", _cid("s2")),
        ("progress", 3),
        ("done", None),
    ]


async def test_override_is_read_per_request_clamped_and_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imports_module, "LOCAL_IMPORT_CONCURRENCY", 2)
    monkeypatch.setattr(imports_module, "_LOCAL_IMPORT_MAX_CONCURRENCY", 3)
    calls: list[int] = []

    def counted() -> int:
        calls.append(1)
        return 100

    def broken() -> int:
        raise RuntimeError("flag service down")

    cases: dict[str, tuple[Any, int]] = {
        "absent -> default": (None, 2),
        "raises -> default": (broken, 2),
        "zero -> serial": (lambda: 0, 1),
        "over the max -> max": (counted, 3),
    }
    for label, (override, expected) in cases.items():
        store = SlowStore(delay=0.15)
        host = ScriptedHost()
        script = [
            (
                "session",
                {
                    "total": 6,
                    "external_session_id": f"s{i}",
                    "workspace": "/repo",
                    "items": _raw_items(f"s{i}", 1),
                    "title": None,
                    "source": "claude",
                },
            )
            for i in range(6)
        ] + [_done()]
        events = await _stream(_app(store, host, override), host, script)
        assert events[-1]["imported"] == 6
        assert store.max_active == expected
    assert len(calls) == 1  # once per request, not per session


async def test_buffered_route_stays_serial_in_host_order() -> None:
    from tests.server.import_tunnel_harness import client

    store = SlowStore(delays={"s0": 0.3, "s1": 0.2, "s2": 0.1, "s3": 0.0})
    host = ScriptedHost()
    app = _app(store, host, 8)
    feeder = asyncio.create_task(
        host.serve(
            [
                (
                    "session",
                    {
                        "total": 4,
                        "external_session_id": f"s{i}",
                        "workspace": "/repo",
                        "items": _raw_items(f"s{i}", 1),
                        "title": None,
                        "source": "claude",
                    },
                )
                for i in range(4)
            ]
            + [_done()]
        )
    )
    async with client(app) as http:
        response = await http.post(
            "/v1/imports/local",
            json={"host_id": HOST_ID, "source": "all", "limit": 10},
        )
    await feeder
    assert response.status_code == 200, response.text
    body = response.json()
    assert [ref["session_id"] for ref in body["sessions"]] == [_cid(f"s{i}") for i in range(4)]
    assert (body["imported"], body["already_imported"], body["failed"], body["skipped"]) == (
        4,
        0,
        0,
        0,
    )
    assert store.max_active == 1


async def test_env_override_sets_concurrency_when_no_app_state_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMNIGENT_LOCAL_IMPORT_CONCURRENCY env sets concurrency when app.state override is absent."""
    monkeypatch.setenv("OMNIGENT_LOCAL_IMPORT_CONCURRENCY", "8")
    store = SlowStore(delay=0.15)
    host = ScriptedHost()
    script = [
        (
            "session",
            {
                "total": 9,
                "external_session_id": f"s{i}",
                "workspace": "/repo",
                "items": _raw_items(f"s{i}", 1),
                "title": None,
                "source": "claude",
            },
        )
        for i in range(9)
    ] + [_done()]
    app = imports_app(store, host_registry=host.registry, host=host_record())
    # No app.state.local_import_concurrency set; should read from env
    events = await _stream(app, host, script)
    assert events[-1]["imported"] == 9
    assert store.max_active == 8


async def test_app_state_override_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app.state.local_import_concurrency override wins over env."""
    monkeypatch.setenv("OMNIGENT_LOCAL_IMPORT_CONCURRENCY", "8")
    store = SlowStore(delay=0.15)
    host = ScriptedHost()
    script = [
        (
            "session",
            {
                "total": 9,
                "external_session_id": f"s{i}",
                "workspace": "/repo",
                "items": _raw_items(f"s{i}", 1),
                "title": None,
                "source": "claude",
            },
        )
        for i in range(9)
    ] + [_done()]
    app = _app(store, host, 3)
    # app.state override should win
    events = await _stream(app, host, script)
    assert events[-1]["imported"] == 9
    assert store.max_active == 3


async def test_non_integer_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer OMNIGENT_LOCAL_IMPORT_CONCURRENCY falls back to default with warning."""
    monkeypatch.setenv("OMNIGENT_LOCAL_IMPORT_CONCURRENCY", "not_a_number")
    store = SlowStore(delay=0.15)
    host = ScriptedHost()
    script = [
        (
            "session",
            {
                "total": 6,
                "external_session_id": f"s{i}",
                "workspace": "/repo",
                "items": _raw_items(f"s{i}", 1),
                "title": None,
                "source": "claude",
            },
        )
        for i in range(6)
    ] + [_done()]
    app = imports_app(store, host_registry=host.registry, host=host_record())
    events = await _stream(app, host, script)
    assert events[-1]["imported"] == 6
    # Should use default concurrency (4)
    assert store.max_active == 4


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("100", 32), ("32", 32), ("0", 1), ("-3", 1), (" 2 ", 2), ("", 4)],
)
def test_env_values_clamp_to_one_to_thirty_two(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """OMNIGENT_LOCAL_IMPORT_CONCURRENCY clamps to 1-32; unset or blank means the default."""
    monkeypatch.setenv(imports_module.LOCAL_IMPORT_CONCURRENCY_ENV, raw)
    assert imports_module._local_import_concurrency_from_env() == expected
