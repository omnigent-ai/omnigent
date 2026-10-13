"""Continuing an interrupted batch import: the re-run skips what the server already has."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import cachetools
import pytest

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.host.frames import (
    MAX_IMPORT_SKIP_IDS,
    HostImportLocalFrame,
    HostImportLocalSessionFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.errors import ImportErrorCode
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    cli_import_body,
    error_event,
    expire_deadline_after_first_append,
    host_record,
    imports_app,
    local_session,
    post_stream,
    register_host,
    serve_local_sessions,
    wait_until,
)


async def _push_after_request(conn: HostConnection, *events: tuple[str, dict[str, Any]]) -> None:
    """Act as the tunnel: wait for the request, then queue ``events`` for it."""
    await conn.outbound_queue.get()
    (queue,) = conn.pending_import_local.values()
    for event in events:
        queue.put_nowait(event)


def _requests(pair: TunnelPair) -> list[HostImportLocalFrame]:
    """Every batch import request the server sent the host."""
    return [f for f in pair.to_host if isinstance(f, HostImportLocalFrame)]


def _sent_session_ids(pair: TunnelPair, since: int) -> list[str]:
    """External ids of the session frames the host sent after frame ``since``."""
    frames = [decode_host_frame(text) for text in pair.host_ws.sent[since:]]
    return [
        f.session.external_session_id for f in frames if isinstance(f, HostImportLocalSessionFrame)
    ]


async def test_rerun_after_the_time_limit_skips_what_the_server_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-run after the time limit skips stored sessions; a completed run forgets them."""
    store = FakeConversationStore()
    # The deadline passes once the first session (s5, oldest) is stored, while
    # the host's next read (s4) is stalled.
    expire_deadline_after_first_append(monkeypatch, store)
    release = threading.Event()
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(6)}
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, sessions, held={"s4"}, release=release)
    try:
        async with pair:
            first = await post_stream(app)
            assert error_event(first)["code"] == ImportErrorCode.TIME_LIMIT_REACHED
            assert set(store.external) == {"s5"}
            await wait_until(lambda: not pair.host._import_tasks)
            release.set()

            sent_before = len(pair.host_ws.sent)
            second = await post_stream(app)
            assert _requests(pair)[-1].skip_external_session_ids == ["s5"]
            # The host skipped it unread; only the rest crossed the tunnel.
            assert set(_sent_session_ids(pair, sent_before)) == set(sessions) - {"s5"}
            done = second[-1]
            assert (done["imported"], done["already_imported"], done["failed"]) == (5, 1, 0)
            assert done["complete"] is True

            # A completed run leaves nothing to continue.
            third = await post_stream(app)
            assert _requests(pair)[-1].skip_external_session_ids == []
            assert third[-1]["already_imported"] == 6
    finally:
        release.set()


async def test_host_without_the_capability_gets_no_skip_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that doesn't advertise skip support is sent no skip list and reads everything."""
    imports_module._remember_continue_skip_ids(None, HOST_ID, ["s0"])
    pair = TunnelPair(legacy_host=True)
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    async with pair:
        events = await post_stream(app)
    assert _requests(pair)[-1].skip_external_session_ids == []
    assert events[-1]["imported"] == 1


async def test_exact_session_import_never_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing one exact session reads it even when it is on the skip list."""
    imports_module._remember_continue_skip_ids(None, HOST_ID, ["s0"])
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    async with pair:
        events = await post_stream(app, source="claude", session_id="s0")
    assert events[-1]["imported"] == 1
    # The exact import leaves the batch's skip list for its own re-run.
    assert imports_module._continue_skip_ids(None, HOST_ID) == ["s0"]


@pytest.mark.parametrize(
    ("body", "remembered"),
    [({"source": "claude", "session_id": "s0"}, None), ({}, ("s0",))],
    ids=["exact", "batch"],
)
async def test_only_an_interrupted_batch_is_remembered(
    body: dict[str, str], remembered: tuple[str, ...] | None
) -> None:
    """A disconnect after one stored session is remembered for a batch, never an exact import."""
    registry = HostRegistry()
    conn = register_host(registry)
    app = imports_app(FakeConversationStore(), host_registry=registry, host=host_record())
    session = {
        "external_session_id": "s0",
        "source": "claude",
        "items": cli_import_body()["items"],
        "total": 1,
    }
    host = asyncio.create_task(
        _push_after_request(conn, ("session", session), ("disconnected", {}))
    )
    events = await post_stream(app, **body)
    await host
    assert error_event(events)["code"] == ImportErrorCode.HOST_DISCONNECTED
    assert events[-1]["imported"] == 1
    assert imports_module._CONTINUE_SKIP_IDS.get((None, HOST_ID)) == remembered


async def test_host_reported_skips_count_as_already_imported() -> None:
    """Skips from heartbeats and the done frame are folded into already_imported once each."""
    registry = HostRegistry()
    conn = register_host(registry)
    app = imports_app(FakeConversationStore(), host_registry=registry, host=host_record())

    async def scripted_host() -> None:
        await conn.outbound_queue.get()
        (queue,) = conn.pending_import_local.values()
        for event in (
            ("progress", {"done": 0, "total": 3, "skipped": 0}),
            ("progress", {"done": 1, "total": 3, "skipped": 1}),
            ("progress", {"done": 2, "total": 3, "skipped": 2}),
            ("done", {"status": "ok", "skipped": 3}),
        ):
            queue.put_nowait(event)

    host = asyncio.create_task(scripted_host())
    events = await post_stream(app)
    await host
    done = events[-1]
    assert (done["imported"], done["already_imported"], done["failed"]) == (0, 3, 0)
    assert done["total"] == 3


def _cache_with_clock(
    monkeypatch: pytest.MonkeyPatch, *, maxsize: int = 8, ttl: float = 60.0
) -> list[float]:
    """Swap in a skip-id cache whose TTL clock the test advances via the returned cell."""
    now = [0.0]
    cache: WorkspaceScopedCache[Any, Any] = WorkspaceScopedCache(
        lambda: cachetools.TTLCache(maxsize=maxsize, ttl=ttl, timer=lambda: now[0])
    )
    monkeypatch.setattr(imports_module, "_CONTINUE_SKIP_IDS", cache)
    return now


def test_remembered_ids_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remembered skip ids are dropped once their TTL passes."""
    now = _cache_with_clock(monkeypatch, ttl=60.0)
    imports_module._remember_continue_skip_ids(None, HOST_ID, ["s0"])
    now[0] = 59.0
    assert imports_module._continue_skip_ids(None, HOST_ID) == ["s0"]
    now[0] = 61.0
    assert imports_module._continue_skip_ids(None, HOST_ID) == []


def test_skip_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past its entry cap the cache drops the oldest (user, host) entries."""
    _cache_with_clock(monkeypatch, maxsize=2)
    for host_id in ("h1", "h2", "h3"):
        imports_module._remember_continue_skip_ids(None, host_id, ["s0"])
    assert imports_module._continue_skip_ids(None, "h1") == []
    assert imports_module._continue_skip_ids(None, "h3") == ["s0"]
    assert len(imports_module._CONTINUE_SKIP_IDS) == 2


def test_production_cache_is_a_bounded_ttl_cache() -> None:
    """The real cache expires entries and caps how many it holds."""
    backing = imports_module._CONTINUE_SKIP_IDS._backing
    assert isinstance(backing, cachetools.TTLCache)
    assert backing.ttl == imports_module._CONTINUE_SKIP_TTL_S
    assert backing.maxsize == imports_module._CONTINUE_SKIP_MAX_ENTRIES


def test_remembered_ids_are_capped_to_the_newest() -> None:
    """At most the cap is remembered, keeping the most recently confirmed ids."""
    ids = [f"s{i}" for i in range(MAX_IMPORT_SKIP_IDS + 50)]
    imports_module._remember_continue_skip_ids(None, HOST_ID, ids)
    kept = imports_module._continue_skip_ids(None, HOST_ID)
    assert len(kept) == MAX_IMPORT_SKIP_IDS
    assert ids[-1] in kept
    assert ids[0] not in kept


def test_remembered_ids_are_per_user_and_host() -> None:
    """One user's or host's skip list never applies to another."""
    imports_module._remember_continue_skip_ids("alice", HOST_ID, ["s0"])
    assert imports_module._continue_skip_ids("bob", HOST_ID) == []
    assert imports_module._continue_skip_ids("alice", "other-host") == []
    assert imports_module._continue_skip_ids("alice", HOST_ID) == ["s0"]


def test_nothing_is_remembered_for_an_empty_run() -> None:
    """An interrupted run that confirmed nothing leaves no entry."""
    imports_module._remember_continue_skip_ids(None, HOST_ID, [])
    assert imports_module._CONTINUE_SKIP_IDS.get((None, HOST_ID)) is None
