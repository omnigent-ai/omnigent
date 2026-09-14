"""Regression: host startup must not block on the orphan bridge-dir reap.

The user-observable journey is: run ``omnigent host`` on a machine that has
accumulated stale native-harness bridge dirs (left by prior runners that died
uncleanly) and wait for the host to come online. On a box with many leftover
bridge dirs the host takes a long time to register, because
``HostProcess.run()`` performs the cross-harness orphan bridge-dir sweep
(``reap_orphaned_native_bridge_dirs`` — which ``os.walk``s a per-dir session
tree for the codex harness) *inline*, ``await``-ing it to completion **before**
it enters the connect loop that registers the host with the server. So the
sweep sits squarely on the critical startup path and its cost is added to the
host's time-to-online.

This test drives the real ``HostProcess.run()`` with the WebSocket transport
stubbed and the orphan sweep replaced by one that blocks until the test
releases it. It asserts that the host reaches its first connect attempt (the
step that registers it) *while the sweep is still running* — i.e. the sweep is
off the critical path. On the buggy code the connect attempt is never reached
until the sweep returns, so the wait times out and the test fails. Once the
sweep is launched as a background task (the fix) the connect attempt is reached
immediately and the test passes.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest

from omnigent.host.connect import HostProcess
from omnigent.host.identity import HostIdentity


class _ConnectReachedThenStop:
    """Async-CM stand-in for ``websockets.asyncio.client.connect``.

    Signals (on the event loop) that the host reached its connect attempt —
    the step that registers it with the server — then raises
    ``CancelledError`` from ``__aenter__`` so ``HostProcess.run()`` breaks out
    of its connect loop and returns cleanly.
    """

    def __init__(self, reached: asyncio.Event) -> None:
        self._reached = reached

    async def __aenter__(self) -> object:
        self._reached.set()
        raise asyncio.CancelledError()

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def test_host_start_does_not_block_on_orphan_bridge_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orphan bridge-dir sweep must not gate the host's registration.

    A slow ``reap_orphaned_native_bridge_dirs`` must not delay ``omnigent
    host`` coming online. Asserts the host reaches its connect attempt while
    the sweep is still in flight.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)

    # Isolate the WS connect + auth so no real network/credentials are touched.
    import websockets.asyncio.client as ws_client

    import omnigent.runner._entry as entry_mod

    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)

    connect_reached = asyncio.Event()

    def _fake_connect(url: str, **kwargs: object) -> _ConnectReachedThenStop:
        return _ConnectReachedThenStop(connect_reached)

    monkeypatch.setattr(ws_client, "connect", _fake_connect)

    # A sweep that blocks in its worker thread (it runs via asyncio.to_thread)
    # until the test releases it, standing in for a genuinely slow sweep on a
    # box with many stale bridge dirs.
    sweep_started = threading.Event()
    sweep_release = threading.Event()

    def _blocking_sweep() -> int:
        sweep_started.set()
        # Bounded so a hung test still tears down instead of blocking forever.
        sweep_release.wait(timeout=30.0)
        return 0

    monkeypatch.setattr(
        "omnigent.native.native_bridge_common.reap_orphaned_native_bridge_dirs",
        _blocking_sweep,
    )

    identity = HostIdentity(host_id="host_bridge_reap", name="repro-laptop")
    host = HostProcess(identity, "https://app.example.databricks.com")
    # Skip live capability discovery — irrelevant to this ordering and it would
    # otherwise probe local harnesses on startup.
    host._capabilities_initialized = True

    run_task = asyncio.create_task(host.run())
    reached = False
    try:
        # The sweep runs in a worker thread and is held blocked. On the fixed
        # code the connect attempt is reached promptly (sweep is off the
        # critical path); on the buggy code run() is parked awaiting the sweep
        # and never reaches connect, so this times out.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(connect_reached.wait(), timeout=10.0)
            reached = True
    finally:
        sweep_release.set()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await run_task

    assert sweep_started.is_set(), "the orphan bridge-dir sweep never ran"
    assert reached, (
        "host startup blocked on the orphan bridge-dir sweep: the connect "
        "attempt (host registration) was not reached while the sweep was "
        "still in flight"
    )
