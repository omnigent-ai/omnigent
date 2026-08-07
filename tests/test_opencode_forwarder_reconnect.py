"""Tests for SSE reconnect gap-fill in OpenCodeNativeForwarder (#1778).

Verifies that after an SSE reconnect the forwarder replays persisted history so
content produced during the disconnect window is delivered exactly once, and
that content produced before the drop is never re-posted.
"""

from __future__ import annotations

from typing import Any

import httpx

import omnigent.opencode_native_forwarder as fwd_mod
from omnigent.opencode_native_client import OpenCodeEvent

_SESSION = "ses_reconnect"


class _RecordingServerClient:
    """httpx-shaped stub recording Omnigent event POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


class _FakeOpenCodeClient:
    """Fake OpenCode client for reconnect tests."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.message_snapshots: list[list[dict[str, Any]]] = []
        self._message_snapshot_index = 0
        # Each call to events() returns one iteration of this list, then stops.
        self._event_batches: list[list[OpenCodeEvent]] = []
        self._batch_index = 0

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        if self._message_snapshot_index < len(self.message_snapshots):
            messages = self.message_snapshots[self._message_snapshot_index]
            self._message_snapshot_index += 1
            return messages
        return self.messages

    async def reply_permission(self, request_id: str, reply: dict[str, Any]) -> bool:
        return True

    async def events(self):
        """Yield one batch of events per call (simulates separate SSE connections)."""
        if self._batch_index < len(self._event_batches):
            batch = self._event_batches[self._batch_index]
            self._batch_index += 1
            for ev in batch:
                yield ev


def _forwarder(
    server: _RecordingServerClient,
    opencode: _FakeOpenCodeClient,
) -> fwd_mod.OpenCodeNativeForwarder:
    return fwd_mod.OpenCodeNativeForwarder(
        session_id="conv_1",
        opencode_session_id=_SESSION,
        opencode_client=opencode,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )


def _ev(event_type: str, **props: Any) -> OpenCodeEvent:
    props.setdefault("sessionID", _SESSION)
    return OpenCodeEvent(id=None, type=event_type, properties=props, raw={})


async def test_run_seeds_on_initial_connect() -> None:
    """run() calls seed_dedupe_from_history before the first SSE consume."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {"info": {"id": "msg_old", "role": "user"}, "parts": [{"id": "prt_old", "type": "text"}]},
    ]
    fwd = _forwarder(server, opencode)

    # Make run() stop after one iteration (no reconnects).
    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=0)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]

    # The old part key should be pre-marked — a fresh event for it would be deduped.
    assert fwd.state.mark(fwd._key("text-final", "prt_old")) is False


async def test_run_catches_up_on_reconnect_posts_gap_content() -> None:
    """After an SSE disconnect the gap items reach the server exactly once.

    Scenario:
      - First connection: msg_1 is processed and dedupe-marked.
      - Connection drops; during the gap msg_2 is persisted by opencode.
      - Reconnect: catch_up_from_history() posts msg_2 from history and marks
        it. The resumed SSE stream also delivers
        msg_2; the dedupe key prevents a second post.
    """
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)

    # --- First SSE connection: deliver msg_1 part ---
    gap_message = {
        "info": {"id": "msg_2", "role": "assistant", "time": {"completed": 2}},
        "parts": [{"id": "prt_2", "type": "text", "text": "from gap"}],
    }
    opencode.message_snapshots = [
        [],
        [
            {
                "info": {"id": "msg_1", "role": "assistant", "time": {"completed": 1}},
                "parts": [{"id": "prt_1", "type": "text", "text": "hello"}],
            },
            gap_message,
        ],
    ]
    first_batch = [
        _ev("message.updated", info={"id": "msg_1", "role": "assistant"}),
        _ev(
            "message.part.updated",
            part={"id": "prt_1", "messageID": "msg_1", "type": "text", "text": "hello"},
        ),
        _ev("session.idle"),
    ]
    second_batch = [
        _ev("message.updated", info={"id": "msg_2", "role": "assistant"}),
        _ev(
            "message.part.updated",
            part={"id": "prt_2", "messageID": "msg_2", "type": "text", "text": "from gap"},
        ),
        _ev("session.idle"),
    ]
    opencode._event_batches = [first_batch, second_batch]

    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=1)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]

    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    texts = [item["data"]["item_data"]["content"][0]["text"] for item in items]
    assert texts == ["hello", "from gap"]


async def test_catch_up_called_on_reconnect_not_initial_connect() -> None:
    """Initial connect is mark-only; reconnects replay persisted history."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)

    seed_calls: list[int] = []
    catch_up_calls: list[int] = []

    async def _counting_seed() -> None:
        seed_calls.append(1)

    fwd.seed_dedupe_from_history = _counting_seed  # type: ignore[method-assign]

    async def _counting_catch_up() -> None:
        catch_up_calls.append(1)

    fwd.catch_up_from_history = _counting_catch_up  # type: ignore[method-assign]

    import httpx as _httpx

    call_count = {"n": 0}

    async def _failing_consume() -> None:
        call_count["n"] += 1
        raise _httpx.ReadError("dropped", request=_httpx.Request("GET", "http://x/event"))

    fwd._consume_once = _failing_consume  # type: ignore[method-assign]

    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=2)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]

    assert len(seed_calls) == 1
    assert len(catch_up_calls) == 2


async def test_reconnect_catches_up_user_text_and_tool_parts() -> None:
    """Reconnect catch-up replays all persisted part types the live stream mirrors."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)

    opencode.message_snapshots = [
        [],
        [
            {
                "info": {"id": "msg_u", "role": "user"},
                "parts": [
                    {
                        "id": "prt_u",
                        "messageID": "msg_u",
                        "type": "text",
                        "text": "run the command",
                    }
                ],
            },
            {
                "info": {"id": "msg_a", "role": "assistant", "time": {"completed": 2}},
                "parts": [
                    {
                        "id": "prt_t",
                        "messageID": "msg_a",
                        "type": "tool",
                        "callID": "call_1",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pwd"},
                            "output": "/workspace",
                        },
                    },
                    {
                        "id": "prt_a",
                        "messageID": "msg_a",
                        "type": "text",
                        "text": "done",
                    },
                ],
            },
        ],
    ]
    opencode._event_batches = [
        [],
        [
            _ev("message.updated", info={"id": "msg_u", "role": "user"}),
            _ev(
                "message.part.updated",
                part={
                    "id": "prt_u",
                    "messageID": "msg_u",
                    "type": "text",
                    "text": "run the command",
                },
            ),
            _ev("message.updated", info={"id": "msg_a", "role": "assistant"}),
            _ev(
                "message.part.updated",
                part={
                    "id": "prt_t",
                    "messageID": "msg_a",
                    "type": "tool",
                    "callID": "call_1",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pwd"},
                        "output": "/workspace",
                    },
                },
            ),
            _ev(
                "message.part.updated",
                part={
                    "id": "prt_a",
                    "messageID": "msg_a",
                    "type": "text",
                    "text": "done",
                },
            ),
            _ev("session.idle"),
        ],
    ]

    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=1)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]

    items = [b["data"] for _u, b in server.posts if b["type"] == "external_conversation_item"]
    item_types = [item["item_type"] for item in items]
    assert item_types == ["message", "function_call", "function_call_output", "message"]
    assert items[0]["item_data"]["role"] == "user"
    assert items[0]["item_data"]["content"][0]["text"] == "run the command"
    assert items[1]["item_data"]["name"] == "bash"
    assert items[2]["item_data"]["output"] == "/workspace"
    assert items[3]["item_data"]["role"] == "assistant"
    assert items[3]["item_data"]["content"][0]["text"] == "done"


async def test_reconnect_does_not_finalize_incomplete_assistant_text_snapshot() -> None:
    """A still-streaming assistant text snapshot must not be frozen as final."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)

    opencode.message_snapshots = [
        [],
        [
            {
                "info": {"id": "msg_streaming", "role": "assistant"},
                "parts": [
                    {
                        "id": "prt_streaming",
                        "messageID": "msg_streaming",
                        "type": "text",
                        "text": "partial",
                    }
                ],
            }
        ],
    ]
    opencode._event_batches = [
        [],
        [
            _ev("message.updated", info={"id": "msg_streaming", "role": "assistant"}),
            _ev(
                "message.part.updated",
                part={
                    "id": "prt_streaming",
                    "messageID": "msg_streaming",
                    "type": "text",
                    "text": "partial then complete",
                },
            ),
            _ev("session.idle"),
        ],
    ]

    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=1)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]

    items = [b["data"] for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert len(items) == 1
    assert items[0]["item_data"]["content"][0]["text"] == "partial then complete"


async def test_reconnect_does_not_repost_already_seeded_content() -> None:
    """Items seeded before a reconnect are not reposted after reconnect."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    # Pre-populate history with a completed item.
    opencode.messages = [
        {
            "info": {"id": "msg_pre", "role": "assistant"},
            "parts": [{"id": "prt_pre", "type": "text"}],
        }
    ]
    fwd = _forwarder(server, opencode)

    # Simulate two consecutive connections: the second delivers the same msg_pre.
    old_event = _ev(
        "message.part.updated",
        part={
            "id": "prt_pre",
            "messageID": "msg_pre",
            "type": "text",
            "text": "before disconnect",
        },
    )
    opencode._event_batches = [
        [  # first connection: carries msg_pre from before the drop
            _ev("message.updated", info={"id": "msg_pre", "role": "assistant"}),
            old_event,
            _ev("session.idle"),
        ],
        [  # second connection (reconnect): same old event arrives again
            _ev("message.updated", info={"id": "msg_pre", "role": "assistant"}),
            old_event,
            _ev("session.idle"),
        ],
    ]

    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=1)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]

    # The item was already present before the forwarder started, so neither
    # connection should repost it.
    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert items == []


async def test_handle_event_no_longer_calls_update_last_event_id() -> None:
    """The dead update_last_event_id call is removed from handle_event.

    The SSE `Last-Event-ID` resume header was never wired in opencode's
    server; calling update_last_event_id was dead code. Confirm the import
    is gone and the event is handled without touching bridge persistence.
    """
    import inspect

    import omnigent.opencode_native_forwarder as _fwd_module

    source = inspect.getsource(_fwd_module)
    assert "update_last_event_id" not in source, (
        "update_last_event_id must be removed from opencode_native_forwarder"
    )
