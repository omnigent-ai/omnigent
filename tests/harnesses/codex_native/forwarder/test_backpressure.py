"""Sub-agent delivery lanes: parent latency, shutdown draining, and usage order."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from tests.harnesses.codex_native.forwarder._support import (
    _posted_cumulative_tokens,
    _usage_event,
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
_ELICITATION_HOOK_PATH = "/hooks/codex-elicitation-request"


class _StreamingCodexClient:
    """Fake app-server client whose notifications arrive on a wall-clock schedule.

    The real client buffers socket frames independently of the consumer, so
    events are queued by the test rather than generated on demand; otherwise a
    slow consumer would throttle the producer and hide any lag.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.emitted_at: dict[str, float] = {}
        self.responses: list[tuple[int | str, dict[str, Any]]] = []

    async def connect(self) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"result": {"thread": {"id": params.get("threadId", _PARENT_THREAD), "turns": []}}}

    def is_subscribed_to_thread(self, _thread_id: str) -> bool:
        return True

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

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


def _child_thread(index: int) -> str:
    return f"thread_child_{index}"


def _spawn_children(client: _StreamingCodexClient, children: list[str]) -> None:
    client.emit(_event("turn/started", _PARENT_THREAD, _PARENT_TURN, turn={"id": _PARENT_TURN}))
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


def _emit_command(client: _StreamingCodexClient, child: str, step: int) -> None:
    item = _command_item(f"{child}-cmd-{step}")
    client.emit(_event("item/started", child, f"turn_{child}", item=item))
    client.emit(_event("item/completed", child, f"turn_{child}", item=item))


def _write_parent_bridge(bridge_dir: Path) -> None:
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=_SESSION_ID,
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id=_PARENT_THREAD,
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id=_PARENT_TURN,
        ),
    )


def _start_forwarder(
    bridge_dir: Path,
    codex: _StreamingCodexClient,
    handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
) -> asyncio.Task[None]:
    return asyncio.create_task(
        fwd.supervise_forwarder(
            base_url="http://omnigent.test",
            headers={},
            session_id=_SESSION_ID,
            bridge_dir=bridge_dir,
            app_server_url=str(bridge_dir / "app-server.sock"),
            thread_id=_PARENT_THREAD,
            client=codex,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
        )
    )


def _register_child_response(body: dict[str, Any], request: httpx.Request) -> httpx.Response:
    thread_id = body["data"]["thread_id"]
    return httpx.Response(200, json={"child_session_id": f"session_{thread_id}"}, request=request)


def _child_tool_items(
    posts: list[tuple[str, dict[str, Any]]], child: str
) -> list[tuple[str, str]]:
    return [
        (body["data"]["item_type"], body["data"]["item_data"]["call_id"])
        for path, body in posts
        if path == f"/v1/sessions/session_{child}/events"
        and body.get("type") == "external_conversation_item"
        and body["data"].get("item_type") in {"function_call", "function_call_output"}
    ]


async def test_parent_turn_completion_is_not_delayed_by_subagent_flood(tmp_path: Path) -> None:
    _write_parent_bridge(tmp_path)
    codex = _StreamingCodexClient()
    posts: list[tuple[str, dict[str, Any]]] = []
    parent_turn_cleared = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(_POST_LATENCY_SECONDS)
        body = json.loads(request.content) if request.content else {}
        posts.append((request.url.path, body))
        state = read_bridge_state(tmp_path)
        if state is not None and state.active_turn_id is None:
            parent_turn_cleared.set()
        if body.get("type") == "external_codex_subagent_start":
            return _register_child_response(body, request)
        return httpx.Response(200, json={}, request=request)

    forwarder = _start_forwarder(tmp_path, codex, handler)
    children = [_child_thread(index) for index in range(_CHILD_COUNT)]
    try:
        _spawn_children(codex, children)
        interval = _CHILD_FLOOD_SECONDS / (_CHILD_COUNT * _COMMANDS_PER_CHILD)
        for step in range(_COMMANDS_PER_CHILD):
            for child in children:
                _emit_command(codex, child, step)
                await asyncio.sleep(interval)
        codex.emit(
            _event(
                "turn/completed",
                _PARENT_THREAD,
                _PARENT_TURN,
                turn={"id": _PARENT_TURN, "status": "completed", "items": []},
            ),
            marker="parent_turn_completed",
        )
        async with asyncio.timeout(60):
            await parent_turn_cleared.wait()
        cleared_at = asyncio.get_running_loop().time()
    finally:
        codex.queue.put_nowait(None)
        await asyncio.gather(forwarder, return_exceptions=True)

    lag = cleared_at - codex.emitted_at["parent_turn_completed"]
    child_posts = sum(1 for path, _ in posts if path.startswith("/v1/sessions/session_"))
    assert lag < _MAX_PARENT_COMPLETION_LAG_SECONDS, (
        f"parent turn/completed was handled {lag:.2f}s after Codex emitted it, "
        f"behind {child_posts} sub-agent posts"
    )
    # Shutdown drains every lane, and each child's calls and outputs keep Codex order.
    for child in children:
        assert _child_tool_items(posts, child) == [
            (kind, f"{child}-cmd-{step}")
            for step in range(_COMMANDS_PER_CHILD)
            for kind in ("function_call", "function_call_output")
        ]


async def test_shutdown_drains_child_questions_before_closing_the_hook_tracker(
    tmp_path: Path,
) -> None:
    _write_parent_bridge(tmp_path)
    codex = _StreamingCodexClient()
    command_released = asyncio.Event()
    hook_requested = asyncio.Event()
    child = _child_thread(0)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith(_ELICITATION_HOOK_PATH):
            hook_requested.set()
            await asyncio.Event().wait()  # an unanswered question waits for the user
        if body.get("type") == "external_codex_subagent_start":
            return _register_child_response(body, request)
        if request.url.path == f"/v1/sessions/session_{child}/events":
            await command_released.wait()
        return httpx.Response(200, json={}, request=request)

    forwarder = _start_forwarder(tmp_path, codex, handler)
    _spawn_children(codex, [child])
    _emit_command(codex, child, 0)
    codex.emit(
        {
            "id": 41,
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": child,
                "turnId": f"turn_{child}",
                "itemId": "question",
                "questions": [{"id": "pick", "header": "Pick", "question": "Which one?"}],
            },
        }
    )
    codex.queue.put_nowait(None)
    # Release the held child post only once shutdown is already draining lanes.
    asyncio.get_running_loop().call_later(0.2, command_released.set)
    async with asyncio.timeout(10):
        await forwarder

    assert hook_requested.is_set(), "the queued child question never reached its hook"
    leaked = [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "codex-native-elicitation-hook" and not task.done()
    ]
    assert leaked == []


async def test_nested_child_usage_baseline_follows_connection_order(tmp_path: Path) -> None:
    client_posts: list[tuple[str, dict[str, Any]]] = []
    first_child_released = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/sessions/conv_c/events":
            await first_child_released.wait()
        client_posts.append((request.url.path, body))
        return httpx.Response(200, json={}, request=request)

    client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))
    state = fwd._CodexForwarderState(parent_session_id=_SESSION_ID, mcp_startup_settled=True)
    state.note_child_thread("thread_c", "conv_c")
    # G is spawned from C, so G's first frame repeats C's totals plus its own request.
    state.note_pending_child_thread("thread_g", "thread_c")
    state.note_child_thread("thread_g", "conv_g")
    tracker = fwd._CodexElicitationTaskTracker()
    events = [
        _event("item/completed", "thread_c", "turn_c", item=_command_item("c-cmd")),
        _usage_event("thread_c", (2500, 1000, 110), (2500, 1000, 110), "turn_c"),
        _usage_event("thread_g", (2800, 1000, 140), (300, 0, 30), "turn_g"),
    ]
    try:
        for event in events:
            handle = functools.partial(
                fwd._handle_event,
                client,
                session_id=_SESSION_ID,
                bridge_dir=tmp_path,
                event=event,
                usage_coalescer=fwd._SessionUsageCoalescer(client, _SESSION_ID),
                elicitation_tracker=tracker,
                expected_thread_id=_PARENT_THREAD,
                forwarder_state=state,
            )
            queued = state.enqueue_child_event(
                client,
                event,
                functools.partial(handle, usage_frame_recorded=True),
                parent_thread_id=_PARENT_THREAD,
            )
            assert queued
        async with asyncio.timeout(5):
            while not _posted_cumulative_tokens(client_posts, "conv_g"):
                await asyncio.sleep(0.01)
        first_child_released.set()
        await state.close_child_streams()
    finally:
        first_child_released.set()
        await tracker.close()
        await client.aclose()

    assert _posted_cumulative_tokens(client_posts, "conv_g") == [(300, 0, 30)]
    assert _posted_cumulative_tokens(client_posts, "conv_c") == [(2500, 1000, 110)]
