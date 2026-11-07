"""Parent-turn latency while sub-agent threads flood the shared app-server stream."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)

_SESSION_ID = "conv_parent"
_PARENT_THREAD = "thread_parent"
_PARENT_TURN = "turn_parent"
# Round trip from a managed sandbox to the Omnigent server is ~120 ms in prod;
# scaled down so the test stays fast while inflow still exceeds serial capacity.
_POST_LATENCY_SECONDS = 0.01
_CHILD_COUNT = 20
_COMMANDS_PER_CHILD = 15
_CHILD_FLOOD_SECONDS = 0.5
# Generous for a concurrent forwarder; a serial one needs ~600 posts x 10 ms.
_MAX_PARENT_COMPLETION_LAG_SECONDS = 1.0


class _StreamingCodexClient:
    """Fake app-server client whose notifications arrive on a wall-clock schedule.

    The real client buffers socket frames independently of the consumer, so
    events are queued by a producer task rather than generated on demand;
    otherwise a slow consumer would throttle the producer and hide any lag.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.emitted_at: dict[str, float] = {}
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        return {"result": {"thread": {"id": params.get("threadId", _PARENT_THREAD), "turns": []}}}

    def is_subscribed_to_thread(self, _thread_id: str) -> bool:
        return True

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None

    def emit(self, event: dict[str, Any], *, marker: str | None = None) -> None:
        if marker is not None:
            self.emitted_at[marker] = asyncio.get_running_loop().time()
        self.queue.put_nowait(event)

    async def iter_events(self) -> Any:
        while (event := await self.queue.get()) is not None:
            yield event


def _event(method: str, thread_id: str, turn_id: str, **params: Any) -> dict[str, Any]:
    return {"method": method, "params": {"threadId": thread_id, "turnId": turn_id, **params}}


def _command_item(item_id: str) -> dict[str, Any]:
    return {
        "type": "commandExecution",
        "id": item_id,
        "command": "echo step",
        "cwd": "/tmp",
        "status": "completed",
        "aggregatedOutput": "step\n",
        "exitCode": 0,
    }


async def _produce(client: _StreamingCodexClient) -> None:
    client.emit(_event("turn/started", _PARENT_THREAD, _PARENT_TURN, turn={"id": _PARENT_TURN}))
    children = [f"thread_child_{index}" for index in range(_CHILD_COUNT)]
    client.emit(
        _event(
            "item/started",
            _PARENT_THREAD,
            _PARENT_TURN,
            item={
                "type": "collabAgentToolCall",
                "id": "spawn",
                "tool": "spawnAgent",
                "status": "inProgress",
                "senderThreadId": _PARENT_THREAD,
                "receiverThreadIds": children,
            },
        )
    )
    total = _CHILD_COUNT * _COMMANDS_PER_CHILD
    interval = _CHILD_FLOOD_SECONDS / total
    for step in range(_COMMANDS_PER_CHILD):
        for child in children:
            item = _command_item(f"{child}-cmd-{step}")
            child_turn = f"turn_{child}"
            client.emit(_event("item/started", child, child_turn, item=item))
            client.emit(_event("item/completed", child, child_turn, item=item))
            await asyncio.sleep(interval)
    client.emit(
        _event(
            "turn/completed",
            _PARENT_THREAD,
            _PARENT_TURN,
            turn={"id": _PARENT_TURN, "status": "completed", "items": []},
        ),
        marker="parent_turn_completed",
    )


async def test_parent_turn_completion_is_not_delayed_by_subagent_flood(tmp_path: Path) -> None:
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id=_SESSION_ID,
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id=_PARENT_THREAD,
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=_PARENT_TURN,
        ),
    )
    codex = _StreamingCodexClient()
    posts: list[tuple[float, str, dict[str, Any]]] = []
    parent_turn_cleared = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(_POST_LATENCY_SECONDS)
        body = json.loads(request.content) if request.content else {}
        posts.append((asyncio.get_running_loop().time(), request.url.path, body))
        state = read_bridge_state(tmp_path)
        if state is not None and state.active_turn_id is None:
            parent_turn_cleared.set()
        if body.get("type") == "external_codex_subagent_start":
            thread_id = body["data"]["thread_id"]
            return httpx.Response(200, json={"child_session_id": f"session_{thread_id}"})
        return httpx.Response(200, json={})

    forwarder = asyncio.create_task(
        fwd.supervise_forwarder(
            base_url="http://omnigent.test",
            headers={},
            session_id=_SESSION_ID,
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id=_PARENT_THREAD,
            client=codex,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
        )
    )
    try:
        await _produce(codex)
        async with asyncio.timeout(60):
            await parent_turn_cleared.wait()
        cleared_at = asyncio.get_running_loop().time()
    finally:
        codex.queue.put_nowait(None)
        forwarder.cancel()
        await asyncio.gather(forwarder, return_exceptions=True)

    child_posts = sum(1 for _, path, _ in posts if path.startswith("/v1/sessions/session_"))
    lag = cleared_at - codex.emitted_at["parent_turn_completed"]
    assert lag < _MAX_PARENT_COMPLETION_LAG_SECONDS, (
        f"parent turn/completed was handled {lag:.2f}s after Codex emitted it, "
        f"behind {child_posts} sub-agent posts"
    )
    # Shutdown drains every lane, and each child's tool calls keep Codex order.
    for index in range(_CHILD_COUNT):
        child = f"thread_child_{index}"
        call_ids = [
            body["data"]["item_data"]["call_id"]
            for _, path, body in posts
            if path == f"/v1/sessions/session_{child}/events"
            and body.get("type") == "external_conversation_item"
            and body["data"].get("item_type") == "function_call"
        ]
        assert call_ids == [f"{child}-cmd-{step}" for step in range(_COMMANDS_PER_CHILD)]
