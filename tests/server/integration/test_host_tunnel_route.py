"""Integration tests for the host WebSocket tunnel route."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlHost
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.host.frames import (
    HostHarnessReadinessFrame,
    HostHelloFrame,
    HostLaunchRunnerResultFrame,
    encode_host_frame,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.stores.host_store import HostStore, host_is_live

pytestmark = pytest.mark.asyncio

_HOST_ID = "1444b179a19322377dcc75cf7fcd1bd2"
_TUNNEL_PATH = f"/v1/hosts/{_HOST_ID}/tunnel"


def _websocket_scope(
    path: str,
    *,
    client_host: str = "127.0.0.1",
) -> dict[str, object]:
    """Build an ASGI WebSocket scope for a test path.

    :param path: WebSocket path, e.g.
        ``"/v1/hosts/1444b179a19322377dcc75cf7fcd1bd2/tunnel"``.
    :param client_host: ASGI client host, e.g. ``"127.0.0.1"``.
    :returns: A minimal ASGI WebSocket scope accepted by FastAPI.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": (client_host, 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


async def _connect_route(
    app: FastAPI,
    path: str,
) -> ApplicationCommunicator:
    """Connect an ASGI WebSocket communicator to the host tunnel.

    :param app: FastAPI app containing the host tunnel router.
    :param path: WebSocket path.
    :returns: The connected ASGI communicator.
    """
    communicator = ApplicationCommunicator(app, _websocket_scope(path))
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept", f"Expected {path} to accept; got {accepted!r}"
    return communicator


def _make_hello(
    name: str = "test-laptop",
    runners: list[str] | None = None,
) -> str:
    """Encode a HostHelloFrame for tests.

    :param name: Human-readable host name.
    :param runners: Live runner IDs, defaults to empty.
    :returns: JSON-encoded hello frame string.
    """
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
            runners=runners or [],
        )
    )


@pytest.fixture()
def host_app(db_uri: str) -> tuple[FastAPI, HostRegistry, HostStore]:
    """Minimal FastAPI app with only the host tunnel route.

    :param db_uri: SQLite URI from the shared fixture.
    :returns: Tuple of (app, host_registry, host_store).
    """
    registry = HostRegistry()
    store = HostStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, store),
        prefix="/v1",
    )
    return app, registry, store


async def _send_hello_and_wait(
    communicator: ApplicationCommunicator,
    registry: HostRegistry,
    *,
    host_id: str = _HOST_ID,
    name: str = "test-laptop",
    runners: list[str] | None = None,
) -> None:
    """Send hello and wait for registration.

    :param communicator: Connected ASGI communicator.
    :param registry: Host registry to poll.
    :param host_id: Expected host_id in the registry.
    :param name: Host name for the hello frame.
    :param runners: Live runner IDs for the hello frame.
    """
    await communicator.send_input(
        {"type": "websocket.receive", "text": _make_hello(name, runners)},
    )
    await asyncio.wait_for(
        _wait_registered(registry, host_id),
        timeout=2.0,
    )


async def _wait_registered(
    registry: HostRegistry,
    host_id: str,
) -> None:
    """Poll until the host appears in the registry.

    :param registry: Host registry to poll.
    :param host_id: Host id to wait for.
    """
    while registry.get(host_id) is None:
        await asyncio.sleep(0.01)


async def _wait_offline(
    store: HostStore,
    host_id: str,
) -> None:
    """Poll until the host's DB status flips to ``"offline"``.

    :param store: Host store to query.
    :param host_id: Host id to check.
    """
    while True:
        host = store.get_host(host_id)
        if host is not None and host.status == "offline":
            return
        await asyncio.sleep(0.01)


async def _wait_count(
    events: list[str],
    count: int,
    *,
    timeout_s: float = 2.0,
) -> None:
    """Poll until *events* holds at least *count* entries.

    :param events: Recorded callback announcements.
    :param count: Number of entries to wait for.
    :param timeout_s: Max seconds to poll before raising.
    :raises asyncio.TimeoutError: If the count is not reached in time.
    """

    async def _poll() -> None:
        while len(events) < count:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout_s)


async def _wait_updated_at_at_least(
    store: HostStore,
    host_id: str,
    floor: int,
    *,
    timeout_s: float = 2.0,
) -> int:
    """Poll until a host's ``updated_at`` reaches ``floor``.

    :param store: Host store to query.
    :param host_id: Host id to check.
    :param floor: Minimum ``updated_at`` to wait for (epoch seconds).
    :param timeout_s: Max seconds to poll before raising.
    :returns: The observed ``updated_at`` once it reaches ``floor``.
    :raises asyncio.TimeoutError: If the floor is not reached in time.
    """

    async def _poll() -> int:
        while True:
            host = store.get_host(host_id)
            if host is not None and host.updated_at >= floor:
                return host.updated_at
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_poll(), timeout=timeout_s)


async def test_host_tunnel_ping_loop_persists_heartbeat(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify the ping loop refreshes the host's last-seen in the DB.

    This is the heartbeat that keeps a long-lived host fresh against the
    liveness TTL — the mechanism that, when it STOPS (crash, OOM, deploy,
    silent network drop), lets the freshness gate age a dead host out of
    the Connected group. Here we prove the live half: while the tunnel is
    up, the ping loop advances ``updated_at``.

    We shrink the ping interval and lift the miss threshold so the loop
    heartbeats rapidly and never declares the host dead during the test
    (otherwise ``set_offline`` would also bump ``updated_at`` and we
    couldn't attribute the advance to the heartbeat). We then age the row
    into the past and assert the heartbeat drags it back while the host
    stays ``online``.
    """
    import omnigent.server.routes.host_tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "PING_INTERVAL_S", 0.02)
    # Never trip the ping-timeout path so the only writer of updated_at
    # during the test is the heartbeat (not set_offline).
    monkeypatch.setattr(tunnel_mod, "PING_MISS_THRESHOLD", 100_000)

    app, registry, store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry)

    # Age the row far into the past, as if the last touch were long ago.
    stale = now_epoch() - 10_000
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost).where(SqlHost.host_id == _HOST_ID).values(updated_at=stale)
        )
        session.commit()

    # The ping loop should heartbeat within a couple of intervals,
    # dragging updated_at back to ~now while status stays online.
    observed = await _wait_updated_at_at_least(store, _HOST_ID, now_epoch() - 5)
    assert observed >= now_epoch() - 5, "ping loop did not persist a fresh heartbeat"

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.status == "online", "heartbeat must keep a live host online"

    # Clean up the live tunnel so the loop stops.
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})


@dataclass
class _IdleWebSocket:
    """WebSocket stand-in for driving ``_ping_loop`` without a route.

    :param closed: Close codes the loop asked for, if it ever did.
    """

    closed: list[int] = field(default_factory=list)

    async def send_text(self, data: str) -> None:
        """Discard an outbound frame.

        :param data: The text frame content.
        """
        del data

    async def receive_text(self) -> str:
        """Block forever — the ping loop never reads.

        :returns: Never returns in practice.
        """
        await asyncio.sleep(3600)
        return ""  # pragma: no cover

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Record a close request.

        :param code: WebSocket close code.
        :param reason: Close reason, ignored.
        """
        del reason
        self.closed.append(code)


async def test_ping_loop_stops_heartbeating_after_takeover(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded connection's ping loop must stop refreshing last-seen.

    ``updated_at`` is the liveness freshness gate. Once a newer tunnel
    holds the host, the old loop no longer speaks for it, so a heartbeat
    from there would keep a dead generation looking fresh — and would
    fight the very cleanup a takeover is supposed to settle.
    """
    import omnigent.server.routes.host_tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "PING_INTERVAL_S", 0.01)

    registry = HostRegistry()
    store = HostStore(db_uri)
    store.upsert_on_connect(_HOST_ID, "test-laptop", "alice@example.com")
    stale = now_epoch() - 10_000
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlHost).where(SqlHost.host_id == _HOST_ID).values(updated_at=stale)
        )
        session.commit()

    hello = HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="test-laptop")
    ws = _IdleWebSocket()
    old_conn = registry.register(_HOST_ID, ws, hello, owner=None)
    # A newer tunnel takes the host over.
    registry.register(_HOST_ID, _IdleWebSocket(), hello, owner=None)

    task = asyncio.create_task(
        tunnel_mod._ping_loop(cast(Any, ws), old_conn, _HOST_ID, store, registry),
    )
    # Many intervals' worth of grace — long enough that a loop still
    # writing would have written dozens of times.
    done, _pending = await asyncio.wait({task}, timeout=0.5)

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.updated_at == stale, "a superseded ping loop must not refresh last-seen"
    assert task in done, "a superseded ping loop must exit rather than keep spinning"
    assert ws.closed == [], "a takeover is not a ping timeout; the loop must not close the socket"


class _ProbeLock:
    """Write-lock stand-in that exposes acquisition ordering to tests.

    Two knobs, one per test: *hold_first* parks the first acquirer at
    ``gate`` (modelling a thread that started but has not yet taken the
    lock), and ``contended`` fires when an acquirer has to wait for a
    current holder — a signal, so tests never infer blocking from elapsed
    time.

    :param hold_first: Whether to park the first acquirer at the gate.
    """

    def __init__(self, *, hold_first: bool = False) -> None:
        self._lock = threading.Lock()
        self._hold_first = hold_first
        self.at_gate = threading.Event()
        self.gate = threading.Event()
        self.contended = threading.Event()

    def acquire(self, *args: object, **kwargs: object) -> bool:
        """Acquire, signalling gate arrival and contention.

        :returns: Whether the underlying lock was acquired.
        """
        if self._hold_first:
            self._hold_first = False
            self.at_gate.set()
            assert self.gate.wait(timeout=5.0), "gate was never opened"
        if self._lock.acquire(blocking=False):
            return True
        self.contended.set()
        return self._lock.acquire()

    def release(self) -> None:
        """Release the underlying lock."""
        self._lock.release()

    def __enter__(self) -> _ProbeLock:
        """Acquire on context entry.

        :returns: This lock.
        """
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> bool:
        """Release on context exit.

        :returns: ``False`` so exceptions propagate.
        """
        self.release()
        return False


async def test_set_offline_waits_out_an_inflight_heartbeat(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_offline must take the same lock a running heartbeat holds.

    Cancelling the ping task cannot stop a ``to_thread`` heartbeat that
    already started, and the heartbeat rewrites ``status="online"``. This
    pins the exclusion half: while that write is in progress, the route's
    ``set_offline`` waits rather than interleaving with it.
    """
    import omnigent.server.routes.host_tunnel as tunnel_mod

    store = HostStore(db_uri)
    store.upsert_on_connect(_HOST_ID, "test-laptop", "alice@example.com")
    registry = HostRegistry()
    hello = HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="test-laptop")
    conn = registry.register(_HOST_ID, _IdleWebSocket(), hello, owner=None)
    lock = _ProbeLock()
    conn.db_write_lock = cast(Any, lock)

    entered = threading.Event()
    release = threading.Event()
    real_heartbeat = store.heartbeat

    def _slow_heartbeat(host_id: str) -> None:
        """Hold the connection's write lock until the test releases it.

        :param host_id: Host identifier forwarded to the real heartbeat.
        """
        entered.set()
        assert release.wait(timeout=5.0), "heartbeat was never released"
        real_heartbeat(host_id)

    monkeypatch.setattr(store, "heartbeat", _slow_heartbeat)

    beat = asyncio.create_task(
        asyncio.to_thread(tunnel_mod._write_heartbeat, conn, store, _HOST_ID, registry),
    )
    assert await asyncio.to_thread(entered.wait, 5.0), "heartbeat never started"

    offline = asyncio.create_task(
        asyncio.to_thread(tunnel_mod._write_offline, conn, store, _HOST_ID),
    )
    # Signalled, not timed: the offline worker reached the lock and blocked.
    assert await asyncio.to_thread(lock.contended.wait, 5.0), (
        "set_offline never waited for the connection lock"
    )
    blocked = store.get_host(_HOST_ID)
    assert blocked is not None
    assert blocked.status == "online", "offline landed while the heartbeat held the lock"

    release.set()
    await asyncio.wait_for(asyncio.gather(beat, offline), timeout=5.0)

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.status == "offline", "a late heartbeat must not resurrect a dead host"
    assert not host_is_live(host)


async def test_late_heartbeat_after_cleanup_does_not_resurrect_host(
    db_uri: str,
) -> None:
    """A heartbeat reaching its write after cleanup must be a no-op.

    The write lock only excludes, it does not order: a heartbeat thread
    that started before teardown can take the lock *after* ``set_offline``
    released it, rewriting ``status="online"`` for a host with no tunnel.
    Cleanup pops the connection first, so the write-side registration
    check is what makes that late heartbeat harmless.
    """
    import omnigent.server.routes.host_tunnel as tunnel_mod

    store = HostStore(db_uri)
    store.upsert_on_connect(_HOST_ID, "test-laptop", "alice@example.com")
    registry = HostRegistry()
    hello = HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="test-laptop")
    conn = registry.register(_HOST_ID, _IdleWebSocket(), hello, owner=None)
    late = _ProbeLock(hold_first=True)
    conn.db_write_lock = cast(Any, late)

    # The heartbeat is dispatched and running, but parked before the lock.
    beat = asyncio.create_task(
        asyncio.to_thread(tunnel_mod._write_heartbeat, conn, store, _HOST_ID, registry),
    )
    assert await asyncio.to_thread(late.at_gate.wait, 5.0), "heartbeat never reached the lock"

    # Route teardown runs to completion while the heartbeat is parked.
    registry.deregister(_HOST_ID, conn=conn)
    await asyncio.to_thread(tunnel_mod._write_offline, conn, store, _HOST_ID)
    assert store.get_host(_HOST_ID).status == "offline"  # type: ignore[union-attr]

    # Only now does the heartbeat get the lock — strictly after offline.
    late.gate.set()
    await asyncio.wait_for(beat, timeout=5.0)

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.status == "offline", "a late heartbeat must not resurrect a dead host"
    assert not host_is_live(host)


async def test_host_tunnel_marks_offline_after_an_out_of_band_deregister(
    db_uri: str,
) -> None:
    """An empty registry slot still needs the row marked offline.

    The managed-wake path deregisters the live connection out of band. The
    route's own teardown then finds an empty slot — which is NOT a
    takeover, so suppressing cleanup would leave the row online for a
    liveness TTL with no hosts-changed nudge for clients.
    """
    app, registry, store, _connects, disconnects = _announcing_app(db_uri)
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry)

    # The wake path drops the current connection; no newer tunnel exists.
    assert registry.deregister(_HOST_ID) is not None
    assert registry.get(_HOST_ID) is None

    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(comm.future, timeout=2.0)

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.status == "offline", "an empty slot is not a takeover; the row must go offline"
    assert disconnects == [_HOST_ID], "clients still need exactly one hosts-changed nudge"


async def test_host_tunnel_accepts_and_registers(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    Verify that a host connecting and sending hello appears in the
    HostRegistry.

    If the host_id is missing from online_host_ids after hello, the
    registration path in the tunnel handler is broken.
    """
    app, registry, _store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry)

    # Host should be registered.
    assert _HOST_ID in registry.online_host_ids()
    conn = registry.get(_HOST_ID)
    assert conn is not None
    assert conn.hello.name == "test-laptop"


async def test_host_tunnel_deregisters_on_disconnect(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    Verify that the host is removed from the registry on disconnect.

    If the host remains registered after disconnect, the deregister
    call in the finally block is missing.
    """
    app, registry, _store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry)
    assert registry.get(_HOST_ID) is not None

    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    # Give the handler a moment to process the disconnect.
    await asyncio.sleep(0.1)

    assert registry.get(_HOST_ID) is None


async def test_host_tunnel_upserts_db_on_connect(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    Verify that the host is upserted into the DB on connect.

    If get_host returns None after connect, the upsert_on_connect
    call in the tunnel handler is missing.
    """
    app, _registry, store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, _registry)

    host = store.get_host(_HOST_ID)
    assert host is not None, "Host row should exist in DB after tunnel connect"
    assert host.name == "test-laptop"
    assert host.status == "online"


async def test_host_tunnel_refreshes_harness_readiness_without_reconnect(
    db_uri: str,
) -> None:
    """A live host readiness update must replace the stale setup warning state."""
    registry = HostRegistry()
    store = HostStore(db_uri)
    updates: list[str] = []

    async def _on_host_update(host_id: str, _owner: str | None) -> None:
        updates.append(host_id)

    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(
            registry,
            store,
            on_host_update=_on_host_update,
        ),
        prefix="/v1",
    )
    comm = await _connect_route(app, _TUNNEL_PATH)
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(
                HostHelloFrame(
                    version="0.1.0-test",
                    frame_protocol_version=1,
                    name="test-laptop",
                    configured_harnesses={"pi": False},
                )
            ),
        }
    )
    await asyncio.wait_for(_wait_registered(registry, _HOST_ID), timeout=2.0)

    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(
                HostHarnessReadinessFrame(configured_harnesses={"pi": True})
            ),
        }
    )

    async def _wait_until_ready() -> None:
        while True:
            host = store.get_host(_HOST_ID)
            if host is not None and host.configured_harnesses == {"pi": True}:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait_until_ready(), timeout=0.5)

    conn = registry.get(_HOST_ID)
    assert conn is not None
    assert conn.hello.configured_harnesses == {"pi": True}
    assert updates == [_HOST_ID]


async def test_host_tunnel_sets_offline_on_disconnect(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    Verify that the host is marked offline in the DB on disconnect.

    If status is still 'online' after disconnect, the set_offline
    call in the finally block is missing.
    """
    app, registry, store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry)

    await comm.send_input({"type": "websocket.disconnect", "code": 1000})

    # Poll until status flips — avoids the fixed-sleep race that
    # causes flakes under load (set_offline runs via to_thread and
    # may not complete within a fixed 0.1 s window).
    await asyncio.wait_for(_wait_offline(store, _HOST_ID), timeout=2.0)

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.status == "offline"


def _announcing_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, list[str], list[str]]:
    """Build a host-tunnel app that records its connect/disconnect announcements.

    :param db_uri: SQLite URI from the shared fixture.
    :returns: Tuple of (app, registry, store, connects, disconnects),
        the two lists collecting announced host ids in call order.
    """
    registry = HostRegistry()
    store = HostStore(db_uri)
    connects: list[str] = []
    disconnects: list[str] = []

    async def _on_host_connect(host_id: str, _owner: str | None) -> None:
        connects.append(host_id)

    async def _on_host_disconnect(host_id: str, _owner: str | None) -> None:
        disconnects.append(host_id)

    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(
            registry,
            store,
            on_host_connect=_on_host_connect,
            on_host_disconnect=_on_host_disconnect,
        ),
        prefix="/v1",
    )
    return app, registry, store, connects, disconnects


async def _overlap_tunnels(
    app: FastAPI,
    registry: HostRegistry,
    connects: list[str],
) -> tuple[ApplicationCommunicator, HostConnection]:
    """Open a second tunnel for an already-connected host and let the first unwind.

    The second connect announcement — not a registry poll — is the
    "replacement is live" signal: a buggy cleanup can empty the registry
    between polls, which would look like the takeover never happened.

    :param app: App hosting the tunnel route.
    :param registry: Registry the tunnels register in.
    :param connects: Connect announcements recorded by the app.
    :returns: Tuple of (surviving communicator, the superseded connection).
    """
    comm_old = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm_old, registry)
    old_conn = registry.get(_HOST_ID)
    assert old_conn is not None

    comm_new = await _connect_route(app, _TUNNEL_PATH)
    await comm_new.send_input({"type": "websocket.receive", "text": _make_hello()})
    await _wait_count(connects, 2)

    # Barrier: the superseded handler's task has finished, so whatever
    # cleanup it does has already happened — no sleep-based guessing.
    await asyncio.wait_for(comm_old.future, timeout=2.0)
    return comm_new, old_conn


async def test_host_tunnel_takeover_keeps_replacement_online(
    db_uri: str,
) -> None:
    """A second tunnel for one host_id must not leave the live host offline.

    Registration is newest-wins: the second tunnel poisons the first's
    outbound queue and takes the registry slot. The first handler then
    unwinds cleanly — and must not run disconnect cleanup, or it
    deregisters the replacement and flips the DB row offline while a
    perfectly good tunnel is up. Nothing rewrites ``status`` until the
    next reconnect, so the host would read offline for the whole life of
    a working tunnel.
    """
    app, registry, store, connects, _disconnects = _announcing_app(db_uri)
    comm_new, old_conn = await _overlap_tunnels(app, registry, connects)

    live = registry.get(_HOST_ID)
    assert live is not None, "superseded handler deregistered the live tunnel"
    assert live is not old_conn, "the replacement should hold the registry slot"

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.status == "online", "superseded handler flipped the live host offline"
    assert host_is_live(host), "the live tunnel's host must still read as live"

    await comm_new.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_host_tunnel_takeover_suppresses_disconnect_callback(
    db_uri: str,
) -> None:
    """Takeover announces nothing; the survivor's real close announces once.

    ``on_host_disconnect`` drives the SSE "hosts changed" nudge, so firing
    it for a superseded connection tells clients a live host went away.
    """
    app, registry, store, connects, disconnects = _announcing_app(db_uri)
    comm_new, _old_conn = await _overlap_tunnels(app, registry, connects)

    assert disconnects == [], "a superseded connection must not announce a disconnect"

    await comm_new.send_input({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(comm_new.future, timeout=2.0)

    assert disconnects == [_HOST_ID], "the survivor's real close must announce exactly once"
    await asyncio.wait_for(_wait_offline(store, _HOST_ID), timeout=2.0)


async def test_host_tunnel_rejects_bad_protocol_version(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    Verify that a hello with wrong protocol version closes with 4002.

    If the tunnel accepts a mismatched version, future frame
    encoding/decoding could silently fail.
    """
    app, _registry, _store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)

    bad_hello = encode_host_frame(
        HostHelloFrame(
            version="0.1.0",
            frame_protocol_version=99,
            name="laptop",
        )
    )
    await comm.send_input(
        {"type": "websocket.receive", "text": bad_hello},
    )

    close = await comm.receive_output(timeout=1.0)
    assert close["type"] == "websocket.close"
    assert close.get("code") == 4002


async def test_host_tunnel_rejects_non_hello_first_frame(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    Verify that a non-hello frame as the first message closes
    with 4001.

    The protocol requires hello as the first frame; sending
    anything else is a client bug.
    """
    app, _registry, _store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)

    result_frame = encode_host_frame(
        HostLaunchRunnerResultFrame(
            request_id="req_1",
            status="launched",
            runner_id="runner_x",
        )
    )
    await comm.send_input(
        {"type": "websocket.receive", "text": result_frame},
    )

    close = await comm.receive_output(timeout=1.0)
    assert close["type"] == "websocket.close"
    assert close.get("code") == 4001


async def test_host_tunnel_routes_launch_result_to_future(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    Verify that a launch_runner_result frame resolves the pending
    future on the HostConnection.

    This is the mechanism by which POST /v1/hosts/{id}/runners
    awaits the host's response. If the future doesn't resolve,
    the launch endpoint would time out.
    """
    app, registry, _store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry)

    conn = registry.get(_HOST_ID)
    assert conn is not None

    # Simulate the server creating a pending launch future
    # (this is what the REST endpoint would do).
    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, str | None]] = loop.create_future()
    conn.pending_launches["req_test"] = future

    # Host sends the result.
    result_frame = encode_host_frame(
        HostLaunchRunnerResultFrame(
            request_id="req_test",
            status="launched",
            runner_id="runner_token_xyz",
        )
    )
    await comm.send_input(
        {"type": "websocket.receive", "text": result_frame},
    )

    # Future should resolve within a short time.
    result = await asyncio.wait_for(future, timeout=2.0)
    assert result["status"] == "launched"
    assert result["runner_id"] == "runner_token_xyz"
    assert result["error"] is None


# ── Cross-owner re-registration rejection ───────────────────


class _FixedAuthProvider(AuthProvider):
    """Auth provider that resolves every request to one fixed user.

    :param user_id: The user id ``get_user_id`` always returns.
    """

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def get_user_id(self, request: object) -> str:
        """Return the fixed user id regardless of the request."""
        del request
        return self._user_id


def _owned_app(
    db_uri: str,
    *,
    authed_user: str,
) -> tuple[FastAPI, HostRegistry, HostStore]:
    """Build a host-tunnel app whose auth resolves to ``authed_user``.

    Wires a multi-user posture (``local_single_user=False``), so the
    host-hijack boundary that the cross-owner check enforces is active.

    :param db_uri: SQLite URI from the shared fixture.
    :param authed_user: Identity the connecting peer authenticates as.
    :returns: Tuple of (app, host_registry, host_store).
    """
    registry = HostRegistry()
    store = HostStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(
            registry,
            store,
            auth_provider=_FixedAuthProvider(authed_user),
            local_single_user=False,
        ),
        prefix="/v1",
    )
    return app, registry, store


async def test_cross_owner_refused_with_409_before_accept(db_uri: str) -> None:
    """A host_id owned by another user is refused with HTTP 409 pre-accept.

    Reproduces the stranded-host trap: a machine first registered under
    one identity (e.g. the single-user ``local`` owner) and later dialing
    in under a different account must NOT silently complete the handshake
    and then have its registration dropped by the host_id UNIQUE
    collision. The server detects the conflict before ``accept()`` and
    answers the upgrade with a 409 denial response, so the host can
    surface a specific, actionable error instead of looping.
    """
    app, registry, store = _owned_app(db_uri, authed_user="bob@example.com")
    # The host_id is already owned by someone else.
    store.upsert_on_connect(host_id=_HOST_ID, name="alices-laptop", user_id="alice@example.com")

    scope = _websocket_scope(_TUNNEL_PATH)
    # Advertise the denial-response extension, as uvicorn does in prod.
    scope["extensions"] = {"websocket.http.response": {}}
    comm = ApplicationCommunicator(app, scope)
    await comm.send_input({"type": "websocket.connect"})

    start = await comm.receive_output(timeout=1.0)
    assert start["type"] == "websocket.http.response.start"
    assert start["status"] == 409
    body = await comm.receive_output(timeout=1.0)
    assert body["type"] == "websocket.http.response.body"
    assert b"already registered to a different account" in body["body"]

    # Bob never registered, and Alice's row is untouched (no cross-user
    # takeover, and her host was not flipped offline).
    assert registry.get(_HOST_ID) is None
    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.user_id == "alice@example.com"
    assert host.status == "online"


async def test_cross_owner_refused_with_close_when_no_denial_extension(db_uri: str) -> None:
    """Without the denial-response extension, the refusal falls back to a close.

    The ASGI server may not advertise ``websocket.http.response``; the
    rejection must still land (as a pre-accept close → 403 on the client),
    just with the less specific message.
    """
    app, registry, store = _owned_app(db_uri, authed_user="bob@example.com")
    store.upsert_on_connect(host_id=_HOST_ID, name="alices-laptop", user_id="alice@example.com")

    # No "extensions" key in the scope → fallback path.
    comm = ApplicationCommunicator(app, _websocket_scope(_TUNNEL_PATH))
    await comm.send_input({"type": "websocket.connect"})

    closed = await comm.receive_output(timeout=1.0)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == 4009
    assert registry.get(_HOST_ID) is None


async def test_same_owner_reconnect_still_accepts(db_uri: str) -> None:
    """The cross-owner guard does not block a legitimate same-owner reconnect.

    A host owned by Bob that reconnects as Bob must accept and register —
    otherwise the new check would break normal reconnection.
    """
    app, registry, store = _owned_app(db_uri, authed_user="bob@example.com")
    store.upsert_on_connect(host_id=_HOST_ID, name="bobs-laptop", user_id="bob@example.com")

    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry, name="bobs-laptop")

    assert _HOST_ID in registry.online_host_ids()
    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.user_id == "bob@example.com"
    assert host.status == "online"

    await comm.send_input({"type": "websocket.disconnect", "code": 1000})


# ── Managed-host launch-token auth ──────────────────────────


def _managed_scope(path: str, token: str) -> dict[str, object]:
    """Build a WebSocket scope carrying a managed-host launch token.

    :param path: WebSocket path, e.g. ``"/v1/hosts/5d23e459b50e20479abf5d3fa8e2f936/tunnel"``.
    :param token: Raw launch token for the managed-host header.
    :returns: ASGI WebSocket scope with the token header set.
    """
    scope = _websocket_scope(path)
    scope["headers"] = [(b"x-omnigent-host-token", token.encode("ascii"))]
    return scope


def _register_managed(
    store: HostStore,
    *,
    host_id: str,
    token: str,
    expires_in_s: int = 3600,
) -> None:
    """Pre-register a managed host credential for tunnel tests.

    Mirrors what the managed-launch orchestration does before the
    sandbox host dials in: an offline hosts row carrying the token
    digest.

    :param store: Host store to register into.
    :param host_id: Host id the token is scoped to.
    :param token: Raw launch token.
    :param expires_in_s: Seconds until token expiry (negative =
        already expired).
    """
    store.register_managed_host(
        host_id=host_id,
        name=f"managed-{host_id}",
        user_id="alice@example.com",
        token=token,
        provider="modal",
        sandbox_id="sb-tunnel-1",
        token_expires_at=now_epoch() + expires_in_s,
    )


async def test_managed_token_authenticates_as_record_owner(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """
    A valid launch token connects the host and flips its pre-registered
    row online under the RECORD's owner.

    The connecting sandbox presents no user credentials at all — if
    the host row's owner is anything but the token record's owner, the
    managed host would act for the wrong user (W4-class identity bug).
    """
    app, registry, store = host_app
    _register_managed(store, host_id=_HOST_ID, token="tunnel-token-ok")

    communicator = ApplicationCommunicator(app, _managed_scope(_TUNNEL_PATH, "tunnel-token-ok"))
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"

    await _send_hello_and_wait(communicator, registry, name=f"managed-{_HOST_ID}")

    host = store.get_host(_HOST_ID)
    assert host is not None
    assert host.user_id == "alice@example.com"
    assert host.status == "online"
    # The managed binding survives the connect upsert.
    assert host.sandbox_id == "sb-tunnel-1"


@pytest.mark.parametrize(
    ("record_host_id", "token", "presented_token", "expires_in_s"),
    [
        # Unknown token: no credential registered at all. Also covers
        # the junk-header fail-closed case — a stray token header must
        # never downgrade into the anonymous/local auth path.
        (None, None, "no-such-token", 3600),
        # Token scoped to a DIFFERENT host id than the path.
        ("33fc174daced5821a9bfb975aa99b086", "tunnel-token-scoped", "tunnel-token-scoped", 3600),
        # Expired token.
        (_HOST_ID, "tunnel-token-expired", "tunnel-token-expired", -1),
    ],
)
async def test_invalid_managed_token_refused_before_accept(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
    record_host_id: str | None,
    token: str | None,
    presented_token: str,
    expires_in_s: int,
) -> None:
    """
    Unknown / wrong-host / expired tokens are refused with 4004 BEFORE
    the WS handshake completes (no acceptance oracle). The
    wrong-host case is the capability scoping: a leaked token must not
    register hosts other than the one it was minted for.
    """
    app, registry, store = host_app
    if record_host_id is not None and token is not None:
        _register_managed(
            store,
            host_id=record_host_id,
            token=token,
            expires_in_s=expires_in_s,
        )

    communicator = ApplicationCommunicator(app, _managed_scope(_TUNNEL_PATH, presented_token))
    await communicator.send_input({"type": "websocket.connect"})
    closed = await communicator.receive_output(timeout=1.0)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == 4004
    # Nothing registered on this replica, and the target host never
    # came online. (The expired case pre-registers an OFFLINE row for
    # _HOST_ID — that row existing is fine; it must just stay offline.)
    assert registry.get(_HOST_ID) is None
    host = store.get_host(_HOST_ID)
    assert host is None or host.status == "offline"
