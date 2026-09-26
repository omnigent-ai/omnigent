"""Tests for SSE reconnect gap-fill in OpenCodeNativeForwarder.

After an SSE reconnect the forwarder re-reads v2 history past its cursor so
content produced during the disconnect window is delivered exactly once, and
content produced before the drop is never re-posted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from tests.opencode_v2_fixtures import load_messages

_SESSION = "ses_reconnect"


class _RecordingServerClient:
    """httpx-shaped stub recording Omnigent event POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


class _FakeOpenCodeClient:
    """Fake v2 OpenCode client: one event batch per SSE connection."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.message_snapshots: list[list[dict[str, Any]]] = []
        self._message_snapshot_index = 0
        self.after_ids: list[str | None] = []
        self._event_batches: list[list[OpenCodeEvent]] = []
        self._batch_index = 0

    async def list_messages(
        self, session_id: str, *, after_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.after_ids.append(after_id)
        if self._message_snapshot_index < len(self.message_snapshots):
            messages = self.message_snapshots[self._message_snapshot_index]
            self._message_snapshot_index += 1
        else:
            messages = self.messages
        if after_id is None:
            return messages
        ids = [m.get("id") for m in messages]
        return messages[ids.index(after_id) + 1 :] if after_id in ids else messages

    async def reply_permission(
        self, session_id: str, request_id: str, decision: str, message: str | None = None
    ) -> bool:
        return True

    async def stream_events(self) -> AsyncIterator[OpenCodeEvent]:
        """Yield one batch of events per call (simulates separate SSE connections)."""
        if self._batch_index < len(self._event_batches):
            batch = self._event_batches[self._batch_index]
            self._batch_index += 1
            for event in batch:
                yield event


def _forwarder(
    server: _RecordingServerClient,
    opencode: _FakeOpenCodeClient,
    *,
    opencode_session_id: str = _SESSION,
) -> fwd_mod.OpenCodeNativeForwarder:
    return fwd_mod.OpenCodeNativeForwarder(
        session_id="conv_1",
        opencode_session_id=opencode_session_id,
        opencode_client=opencode,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )


def _ev(event_type: str, **data: Any) -> OpenCodeEvent:
    data.setdefault("sessionID", _SESSION)
    return OpenCodeEvent(id=None, type=event_type, data=data, location=None)


def _assistant(
    message_id: str, *content: dict[str, Any], completed: bool = True
) -> dict[str, Any]:
    time_info: dict[str, Any] = {"created": 1}
    if completed:
        time_info["completed"] = 2
    return {
        "id": message_id,
        "type": "assistant",
        "agent": "build",
        "model": {"id": "m", "providerID": "p"},
        "content": list(content),
        "time": time_info,
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _live_text_turn(message_id: str, text: str) -> list[OpenCodeEvent]:
    """The live frames for one assistant step that says *text*."""
    return [
        _ev("session.execution.started"),
        _ev(
            "session.step.started",
            assistantMessageID=message_id,
            agent="build",
            model={"id": "m", "providerID": "p"},
            started=1,
        ),
        _ev("session.text.ended", assistantMessageID=message_id, ordinal=0, text=text),
        _ev(
            "session.step.ended",
            assistantMessageID=message_id,
            finish="stop",
            cost=0.0,
            tokens={"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        ),
        _ev("session.execution.succeeded"),
    ]


async def _run(fwd: fwd_mod.OpenCodeNativeForwarder, max_reconnects: int) -> None:
    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=max_reconnects)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]


def _assistant_texts(server: _RecordingServerClient) -> list[str]:
    return [
        body["data"]["item_data"]["content"][0]["text"]
        for _u, body in server.posts
        if body["type"] == "external_conversation_item"
        and body["data"]["item_data"].get("role") == "assistant"
    ]


async def test_handle_event_no_longer_calls_update_last_event_id() -> None:
    """The SSE ``Last-Event-ID`` resume path stays unused."""
    import inspect

    source = inspect.getsource(fwd_mod)
    assert "update_last_event_id" not in source


async def test_run_seeds_on_initial_connect() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [_assistant("msg_old", _text("old answer"))]
    fwd = _forwarder(server, opencode)
    await _run(fwd, max_reconnects=0)
    assert fwd.state.mark(fwd._key("text-final", "msg_old", "0")) is False
    assert opencode.after_ids == [None]


async def test_run_catches_up_on_reconnect_posts_gap_content() -> None:
    """msg_1 arrives live; msg_2 lands during the drop and is replayed exactly once."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    msg_1 = _assistant("msg_1", _text("hello"))
    msg_2 = _assistant("msg_2", _text("from gap"))
    opencode.message_snapshots = [[], [msg_1, msg_2]]
    opencode._event_batches = [
        _live_text_turn("msg_1", "hello"),
        _live_text_turn("msg_2", "from gap"),
    ]
    await _run(fwd, max_reconnects=1)
    assert _assistant_texts(server) == ["hello", "from gap"]


async def test_catch_up_passes_cursor_as_after_id() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {"id": "msg_u", "type": "user", "text": "hi", "time": {"created": 1}},
        _assistant("msg_1", _text("answer")),
    ]
    fwd = _forwarder(server, opencode)
    await _run(fwd, max_reconnects=1)
    assert opencode.after_ids == [None, "msg_1"]


async def test_catch_up_called_on_reconnect_not_initial_connect() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    seed_calls: list[int] = []
    catch_up_calls: list[int] = []

    async def _counting_seed() -> None:
        seed_calls.append(1)

    async def _counting_catch_up() -> None:
        catch_up_calls.append(1)

    fwd.seed_dedupe_from_history = _counting_seed  # type: ignore[method-assign]
    fwd.catch_up_from_history = _counting_catch_up  # type: ignore[method-assign]

    async def _failing_consume() -> None:
        raise httpx.ReadError("dropped", request=httpx.Request("GET", "http://x/api/event"))

    fwd._consume_once = _failing_consume  # type: ignore[method-assign]
    await _run(fwd, max_reconnects=2)
    assert len(seed_calls) == 1
    assert len(catch_up_calls) == 2


async def test_reconnect_catches_up_user_text_and_tool_items() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    opencode.message_snapshots = [
        [],
        [
            {"id": "msg_u", "type": "user", "text": "run the command", "time": {"created": 1}},
            _assistant(
                "msg_a",
                {
                    "type": "tool",
                    "id": "call_1",
                    "name": "shell",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pwd"},
                        "content": [{"type": "text", "text": "/workspace"}],
                    },
                    "time": {"created": 1, "completed": 2},
                },
                _text("done"),
            ),
        ],
    ]
    opencode._event_batches = [[], []]
    await _run(fwd, max_reconnects=1)
    items = [b["data"] for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert [i["item_type"] for i in items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[0]["item_data"]["role"] == "user"
    assert items[0]["item_data"]["content"][0]["text"] == "run the command"
    assert items[1]["item_data"]["name"] == "shell"
    assert items[2]["item_data"]["output"] == "/workspace"
    assert items[3]["item_data"]["content"][0]["text"] == "done"
    assert items[3]["message_id"] == "opencode:msg_a:text:0"


async def test_catch_up_keeps_cursor_before_incomplete_message() -> None:
    """An in-flight assistant message is re-read on the next catch-up."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    opencode.message_snapshots = [
        [],
        [
            _assistant("msg_done", _text("first")),
            _assistant("msg_live", _text("partial"), completed=False),
        ],
    ]
    opencode._event_batches = [[], []]
    await _run(fwd, max_reconnects=1)
    assert fwd._last_seen_message_id == "msg_done"


async def test_reconnect_does_not_repost_already_seeded_content() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [_assistant("msg_pre", _text("before disconnect"))]
    fwd = _forwarder(server, opencode)
    opencode._event_batches = [
        _live_text_turn("msg_pre", "before disconnect"),
        _live_text_turn("msg_pre", "before disconnect"),
    ]
    await _run(fwd, max_reconnects=1)
    assert _assistant_texts(server) == []


async def test_fixture_messages_replay_on_catch_up() -> None:
    """The captured ``GET /api/session/{id}/message`` body replays every text once."""
    body = load_messages()
    messages = body["data"]
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.message_snapshots = [[], messages]
    opencode._event_batches = [[], []]
    fwd = _forwarder(server, opencode)
    await _run(fwd, max_reconnects=1)
    expected = [
        item["text"]
        for message in messages
        if message.get("type") == "assistant"
        for item in message.get("content", [])
        if item.get("type") == "text" and item.get("text")
    ]
    assert _assistant_texts(server) == expected


async def test_catch_up_posts_call_for_running_tool_and_live_success_completes_it() -> None:
    """A tool ``running`` at catch-up gets its call posted; output waits for the live event."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    running_tool = {
        "type": "tool",
        "id": "call_1",
        "name": "shell",
        "state": {"status": "running", "input": {"command": "pwd"}, "metadata": {}},
    }
    opencode.message_snapshots = [[], [_assistant("msg_a", running_tool, completed=False)]]
    opencode._event_batches = [
        [],
        [
            _ev(
                "session.tool.success",
                assistantMessageID="msg_a",
                id="call_1",
                content=[{"type": "text", "text": "/workspace"}],
            )
        ],
    ]
    await _run(fwd, max_reconnects=1)
    items = [b["data"] for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert [i["item_type"] for i in items] == ["function_call", "function_call_output"]
    assert items[1]["item_data"]["output"] == "/workspace"


async def test_handler_exception_does_not_reconnect_and_continues_batch() -> None:
    """A raising handler is isolated: no reconnect, later events in the batch still post."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    original = fwd_mod._HANDLERS["session.text.ended"]

    async def _boom(self: fwd_mod.OpenCodeNativeForwarder, event: OpenCodeEvent) -> None:
        raise RuntimeError("handler exploded")

    fwd_mod._HANDLERS["session.text.ended"] = _boom
    try:
        opencode._event_batches = [_live_text_turn("msg_1", "hello")]
        await _run(fwd, max_reconnects=0)
    finally:
        fwd_mod._HANDLERS["session.text.ended"] = original

    # The raising handler drops the text, but later events in the same batch
    # (step.ended, execution.succeeded) still land and no reconnect happens.
    assert _assistant_texts(server) == []
    statuses = [
        body["data"]["status"]
        for _u, body in server.posts
        if body["type"] == "external_session_status"
    ]
    assert "idle" in statuses
    assert opencode.after_ids == [None]


async def test_catch_up_skips_streaming_tool() -> None:
    """A tool still ``streaming`` at catch-up posts nothing; the live call lands once."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    streaming_tool = {
        "type": "tool",
        "id": "call_1",
        "name": "shell",
        "state": {"status": "streaming", "input": "pw"},
    }
    opencode.message_snapshots = [[], [_assistant("msg_a", streaming_tool, completed=False)]]
    opencode._event_batches = [
        [],
        [
            _ev(
                "session.tool.input.started",
                assistantMessageID="msg_a",
                id="call_1",
                name="shell",
            ),
            _ev(
                "session.tool.called",
                assistantMessageID="msg_a",
                id="call_1",
                input={"command": "pwd"},
                executed=True,
            ),
        ],
    ]
    await _run(fwd, max_reconnects=1)
    items = [b["data"] for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert [i["item_type"] for i in items] == ["function_call"]
