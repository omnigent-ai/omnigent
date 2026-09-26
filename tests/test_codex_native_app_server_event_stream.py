"""Event-stream lifecycle regressions for the native Codex app-server client."""

from __future__ import annotations

import asyncio
import contextlib
import json

from websockets.asyncio.server import ServerConnection, serve

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient

#: Seconds within which a waiting event consumer must settle once its
#: connection is gone. Generous against CI jitter, tiny against a hang.
_SETTLE_DEADLINE = 5.0


async def _serve_probe_scenario(websocket: ServerConnection) -> None:
    """Answer the handshake; emit an event or disconnect on probe notifications."""
    async for raw in websocket:
        message = json.loads(raw)
        method = message.get("method")
        if method == "initialize":
            await websocket.send(json.dumps({"id": message["id"], "result": {}}))
        elif method == "probe/emit":
            await websocket.send(json.dumps({"method": "thread/event", "params": {}}))
        elif method == "probe/disconnect":
            await websocket.close()
            return


async def test_concurrent_event_consumers_all_settle_on_disconnect() -> None:
    """Every waiting event consumer wakes when the connection dies, not just one."""
    async with serve(_serve_probe_scenario, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        streams = [client.iter_events(), client.iter_events()]
        consumers = [asyncio.ensure_future(anext(stream)) for stream in streams]
        try:
            await asyncio.sleep(0)
            await client.notify("probe/disconnect")
            done, _ = await asyncio.wait(consumers, timeout=_SETTLE_DEADLINE)
            still_blocked = len(consumers) - len(done)
            assert still_blocked == 0, (
                f"{still_blocked} of {len(consumers)} event consumers still blocked "
                f"{_SETTLE_DEADLINE}s after the app-server disconnect"
            )
        finally:
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            for stream in streams:
                await stream.aclose()
            await client.close()


async def test_reconnected_client_streams_live_events() -> None:
    """A reconnected client's consumers get live events, not the old stream's end."""
    async with serve(_serve_probe_scenario, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")

        await client.connect()
        first_stream = client.iter_events()
        first = asyncio.ensure_future(anext(first_stream))
        try:
            await asyncio.sleep(0)
            await client.notify("probe/disconnect")
            done, _ = await asyncio.wait({first}, timeout=_SETTLE_DEADLINE)
            assert first in done, (
                f"event consumer still blocked {_SETTLE_DEADLINE}s after the "
                "app-server disconnect"
            )
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            await first_stream.aclose()
            await client.close()

        await client.connect()
        second_stream = client.iter_events()
        second = asyncio.ensure_future(anext(second_stream))
        try:
            await asyncio.sleep(0)
            await client.notify("probe/emit")
            done, _ = await asyncio.wait({second}, timeout=_SETTLE_DEADLINE)
            assert second in done, "reconnected consumer never received the live event"
            assert second.exception() is None, (
                "the reconnected stream ended instead of yielding the live event "
                "the new connection sent"
            )
            assert second.result().get("method") == "thread/event"
        finally:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            await second_stream.aclose()
            with contextlib.suppress(Exception):
                await client.close()
