"""Tests for the per-connection model-options cache and single-flight."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes import _host_model_options
from omnigent.server.routes._host_model_options import cached_host_model_options


@dataclass
class FakeWebSocket:
    """Minimal WebSocket fake that records sent frames."""

    sent: list[str] = field(default_factory=list)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:  # pragma: no cover - tests never read
        await asyncio.sleep(3600)
        return ""


def _make_conn(registry: HostRegistry, name: str = "host_a") -> HostConnection:
    hello = HostHelloFrame(version="0.1.0", frame_protocol_version=1, name=name)
    return registry.register(name, FakeWebSocket(), hello, owner="alice")


async def _answer_pending(conn: HostConnection, result: dict[str, Any]) -> int:
    """Resolve every queued probe by draining sent frames and completing futures.

    Yields to the loop until at least one probe frame is enqueued (the probe
    task reaches ``send_text`` after its own scheduling turns), then drains all.

    :returns: How many probe frames were answered (i.e. host round-trips).
    """
    for _ in range(100):
        if not conn.outbound_queue.empty():
            break
        await asyncio.sleep(0)
    answered = 0
    # Frames land on the outbound queue via send_text; a real sender loop would
    # transmit them. Drain them here and resolve the matching pending future.
    while not conn.outbound_queue.empty():
        frame = conn.outbound_queue.get_nowait()
        assert frame is not None
        payload = json.loads(frame)
        request_id = payload["request_id"]
        future = conn.pending_model_options.get(request_id)
        assert future is not None, "probe frame has no pending future"
        future.set_result(result)
        answered += 1
    return answered


@pytest.mark.asyncio
async def test_cache_serves_repeat_without_reprobe() -> None:
    """A second call within the TTL is served from cache, no host round-trip."""
    registry = HostRegistry()
    conn = _make_conn(registry)
    ok = {"status": "ok", "models": [{"id": "m1"}]}

    task = asyncio.ensure_future(
        cached_host_model_options(
            host_registry=registry, host_conn=conn, harness="claude-native", timeout_s=5.0
        )
    )
    await asyncio.sleep(0)  # let the probe enqueue its frame
    assert await _answer_pending(conn, ok) == 1
    assert await task == ok

    # Second call: cache is warm, so no frame is sent and no future is created.
    result = await cached_host_model_options(
        host_registry=registry, host_conn=conn, harness="claude-native", timeout_s=5.0
    )
    assert result == ok
    assert conn.outbound_queue.empty()


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_probe() -> None:
    """Two concurrent calls for one harness coalesce onto a single round-trip."""
    registry = HostRegistry()
    conn = _make_conn(registry)
    ok = {"status": "ok", "models": []}

    tasks = [
        asyncio.ensure_future(
            cached_host_model_options(
                host_registry=registry, host_conn=conn, harness="codex-native", timeout_s=5.0
            )
        )
        for _ in range(3)
    ]
    await asyncio.sleep(0)
    assert await _answer_pending(conn, ok) == 1  # one frame for three callers
    assert await asyncio.gather(*tasks) == [ok, ok, ok]


@pytest.mark.asyncio
async def test_error_results_are_not_cached() -> None:
    """A non-ok result re-probes on the next call instead of being cached."""
    registry = HostRegistry()
    conn = _make_conn(registry)
    err = {"status": "error", "error": "boot probe racing"}

    task = asyncio.ensure_future(
        cached_host_model_options(
            host_registry=registry, host_conn=conn, harness="pi-native", timeout_s=5.0
        )
    )
    await asyncio.sleep(0)
    assert await _answer_pending(conn, err) == 1
    assert await task == err
    assert "pi-native" not in conn.model_options_cache

    # Next call must probe again (the error was not cached).
    task2 = asyncio.ensure_future(
        cached_host_model_options(
            host_registry=registry, host_conn=conn, harness="pi-native", timeout_s=5.0
        )
    )
    await asyncio.sleep(0)
    assert await _answer_pending(conn, {"status": "ok", "models": []}) == 1
    assert (await task2)["status"] == "ok"


@pytest.mark.asyncio
async def test_expired_entry_reprobes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the TTL lapses, the next call probes the host again."""
    registry = HostRegistry()
    conn = _make_conn(registry)
    ok = {"status": "ok", "models": []}

    clock = {"now": 1_000.0}
    monkeypatch.setattr(_host_model_options.time, "monotonic", lambda: clock["now"])

    task = asyncio.ensure_future(
        cached_host_model_options(
            host_registry=registry, host_conn=conn, harness="claude-native", timeout_s=5.0
        )
    )
    await asyncio.sleep(0)
    assert await _answer_pending(conn, ok) == 1
    assert await task == ok

    clock["now"] += _host_model_options._MODEL_OPTIONS_CACHE_TTL_S + 1.0

    task2 = asyncio.ensure_future(
        cached_host_model_options(
            host_registry=registry, host_conn=conn, harness="claude-native", timeout_s=5.0
        )
    )
    await asyncio.sleep(0)
    assert await _answer_pending(conn, ok) == 1  # re-probed after expiry
    assert await task2 == ok
