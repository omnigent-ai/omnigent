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
