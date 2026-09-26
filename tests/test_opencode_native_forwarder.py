"""Tests for the OpenCode v2 event -> Omnigent event forwarder translation."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeClientError, OpenCodeEvent
from tests.opencode_v2_fixtures import events_of_type, load_events, load_messages

_SESSION = "ses_1"
# The captured turn's own OpenCode session id.
_FIX_SESSION: str = events_of_type("session.execution.started")[0]["data"]["sessionID"]


class _RecordingServerClient:
    """httpx-shaped stub recording Omnigent event POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.hook_response: dict[str, Any] | None = None
        self.child_conversation_id = "conv_child_1"

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        request = httpx.Request("POST", url)
        if url.endswith("/hooks/native-permission-request") and self.hook_response is not None:
            return httpx.Response(200, json=self.hook_response, request=request)
        if json.get("type") == "external_subagent_start":
            body = {"queued": False, "child_session_id": self.child_conversation_id}
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(200, request=request)


class _FakeOpenCodeClient:
    """Fake v2 OpenCode client recording replies and serving history."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.after_ids: list[str | None] = []
        self.permission_replies: list[tuple[str, str, str]] = []
        self.form_replies: list[tuple[str, str, dict[str, Any]]] = []
        self.form_cancels: list[tuple[str, str]] = []
        self.stream: list[OpenCodeEvent] = []

    async def list_messages(
        self, session_id: str, *, after_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.after_ids.append(after_id)
        return self.messages

    async def reply_permission(
        self, session_id: str, request_id: str, decision: str, message: str | None = None
    ) -> bool:
        self.permission_replies.append((session_id, request_id, decision))
        return True

    async def reply_form(self, session_id: str, form_id: str, answer: dict[str, Any]) -> bool:
        self.form_replies.append((session_id, form_id, answer))
        return True

    async def cancel_form(self, session_id: str, form_id: str) -> bool:
        self.form_cancels.append((session_id, form_id))
        return True

    async def stream_events(self) -> AsyncIterator[OpenCodeEvent]:
        for event in self.stream:
            yield event


def _forwarder(
    server: _RecordingServerClient,
    opencode: _FakeOpenCodeClient,
    *,
    opencode_session_id: str = _SESSION,
    **kwargs: Any,
) -> fwd_mod.OpenCodeNativeForwarder:
    return fwd_mod.OpenCodeNativeForwarder(
        session_id="conv_1",
        opencode_session_id=opencode_session_id,
        opencode_client=opencode,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
        **kwargs,
    )


def _event(event_type: str, **data: Any) -> OpenCodeEvent:
    """Hand-build a v2 ``/api/event`` frame for this test's session."""
    data.setdefault("sessionID", _SESSION)
    return OpenCodeEvent(id=None, type=event_type, data=data, location=None)


def _to_event(raw: dict[str, Any]) -> OpenCodeEvent:
    """Convert a captured ``/api/event`` frame into an ``OpenCodeEvent``."""
    return OpenCodeEvent(
        id=raw.get("id"),
        type=raw["type"],
        data=dict(raw.get("data") or {}),
        location=raw.get("location"),
    )


def _fixture(event_type: str, index: int = 0) -> OpenCodeEvent:
    """The *index*-th captured event of *event_type* (for ``_FIX_SESSION``)."""
    return _to_event(events_of_type(event_type)[index])


def _types(posts: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [body["type"] for _url, body in posts]


def _datas(posts: list[tuple[str, dict[str, Any]]], event_type: str) -> list[dict[str, Any]]:
    return [body["data"] for _url, body in posts if body.get("type") == event_type]


def _status_edges(posts: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return _datas(posts, "external_session_status")


def _items(posts: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return _datas(posts, "external_conversation_item")


def _hook_post(server: _RecordingServerClient) -> dict[str, Any] | None:
    """Return the body of the native-permission-request hook POST, if any."""
    for url, body in server.posts:
        if url.endswith("/hooks/native-permission-request"):
            return body
    return None


async def _drain(fwd: fwd_mod.OpenCodeNativeForwarder) -> None:
    """Await every background permission / form task the forwarder spawned."""
    tasks = [*fwd._permission_tasks.values(), *fwd._form_tasks.values()]
    await asyncio.gather(*tasks)


def _step_started(message_id: str, **data: Any) -> OpenCodeEvent:
    data.setdefault("agent", "build")
    data.setdefault("model", {"id": "claude-sonnet-4-5", "providerID": "anthropic"})
    data.setdefault("started", 1)
    return _event("session.step.started", assistantMessageID=message_id, **data)


def _step_ended(message_id: str, **data: Any) -> OpenCodeEvent:
    data.setdefault("finish", "stop")
    data.setdefault("cost", 0.0)
    data.setdefault(
        "tokens", {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    )
    return _event("session.step.ended", assistantMessageID=message_id, **data)


# --- filtering / dispatch ---------------------------------------------------


async def test_unknown_event_is_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("some.unknown.event", foo="bar"))
    assert server.posts == []


async def test_event_for_other_session_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    event = _event("session.execution.started", sessionID="ses_OTHER")
    assert fwd._event_targets_session(event) is False
    await fwd.handle_event(event)
    assert server.posts == []


async def test_event_without_session_id_passes_filter() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    connected = OpenCodeEvent(id="evt_1", type="server.connected", data={}, location=None)
    assert fwd._event_targets_session(connected) is True


async def test_form_created_filters_on_nested_session_id() -> None:
    """``form.created`` carries the session id under ``data.form``."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    ours = OpenCodeEvent(
        id=None,
        type="form.created",
        data={"form": {"id": "frm_1", "sessionID": _SESSION}},
        location=None,
    )
    theirs = OpenCodeEvent(
        id=None,
        type="form.created",
        data={"form": {"id": "frm_2", "sessionID": "ses_X"}},
        location=None,
    )
    assert fwd._event_targets_session(ours) is True
    assert fwd._event_targets_session(theirs) is False


async def test_consume_once_dispatches_stream_events() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    seen: list[str] = []

    async def _record(event: OpenCodeEvent) -> None:
        seen.append(event.type)

    fwd.handle_event = _record  # type: ignore[method-assign]
    opencode.stream = [_event("session.status", status={"type": "busy"})]
    await fwd._consume_once()
    assert seen == ["session.status"]


async def test_run_reconnects_until_cap() -> None:
    """run() retries the SSE consume loop and stops at the reconnect cap."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    calls = {"n": 0}

    async def failing_consume() -> None:
        calls["n"] += 1
        raise httpx.ReadError("dropped", request=httpx.Request("GET", "http://x/api/event"))

    fwd._consume_once = failing_consume  # type: ignore[method-assign]

    async def _no_sleep(_seconds: float) -> None:
        return None

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=3)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert calls["n"] == 4  # initial + 3 reconnects


# --- turn lifecycle ---------------------------------------------------------


async def test_lifecycle_emits_running_then_idle() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [(e["status"], e["response_id"]) for e in edges] == [
        ("running", "msg_1"),
        ("idle", "msg_1"),
    ]


async def test_running_edge_deferred_until_step_started() -> None:
    """``session.status busy`` opens the turn; the edge waits for the assistant id."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.status", status={"type": "busy"}))
    assert _status_edges(server.posts) == []
    await fwd.handle_event(_step_started("msg_1"))
    running = [e for e in _status_edges(server.posts) if e["status"] == "running"]
    assert running == [{"status": "running", "response_id": "msg_1"}]


async def test_multi_step_turn_keeps_first_response_id() -> None:
    """Each step has its own assistant id; the turn keeps the id that went live."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_step_started("msg_2"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [(e["status"], e["response_id"]) for e in edges] == [
        ("running", "msg_1"),
        ("idle", "msg_1"),
    ]


async def test_second_turn_gets_its_own_running_response_id() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    for msg in ("msg_a", "msg_b"):
        await fwd.handle_event(_event("session.execution.started"))
        await fwd.handle_event(_step_started(msg))
        await fwd.handle_event(_event("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [(e["status"], e["response_id"]) for e in edges] == [
        ("running", "msg_a"),
        ("idle", "msg_a"),
        ("running", "msg_b"),
        ("idle", "msg_b"),
    ]


async def test_turn_without_step_idles_with_session_fallback() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.status", status={"type": "busy"}))
    await fwd.handle_event(_event("session.status", status={"type": "idle"}))
    edges = _status_edges(server.posts)
    assert edges == [{"status": "idle", "response_id": _SESSION}]


async def test_status_idle_after_execution_succeeded_posts_one_idle() -> None:
    """v2 emits both ``execution.succeeded`` and ``status idle``; idle posts once."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    await fwd.handle_event(_event("session.status", status={"type": "idle"}))
    assert [e["status"] for e in _status_edges(server.posts)] == ["running", "idle"]


async def test_step_started_records_active_message_id_in_bridge(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[tuple[str | None, str]] = []

    def _record(bridge_dir: Path, message_id: str | None, *, status: str) -> None:
        calls.append((message_id, status))

    monkeypatch.setattr(fwd_mod, "update_active_message_id", _record)
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, bridge_dir=tmp_path)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    assert calls == [("msg_1", "busy"), (None, "idle")]


async def test_fixture_turn_opens_and_closes() -> None:
    """The captured execution/step edges drive one running + one idle edge."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    step = _fixture("session.step.started")
    await fwd.handle_event(_fixture("session.execution.started"))
    await fwd.handle_event(step)
    await fwd.handle_event(_fixture("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [e["status"] for e in edges] == ["running", "idle"]
    assert edges[0]["response_id"] == step.data["assistantMessageID"]


# --- text streaming ---------------------------------------------------------


async def test_text_delta_streams_live_preview_chunks() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for chunk in ("Hel", "lo"):
        await fwd.handle_event(
            _event("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta=chunk)
        )
    deltas = _datas(server.posts, "external_output_text_delta")
    assert deltas == [
        {"delta": "Hel", "message_id": "opencode:msg_1:text:0", "index": 0, "final": False},
        {"delta": "lo", "message_id": "opencode:msg_1:text:0", "index": 1, "final": False},
    ]


async def test_text_ended_flushes_on_step_end_and_retires_preview() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta="Hi")
    )
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="Hi there")
    )
    assert _items(server.posts) == []  # buffered until the step ends
    await fwd.handle_event(_step_ended("msg_1"))
    items = _items(server.posts)
    assert len(items) == 1
    assert items[0]["item_data"]["role"] == "assistant"
    assert items[0]["item_data"]["content"] == [{"type": "output_text", "text": "Hi there"}]
    assert items[0]["response_id"] == "msg_1"
    # Same id as the deltas, so the server retires the live preview.
    assert items[0]["message_id"] == "opencode:msg_1:text:0"


async def test_text_flush_dedupes_repeated_step_end() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    ended = _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="once")
    await fwd.handle_event(ended)
    await fwd.handle_event(_step_ended("msg_1"))
    await fwd.handle_event(ended)
    await fwd.handle_event(_step_ended("msg_1"))
    assert len(_items(server.posts)) == 1


async def test_empty_text_is_not_persisted() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="")
    )
    await fwd.handle_event(_step_ended("msg_1"))
    assert _items(server.posts) == []


async def test_two_text_ordinals_flush_in_order() -> None:
    """Text before and after a tool call (ordinals 0 and 1) become two items, in order."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=1, text="after")
    )
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="before")
    )
    await fwd.handle_event(_step_ended("msg_1"))
    items = _items(server.posts)
    assert [i["item_data"]["content"][0]["text"] for i in items] == ["before", "after"]
    assert [i["message_id"] for i in items] == [
        "opencode:msg_1:text:0",
        "opencode:msg_1:text:1",
    ]


async def test_fixture_text_deltas_and_final_text_share_stream_id() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    ended = events_of_type("session.text.ended")[0]["data"]
    message_id, ordinal = ended["assistantMessageID"], ended["ordinal"]
    await fwd.handle_event(_fixture("session.step.started"))
    for raw in events_of_type("session.text.delta"):
        data = raw["data"]
        if data["assistantMessageID"] == message_id and data["ordinal"] == ordinal:
            await fwd.handle_event(_to_event(raw))
    await fwd.handle_event(_fixture("session.text.ended"))
    await fwd.handle_event(_fixture("session.step.ended"))
    stream_id = f"opencode:{message_id}:text:{ordinal}"
    deltas = _datas(server.posts, "external_output_text_delta")
    assert deltas and {d["message_id"] for d in deltas} == {stream_id}
    assert "".join(d["delta"] for d in deltas) == ended["text"]
    item = next(i for i in _items(server.posts) if i["item_type"] == "message")
    assert item["message_id"] == stream_id
    assert item["item_data"]["content"][0]["text"] == ended["text"]


# --- reasoning --------------------------------------------------------------


async def test_reasoning_deltas_open_block_once() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for chunk in ("Let me", " think"):
        await fwd.handle_event(
            _event("session.reasoning.delta", assistantMessageID="msg_1", ordinal=0, delta=chunk)
        )
    await fwd.handle_event(
        _event(
            "session.reasoning.ended", assistantMessageID="msg_1", ordinal=0, text="Let me think"
        )
    )
    assert _datas(server.posts, "external_output_reasoning_delta") == [
        {"delta": "Let me", "started": True},
        {"delta": " think", "started": False},
    ]


async def test_reasoning_ended_without_deltas_posts_whole_block() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.reasoning.ended", assistantMessageID="msg_1", ordinal=0, text="Hmm.")
    )
    assert _datas(server.posts, "external_output_reasoning_delta") == [
        {"delta": "Hmm.", "started": True}
    ]


async def test_second_reasoning_ordinal_opens_a_new_block() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for ordinal in (0, 1):
        await fwd.handle_event(
            _event(
                "session.reasoning.delta", assistantMessageID="msg_1", ordinal=ordinal, delta="x"
            )
        )
    started = [d["started"] for d in _datas(server.posts, "external_output_reasoning_delta")]
    assert started == [True, True]


async def test_fixture_reasoning_streams_as_one_block() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    ended = events_of_type("session.reasoning.ended")[0]["data"]
    await fwd.handle_event(_fixture("session.step.started"))
    for raw in events_of_type("session.reasoning.delta"):
        if raw["data"]["assistantMessageID"] == ended["assistantMessageID"]:
            await fwd.handle_event(_to_event(raw))
    await fwd.handle_event(_fixture("session.reasoning.ended"))
    deltas = _datas(server.posts, "external_output_reasoning_delta")
    assert deltas[0]["started"] is True
    assert all(d["started"] is False for d in deltas[1:])
    assert "".join(d["delta"] for d in deltas) == ended["text"]


# --- tools ------------------------------------------------------------------


def test_tool_content_text_joins_text_and_names_files() -> None:
    content = [
        {"type": "text", "text": "line 1"},
        {"type": "file", "uri": "file:///tmp/a.png", "mime": "image/png", "name": "a.png"},
    ]
    assert fwd_mod.opencode_tool_content_text(content) == "line 1\n[file: a.png]"


def test_tool_content_text_prefixes_errors() -> None:
    error = {"type": "tool.execution", "message": "boom"}
    assert fwd_mod.opencode_tool_content_text(None, error=error) == "[error] boom"
    partial = [{"type": "text", "text": "partial"}]
    assert fwd_mod.opencode_tool_content_text(partial, error=error) == "[error] boom\npartial"


async def test_fixture_shell_call_and_output() -> None:
    """The captured ``shell`` call posts under its v2 name with its input + output."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    started = next(
        raw
        for raw in events_of_type("session.tool.input.started")
        if raw["data"]["name"] == "shell"
    )
    call_id = started["data"]["id"]
    called = next(
        raw for raw in events_of_type("session.tool.called") if raw["data"]["id"] == call_id
    )
    success = next(
        raw for raw in events_of_type("session.tool.success") if raw["data"]["id"] == call_id
    )
    await fwd.handle_event(_fixture("session.step.started"))
    for raw in (started, called, success):
        await fwd.handle_event(_to_event(raw))
    items = _items(server.posts)
    call = next(i for i in items if i["item_type"] == "function_call")
    assert call["item_data"]["name"] == "shell"
    assert call["item_data"]["call_id"] == call_id
    assert fwd_mod.json.loads(call["item_data"]["arguments"]) == called["data"]["input"]
    out = next(i for i in items if i["item_type"] == "function_call_output")
    assert out["item_data"]["output"] == fwd_mod.opencode_tool_content_text(
        success["data"]["content"]
    )


async def test_tool_names_pass_through_unchanged() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for call_id, name in (("c1", "edit"), ("c2", "subagent"), ("c3", "omnigent_sys_session_list")):
        await fwd.handle_event(
            _event("session.tool.input.started", assistantMessageID="msg_1", id=call_id, name=name)
        )
        await fwd.handle_event(
            _event(
                "session.tool.called",
                assistantMessageID="msg_1",
                id=call_id,
                input={},
                executed=True,
            )
        )
    names = [
        i["item_data"]["name"] for i in _items(server.posts) if i["item_type"] == "function_call"
    ]
    assert names == ["edit", "subagent", "omnigent_sys_session_list"]


async def test_tool_items_share_the_running_response_id() -> None:
    """Tool items in a later step still group under the turn's live id."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_step_started("msg_2"))
    await fwd.handle_event(
        _event("session.tool.input.started", assistantMessageID="msg_2", id="c1", name="shell")
    )
    await fwd.handle_event(
        _event(
            "session.tool.called",
            assistantMessageID="msg_2",
            id="c1",
            input={"command": "ls"},
            executed=True,
        )
    )
    call = next(i for i in _items(server.posts) if i["item_type"] == "function_call")
    assert call["response_id"] == "msg_1"


async def test_tool_failed_posts_error_output() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.tool.input.started", assistantMessageID="msg_1", id="c1", name="shell")
    )
    await fwd.handle_event(
        _event(
            "session.tool.called",
            assistantMessageID="msg_1",
            id="c1",
            input={"command": "x"},
            executed=True,
        )
    )
    await fwd.handle_event(
        _event(
            "session.tool.failed",
            assistantMessageID="msg_1",
            id="c1",
            error={"type": "tool.execution", "message": "boom"},
            executed=True,
        )
    )
    out = next(i for i in _items(server.posts) if i["item_type"] == "function_call_output")
    assert out["item_data"] == {"call_id": "c1", "output": "[error] boom"}


async def test_tool_failed_without_call_posts_call_first() -> None:
    """Malformed tool input fails without ``session.tool.called``; keep the pair."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.tool.input.started", assistantMessageID="msg_1", id="c1", name="edit")
    )
    await fwd.handle_event(
        _event(
            "session.tool.failed",
            assistantMessageID="msg_1",
            id="c1",
            error={"type": "tool.input-json", "message": "bad json"},
            executed=False,
        )
    )
    kinds = [(i["item_type"], i["item_data"].get("name")) for i in _items(server.posts)]
    assert kinds == [("function_call", "edit"), ("function_call_output", None)]


async def test_tool_call_and_output_dedupe() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    called = _event(
        "session.tool.called", assistantMessageID="msg_1", id="c1", input={}, executed=True
    )
    success = _event(
        "session.tool.success",
        assistantMessageID="msg_1",
        id="c1",
        content=[{"type": "text", "text": "ok"}],
        executed=True,
    )
    for event in (called, called, success, success):
        await fwd.handle_event(event)
    kinds = [i["item_type"] for i in _items(server.posts)]
    assert kinds == ["function_call", "function_call_output"]


async def test_text_before_tool_call_lands_first() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="Running ls.")
    )
    await fwd.handle_event(
        _event("session.tool.called", assistantMessageID="msg_1", id="c1", input={}, executed=True)
    )
    assert [i["item_type"] for i in _items(server.posts)] == ["message", "function_call"]


# --- tool progress ----------------------------------------------------------


async def test_tool_progress_streams_growing_output_suffix() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for output in ("line1\n", "line1\nline2\n", "line1\nline2\n"):
        await fwd.handle_event(
            _event(
                "session.tool.progress",
                assistantMessageID="msg_1",
                id="c1",
                metadata={"output": output},
            )
        )
    assert _datas(server.posts, "external_tool_output_delta") == [
        {"call_id": "c1", "delta": "line1\n"},
        {"call_id": "c1", "delta": "line2\n"},
    ]


async def test_tool_progress_replacement_output_is_not_streamed() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for output in ("abc", "xyz-longer"):
        await fwd.handle_event(
            _event(
                "session.tool.progress",
                assistantMessageID="msg_1",
                id="c1",
                metadata={"output": output},
            )
        )
    assert _datas(server.posts, "external_tool_output_delta") == [
        {"call_id": "c1", "delta": "abc"}
    ]


async def test_fixture_shell_progress_without_output_is_dropped() -> None:
    """v2 shell progress is ``{shellID}`` only (tool/plugin/shell.ts), so nothing streams."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    progress = _fixture("session.tool.progress")
    assert "output" not in progress.data["metadata"]
    await fwd.handle_event(_fixture("session.step.started"))
    await fwd.handle_event(progress)
    assert "external_tool_output_delta" not in _types(server.posts)


# --- usage ------------------------------------------------------------------


async def test_step_ended_posts_session_usage() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _step_ended(
            "msg_a",
            cost=0.012,
            tokens={
                "input": 1000,
                "output": 50,
                "reasoning": 0,
                "cache": {"read": 200, "write": 0},
            },
        )
    )
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == 0.012
    assert usage["cumulative_input_tokens"] == 1000
    assert usage["cumulative_output_tokens"] == 50
    assert usage["cumulative_cache_read_input_tokens"] == 200
    assert usage["context_tokens"] == 1200
    assert usage["model"] == "anthropic/claude-sonnet-4-5"
    assert usage["context_window"] > 0


async def test_usage_updated_overrides_cumulative_totals() -> None:
    """``session.usage.updated`` totals win over the per-step sum (they include compaction)."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _step_ended(
            "msg_a",
            cost=0.01,
            tokens={"input": 100, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        )
    )
    await fwd.handle_event(
        _event(
            "session.usage.updated",
            cost=0.05,
            tokens={"input": 900, "output": 40, "reasoning": 0, "cache": {"read": 30, "write": 0}},
        )
    )
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == 0.05
    assert usage["cumulative_input_tokens"] == 900
    assert usage["cumulative_output_tokens"] == 40
    assert usage["cumulative_cache_read_input_tokens"] == 30
    assert usage["context_tokens"] == 100  # latest step, not the totals


async def test_usage_dedupes_identical_posts() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    ended = _step_ended(
        "msg_a",
        cost=0.01,
        tokens={"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    )
    await fwd.handle_event(ended)
    await fwd.handle_event(ended)
    assert len(_datas(server.posts, "external_session_usage")) == 1


async def test_step_failed_flushes_text_and_records_usage() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_a", ordinal=0, text="partial answer")
    )
    await fwd.handle_event(
        _event(
            "session.step.failed",
            assistantMessageID="msg_a",
            error={"type": "provider.transport", "message": "reset"},
            cost=0.002,
            tokens={"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        )
    )
    assert [i["item_data"]["content"][0]["text"] for i in _items(server.posts)] == [
        "partial answer"
    ]
    assert _datas(server.posts, "external_session_usage")[-1]["cumulative_cost_usd"] == 0.002


async def test_fixture_usage_updated_matches_captured_totals() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    raw = events_of_type("session.usage.updated")[-1]["data"]
    await fwd.handle_event(_fixture("session.usage.updated", -1))
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == round(raw["cost"], 6)
    assert usage["cumulative_input_tokens"] == int(raw["tokens"]["input"])
    assert usage["cumulative_output_tokens"] == int(raw["tokens"]["output"])


# --- model ------------------------------------------------------------------


async def test_first_step_model_is_recorded_not_mirrored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    assert "external_model_change" not in _types(server.posts)


async def test_step_model_change_is_mirrored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_step_started("msg_2", model={"id": "gpt-5", "providerID": "openai"}))
    assert _datas(server.posts, "external_model_change") == [{"model": "openai/gpt-5"}]


async def test_model_selected_mirrors_and_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    selected = _event(
        "session.model.selected", model={"id": "claude-opus-4", "providerID": "anthropic"}
    )
    await fwd.handle_event(selected)
    await fwd.handle_event(selected)
    assert _datas(server.posts, "external_model_change") == [{"model": "anthropic/claude-opus-4"}]


# --- execution failure ------------------------------------------------------


async def test_execution_failed_auth_posts_failed_with_reauth() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "provider.auth", "message": "invalid api key", "status": 401},
        )
    )
    status = _status_edges(server.posts)[-1]
    assert status["status"] == "failed"
    assert status["reauth_required"] is True
    assert "invalid api key" in status["output"]
    assert fwd_mod._OPENCODE_REAUTH_HINT in status["output"]


async def test_execution_failed_http_403_is_auth_shaped() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "provider.invalid-request", "message": "forbidden", "status": 403},
        )
    )
    assert _status_edges(server.posts)[-1]["reauth_required"] is True


async def test_execution_failed_generic_posts_failed_without_reauth() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "provider.internal", "message": "upstream boom", "status": 500},
        )
    )
    status = _status_edges(server.posts)[-1]
    assert status["status"] == "failed"
    assert status["output"] == "upstream boom"
    assert "reauth_required" not in status


async def test_execution_failed_aborted_takes_idle_path() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "aborted", "message": "Session interrupted by user"},
        )
    )
    status = _status_edges(server.posts)[-1]
    assert status["status"] == "idle"
    assert "output" not in status


# --- interruption -----------------------------------------------------------


async def test_user_interrupt_posts_idle_only() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.interrupted", reason="user"))
    assert "external_session_interrupted" not in _types(server.posts)
    assert _status_edges(server.posts)[-1] == {"status": "idle", "response_id": "msg_1"}


async def test_shutdown_interrupt_posts_interrupted_then_idle() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.interrupted", reason="shutdown"))
    types = _types(server.posts)
    assert _datas(server.posts, "external_session_interrupted") == [{"response_id": "msg_1"}]
    assert types.index("external_session_interrupted") < len(types) - 1
    assert _status_edges(server.posts)[-1]["status"] == "idle"


async def test_interrupt_persists_partial_streamed_text() -> None:
    """Streamed text without ``text.ended`` is kept and retires its preview."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta="Half an")
    )
    await fwd.handle_event(_event("session.execution.interrupted", reason="user"))
    item = _items(server.posts)[-1]
    assert item["item_data"]["content"][0]["text"] == "Half an"
    assert item["message_id"] == "opencode:msg_1:text:0"


# --- retry ------------------------------------------------------------------


async def test_retry_scheduled_posts_running_with_blocked_on() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.retry.scheduled",
            assistantMessageID="msg_1",
            attempt=2,
            at=1700000000000,
            error={"type": "provider.rate-limit", "message": "429 slow down"},
        )
    )
    edge = _status_edges(server.posts)[-1]
    assert edge == {
        "status": "running",
        "response_id": "msg_1",
        "blocked_on": "Retrying (attempt 2): 429 slow down",
    }


async def test_status_retry_dedupes_with_retry_scheduled() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.retry.scheduled",
            assistantMessageID="msg_1",
            attempt=1,
            at=1,
            error={"type": "provider.rate-limit", "message": "busy"},
        )
    )
    await fwd.handle_event(
        _event(
            "session.status", status={"type": "retry", "attempt": 1, "message": "busy", "next": 1}
        )
    )
    blocked = [e for e in _status_edges(server.posts) if "blocked_on" in e]
    assert len(blocked) == 1


async def test_next_step_after_retry_clears_blocked_on() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.status", status={"type": "retry", "attempt": 1, "message": "busy", "next": 1}
        )
    )
    await fwd.handle_event(_step_started("msg_2"))
    assert _status_edges(server.posts)[-1] == {"status": "running", "response_id": "msg_1"}


async def test_retry_before_first_step_posts_running_with_session_fallback_id() -> None:
    """A retry can arrive before any ``session.step.started`` (real Z.AI ordering)."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(
        _event(
            "session.retry.scheduled",
            assistantMessageID=None,
            attempt=1,
            at=1,
            error={"type": "provider.rate-limit", "message": "busy"},
        )
    )
    edge = _status_edges(server.posts)[-1]
    assert edge == {
        "status": "running",
        "response_id": _SESSION,
        "blocked_on": "Retrying (attempt 1): busy",
    }
    await fwd.handle_event(_step_started("msg_1"))
    assert _status_edges(server.posts)[-1] == {"status": "running", "response_id": "msg_1"}


# --- compaction -------------------------------------------------------------


async def test_fixture_compaction_cycle_brackets_status() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    await fwd.handle_event(_fixture("session.compaction.started"))
    await fwd.handle_event(_fixture("session.compaction.ended"))
    assert _datas(server.posts, "external_compaction_status") == [
        {"status": "in_progress"},
        {"status": "completed"},
    ]


async def test_compaction_failed_posts_failed() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.compaction.failed",
            reason="auto",
            error={"type": "provider.transport", "message": "reset"},
        )
    )
    assert _datas(server.posts, "external_compaction_status") == [{"status": "failed"}]


# --- user prompts -----------------------------------------------------------


def _enqueued(inbox_id: str, text: str, **payload: Any) -> OpenCodeEvent:
    return _event(
        "session.inbox.enqueued",
        inboxID=inbox_id,
        item={"type": "user", "payload": {"text": text, **payload}, "delivery": "steer"},
    )


async def test_user_prompt_posts_on_delivery_before_assistant() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_enqueued("msg_u", "my prompt"))
    assert _items(server.posts) == []
    await fwd.handle_event(_event("session.inbox.delivered", inboxID="msg_u"))
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_a", ordinal=0, text="hello")
    )
    await fwd.handle_event(_step_ended("msg_a"))
    items = _items(server.posts)
    assert [i["item_data"]["role"] for i in items] == ["user", "assistant"]
    assert items[0]["item_data"]["content"] == [{"type": "input_text", "text": "my prompt"}]
    assert items[0]["response_id"] == "msg_u"


async def test_user_prompt_image_attachment_becomes_input_image() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    image = {"data": "AAAA", "mime": "image/png", "source": {"type": "inline"}, "name": "a.png"}
    await fwd.handle_event(_enqueued("msg_u", "see image", files=[image]))
    await fwd.handle_event(_event("session.inbox.delivered", inboxID="msg_u"))
    content = _items(server.posts)[0]["item_data"]["content"]
    assert content == [
        {"type": "input_text", "text": "see image"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]


async def test_cancelled_inbox_prompt_is_never_posted() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_enqueued("msg_u", "never mind"))
    await fwd.handle_event(_event("session.inbox.cancelled", inboxID="msg_u"))
    await fwd.handle_event(_event("session.inbox.delivered", inboxID="msg_u"))
    assert _items(server.posts) == []


# --- permissions ------------------------------------------------------------


def _asked(request_id: str, action: str = "shell", **data: Any) -> OpenCodeEvent:
    data.setdefault("resources", ["ls"])
    data.setdefault("metadata", {"command": "ls"})
    return _event("permission.asked", id=request_id, action=action, **data)


async def test_fixture_permission_rejects_when_no_policy_wired() -> None:
    """No evaluator fails closed: the captured request is rejected."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    asked = _fixture("permission.asked")
    await fwd.handle_event(asked)
    await _drain(fwd)
    assert opencode.permission_replies == [(_FIX_SESSION, asked.data["id"], "reject")]


async def test_permission_asked_rejects_when_policy_denies() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def deny(_normalized: Any) -> dict[str, Any]:
        return {"decision": "deny"}

    fwd = _forwarder(server, opencode, policy_evaluator=deny)
    await fwd.handle_event(_asked("per_2"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_2", "reject")]


async def test_permission_asked_allows_only_on_explicit_policy_allow() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def allow(_normalized: Any) -> dict[str, Any]:
        return {"decision": "allow"}

    fwd = _forwarder(server, opencode, policy_evaluator=allow)
    await fwd.handle_event(_asked("per_a"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_a", "once")]


async def test_permission_asked_allow_always_still_replies_once() -> None:
    """Replying ``always`` would make OpenCode stop asking and bypass live policy."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def allow_always(_normalized: Any) -> dict[str, Any]:
        return {"decision": "allow_always"}

    fwd = _forwarder(server, opencode, policy_evaluator=allow_always)
    await fwd.handle_event(_asked("per_aa"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_aa", "once")]


async def test_permission_asked_rejects_when_policy_returns_ask() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def ask(_normalized: Any) -> dict[str, Any]:
        return {"decision": "ask"}

    fwd = _forwarder(server, opencode, policy_evaluator=ask)
    await fwd.handle_event(_asked("per_ask"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_ask", "reject")]


async def test_permission_asked_passes_normalized_input_to_evaluator() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    seen: list[Any] = []

    async def capture(normalized: Any) -> dict[str, Any]:
        seen.append(normalized)
        return {"decision": "deny"}

    fwd = _forwarder(server, opencode, policy_evaluator=capture, workspace="/work/repo")
    await fwd.handle_event(_asked("per_n"))
    await _drain(fwd)
    assert len(seen) == 1
    assert seen[0]["harness"] == "opencode-native"
    assert seen[0]["action"] == "shell"
    assert seen[0]["omnigent_session_id"] == "conv_1"


async def test_permission_asked_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    event = _asked("per_3")
    await fwd.handle_event(event)
    await fwd.handle_event(event)
    await _drain(fwd)
    assert len(opencode.permission_replies) == 1


async def test_permission_reply_failure_is_surfaced() -> None:
    """OpenCode blocks the turn until answered, so a failed reply must be visible."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def failing_reply(*_args: Any, **_kwargs: Any) -> bool:
        raise OpenCodeClientError("reply failed: 500")

    opencode.reply_permission = failing_reply  # type: ignore[method-assign]
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_asked("per_err"))
    await _drain(fwd)
    statuses = _datas(server.posts, "external_session_status")
    assert statuses[-1]["status"] == "running"
    assert statuses[-1]["blocked_on"] == "permission reply failed for per_err"


async def test_permission_evaluation_does_not_block_the_event_loop() -> None:
    """A parked approval must not stall later events for the session."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    release = asyncio.Event()

    async def parked(_normalized: Any) -> dict[str, Any]:
        await release.wait()
        return {"decision": "allow"}

    fwd = _forwarder(server, opencode, policy_evaluator=parked)
    await fwd.handle_event(_asked("per_p"))
    await fwd.handle_event(_event("session.compaction.started", reason="auto", recent=""))
    assert _datas(server.posts, "external_compaction_status") == [{"status": "in_progress"}]
    release.set()
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_p", "once")]


# --- permission replied -----------------------------------------------------


async def test_own_permission_reply_echo_is_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_asked("per_1"))
    await _drain(fwd)
    await fwd.handle_event(_event("permission.replied", requestID="per_1", reply="reject"))
    assert "external_elicitation_resolved" not in _types(server.posts)


async def test_tui_permission_reply_cancels_parked_evaluation_and_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def parked(_normalized: Any) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"decision": "allow"}

    fwd = _forwarder(server, opencode, policy_evaluator=parked)
    await fwd.handle_event(_asked("per_t"))
    task = fwd._permission_tasks["per_t"]
    await asyncio.sleep(0)
    await fwd.handle_event(_event("permission.replied", requestID="per_t", reply="once"))
    assert task.cancelled()
    assert opencode.permission_replies == []
    assert _datas(server.posts, "external_elicitation_resolved") == [{"elicitation_id": "per_t"}]


async def test_fixture_permission_replied_without_pending_task_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    replied = _fixture("permission.replied")
    await fwd.handle_event(replied)
    assert _datas(server.posts, "external_elicitation_resolved") == [
        {"elicitation_id": replied.data["requestID"]}
    ]


# --- form field mapping -----------------------------------------------------


def test_form_string_with_options_is_single_select() -> None:
    fields = [
        {
            "key": "q0",
            "type": "string",
            "title": "Formatting",
            "description": "Indent style?",
            "options": [
                {"value": "tab", "label": "Tabs", "description": "hard tabs"},
                {"value": "space", "label": "Spaces"},
            ],
        }
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert questions[0].question == {
        "question": "Indent style?",
        "options": [{"label": "Tabs", "description": "hard tabs"}, {"label": "Spaces"}],
        "multiSelect": False,
        "id": "q0",
        "header": "Formatting",
    }
    assert fwd_mod.form_answer(questions, fields, {"q0": "Tabs"}) == {"q0": "tab"}
    # A custom typed answer passes through unchanged.
    assert fwd_mod.form_answer(questions, fields, {"q0": "two spaces"}) == {"q0": "two spaces"}


def test_form_multiselect_maps_labels_to_values() -> None:
    fields = [
        {
            "key": "tools",
            "type": "multiselect",
            "options": [{"value": "t", "label": "Tests"}, {"value": "l", "label": "Lint"}],
        }
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None and questions[0].question["multiSelect"] is True
    assert fwd_mod.form_answer(questions, fields, {"tools": ["Tests", "Lint"]}) == {
        "tools": ["t", "l"]
    }


def test_form_boolean_number_and_integer_fields() -> None:
    fields = [
        {"key": "ok", "type": "boolean", "title": "Proceed?"},
        {"key": "ratio", "type": "number"},
        {"key": "count", "type": "integer"},
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert questions[0].question["options"] == [{"label": "Yes"}, {"label": "No"}]
    assert questions[1].question["options"] == []
    content = {"ok": "No", "ratio": "0.5", "count": "3"}
    assert fwd_mod.form_answer(questions, fields, content) == {
        "ok": False,
        "ratio": 0.5,
        "count": 3,
    }
    assert fwd_mod.form_answer(questions, fields, {"count": "3.5"}) is None
    assert fwd_mod.form_answer(questions, fields, {"ratio": "abc"}) is None


def test_form_external_field_is_acknowledged() -> None:
    fields = [
        {
            "key": "login",
            "type": "external",
            "url": "https://example.test/auth",
            "title": "Sign in",
        }
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert "https://example.test/auth" in questions[0].question["question"]
    assert questions[0].question["options"] == [{"label": "Done"}]
    assert fwd_mod.form_answer(questions, fields, {"login": "Done"}) == {"login": True}


def test_form_hidden_fields_are_skipped_and_unknown_types_reject() -> None:
    hidden = [{"key": "token", "type": "string", "hidden": True}]
    assert fwd_mod.form_questions(hidden) == []
    assert fwd_mod.form_questions([{"key": "x", "type": "date"}]) is None
    assert fwd_mod.form_questions([{"key": "m", "type": "multiselect", "options": []}]) is None


def test_form_answer_drops_inactive_conditional_fields() -> None:
    fields = [
        {
            "key": "mode",
            "type": "string",
            "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
        },
        {"key": "detail", "type": "string", "when": [{"key": "mode", "op": "eq", "value": "b"}]},
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert fwd_mod.form_answer(questions, fields, {"mode": "A", "detail": "x"}) == {"mode": "a"}
    assert fwd_mod.form_answer(questions, fields, {"mode": "B", "detail": "x"}) == {
        "mode": "b",
        "detail": "x",
    }


# --- form.created -----------------------------------------------------------


def _sample_answer(question: fwd_mod.FormQuestion) -> str | list[str]:
    """A web-form answer the mapper accepts for *question*'s field type."""
    labels = [option["label"] for option in question.question["options"]]
    if question.kind == "multiselect":
        return labels[:1]
    if question.kind in ("number", "integer"):
        return "1"
    return labels[0] if labels else "typed answer"


async def test_fixture_form_accept_replies_with_mapped_answer() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    created = _fixture("form.created")
    form = created.data["form"]
    questions = fwd_mod.form_questions(form["fields"])
    assert questions
    content = {question.key: _sample_answer(question) for question in questions}
    server.hook_response = {"action": "accept", "content": content}
    await fwd.handle_event(created)
    await _drain(fwd)
    hook = _hook_post(server)
    assert hook is not None
    assert hook["elicitation_id"] == form["id"]
    assert hook["operation_type"] == "question"
    assert hook["agent"] == "OpenCode"
    assert hook["policy_name"] == "opencode_native_question"
    assert [q["id"] for q in hook["ask_user_question"]["questions"]] == [q.key for q in questions]
    expected = fwd_mod.form_answer(questions, form["fields"], content)
    assert expected is not None
    assert opencode.form_replies == [(_FIX_SESSION, form["id"], expected)]
    assert opencode.form_cancels == []


def _form_event(
    form_id: str, fields: list[dict[str, Any]], title: str = "Questions"
) -> OpenCodeEvent:
    return OpenCodeEvent(
        id=None,
        type="form.created",
        data={"form": {"id": form_id, "sessionID": _SESSION, "title": title, "fields": fields}},
        location=None,
    )


_SINGLE = [
    {
        "key": "q0",
        "type": "string",
        "title": "Formatting",
        "description": "Indent style?",
        "options": [{"value": "Tabs", "label": "Tabs"}, {"value": "Spaces", "label": "Spaces"}],
        "custom": True,
    }
]


async def test_form_decline_cancels_without_reply() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "decline"}
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", _SINGLE))
    await _drain(fwd)
    assert opencode.form_cancels == [(_SESSION, "frm_1")]
    assert opencode.form_replies == []


async def test_form_empty_verdict_cancels() -> None:
    """An empty 200 (TUI answered / timed out) cancels so OpenCode is not wedged."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", _SINGLE))
    await _drain(fwd)
    assert _hook_post(server) is not None
    assert opencode.form_cancels == [(_SESSION, "frm_1")]


async def test_unrenderable_form_cancels_without_hook() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", [{"key": "x", "type": "date"}]))
    await _drain(fwd)
    assert _hook_post(server) is None
    assert opencode.form_cancels == [(_SESSION, "frm_1")]


async def test_all_hidden_form_replies_empty_answer_without_hook() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", [{"key": "t", "type": "string", "hidden": True}]))
    await _drain(fwd)
    assert _hook_post(server) is None
    assert opencode.form_replies == [(_SESSION, "frm_1", {})]


async def test_invalid_form_answer_cancels() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "accept", "content": {"n": "not a number"}}
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", [{"key": "n", "type": "number"}]))
    await _drain(fwd)
    assert opencode.form_cancels == [(_SESSION, "frm_1")]
    assert opencode.form_replies == []


async def test_form_created_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "accept", "content": {"q0": "Tabs"}}
    fwd = _forwarder(server, opencode)
    event = _form_event("frm_1", _SINGLE)
    await fwd.handle_event(event)
    task = fwd._form_tasks["frm_1"]
    await fwd.handle_event(event)
    assert fwd._form_tasks["frm_1"] is task
    await _drain(fwd)
    assert opencode.form_replies == [(_SESSION, "frm_1", {"q0": "Tabs"})]


async def test_form_reply_failure_cancels_and_logs() -> None:
    """A failed reply must still cancel the form and surface a blocked status."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "accept", "content": {"q0": "Tabs"}}

    async def failing_reply(*_args: Any, **_kwargs: Any) -> bool:
        raise OpenCodeClientError("reply failed: 500")

    opencode.reply_form = failing_reply  # type: ignore[method-assign]
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", _SINGLE))
    await _drain(fwd)
    assert opencode.form_cancels == [(_SESSION, "frm_1")]
    statuses = _datas(server.posts, "external_session_status")
    assert statuses[-1]["status"] == "running"
    assert statuses[-1]["blocked_on"] == "form reply failed for frm_1"


async def test_form_hook_transport_failure_cancels_form() -> None:
    """A hook POST transport failure must cancel the form, not hang it."""

    class _FailingServerClient:
        async def post(self, _url: str, *, json: dict[str, Any]) -> httpx.Response:
            raise httpx.ConnectError("boom", request=httpx.Request("POST", "http://x"))

    opencode = _FakeOpenCodeClient()
    fwd = _forwarder(_FailingServerClient(), opencode)  # type: ignore[arg-type]
    await fwd.handle_event(_form_event("frm_1", _SINGLE))
    await _drain(fwd)
    assert opencode.form_cancels == [(_SESSION, "frm_1")]
    assert opencode.form_replies == []


# --- form resolution --------------------------------------------------------


async def test_form_replied_cancels_pending_task_and_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)

    async def _never() -> None:
        await asyncio.sleep(3600)

    pending: asyncio.Task[None] = asyncio.create_task(_never())
    fwd._form_tasks["frm_1"] = pending
    await fwd.handle_event(_event("form.replied", id="frm_1", answer={"q0": "Tabs"}))
    assert "frm_1" not in fwd._form_tasks
    with contextlib.suppress(asyncio.CancelledError):
        await pending
    assert pending.cancelled()
    assert _datas(server.posts, "external_elicitation_resolved") == [{"elicitation_id": "frm_1"}]
    assert opencode.form_replies == []
    assert opencode.form_cancels == []


async def test_fixture_form_replied_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    replied = _fixture("form.replied")
    await fwd.handle_event(replied)
    assert _datas(server.posts, "external_elicitation_resolved") == [
        {"elicitation_id": replied.data["id"]}
    ]


async def test_form_cancelled_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("form.cancelled", id="frm_9"))
    assert _datas(server.posts, "external_elicitation_resolved") == [{"elicitation_id": "frm_9"}]


async def test_form_replied_echo_of_our_own_reply_is_ignored() -> None:
    """Our own reply's echo must not cancel the parked task or post twice."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "accept", "content": {"q0": "Tabs"}}
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", _SINGLE))
    await _drain(fwd)
    assert opencode.form_replies == [(_SESSION, "frm_1", {"q0": "Tabs"})]
    assert "frm_1" not in fwd._form_tasks

    await fwd.handle_event(_event("form.replied", id="frm_1", answer={"q0": "Tabs"}))

    assert _datas(server.posts, "external_elicitation_resolved") == []


async def test_run_awaits_cancelled_background_tasks() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    cleanup_finished = asyncio.Event()

    async def _pending() -> None:
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.set()

    pending = asyncio.create_task(_pending())
    fwd._form_tasks["frm_1"] = pending
    await asyncio.sleep(0)
    await fwd.run(max_reconnects=0)
    assert cleanup_finished.is_set()
    assert pending.cancelled()
    assert fwd._form_tasks == {}
    assert fwd._permission_tasks == {}


# --- subagents --------------------------------------------------------------


def _child_created(child_id: str, parent_id: str = _SESSION) -> OpenCodeEvent:
    return OpenCodeEvent(
        id=None,
        type="session.created",
        data={
            "sessionID": child_id,
            "parentID": parent_id,
            "projectID": "prj_1",
            "location": {"directory": "/work"},
            "slug": "child",
            "title": "Explore the repo",
            "agent": "explore",
            "version": "2.0.18",
        },
        location=None,
    )


async def test_subagent_child_is_minted_with_its_tool_call() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.tool.input.started",
            assistantMessageID="msg_1",
            id="call_sub",
            name="subagent",
        )
    )
    await fwd.handle_event(_child_created("ses_child"))
    assert "external_subagent_start" not in _types(server.posts)
    await fwd.handle_event(
        _event(
            "session.tool.progress",
            assistantMessageID="msg_1",
            id="call_sub",
            metadata={"sessionID": "ses_child", "status": "running"},
        )
    )
    url, start = next((u, b) for u, b in server.posts if b["type"] == "external_subagent_start")
    assert url == "/v1/sessions/conv_1/events"
    assert start["data"] == {
        "subagent_id": "ses_child",
        "agent_type": "explore",
        "description": "Explore the repo",
        "tool_use_id": "call_sub",
    }


async def test_subagent_child_events_post_to_the_child_conversation() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_child_created("ses_child"))
    child_step = OpenCodeEvent(
        id=None,
        type="session.step.started",
        data={
            "sessionID": "ses_child",
            "assistantMessageID": "msg_c1",
            "agent": "explore",
            "model": {"id": "m", "providerID": "p"},
            "started": 1,
        },
        location=None,
    )
    await fwd.handle_event(child_step)
    start = next(b for _u, b in server.posts if b["type"] == "external_subagent_start")
    # No progress arrived first, so the child's own id stands in for the call id.
    assert start["data"]["tool_use_id"] == "ses_child"
    running = [(u, b) for u, b in server.posts if b["type"] == "external_session_status"]
    assert running == [
        (
            "/v1/sessions/conv_child_1/events",
            {
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "msg_c1"},
            },
        )
    ]
    # Child steps never touch the parent's model or usage.
    assert "external_model_change" not in _types(server.posts)


async def test_grandchild_session_follows_the_parent_chain() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_child_created("ses_child"))
    grandchild = _child_created("ses_grand", parent_id="ses_child")
    assert fwd._event_targets_session(grandchild) is True
    await fwd.handle_event(grandchild)
    await fwd.handle_event(
        OpenCodeEvent(
            id=None,
            type="session.compaction.started",
            data={"sessionID": "ses_grand", "reason": "auto", "recent": ""},
            location=None,
        )
    )
    starts = [
        (u, b["data"]["subagent_id"])
        for u, b in server.posts
        if b["type"] == "external_subagent_start"
    ]
    # The child is minted first (on the root), then the grandchild on the child.
    assert starts == [
        ("/v1/sessions/conv_1/events", "ses_child"),
        ("/v1/sessions/conv_child_1/events", "ses_grand"),
    ]


async def test_unrelated_session_created_is_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    stranger = _child_created("ses_other_child", parent_id="ses_stranger")
    assert fwd._event_targets_session(stranger) is False
    await fwd.handle_event(stranger)
    assert "ses_other_child" not in fwd._turns


async def test_child_permission_replies_to_the_child_session() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_child_created("ses_child"))
    await fwd.handle_event(_asked("per_c", sessionID="ses_child"))
    await _drain(fwd)
    assert opencode.permission_replies == [("ses_child", "per_c", "reject")]


# --- history seeding --------------------------------------------------------


def _assistant_message(
    message_id: str, *content: dict[str, Any], completed: bool = True, **extra: Any
) -> dict[str, Any]:
    time_info: dict[str, Any] = {"created": 1}
    if completed:
        time_info["completed"] = 2
    return {
        "id": message_id,
        "type": "assistant",
        "agent": "build",
        "model": {"id": "claude-sonnet-4-5", "providerID": "anthropic"},
        "content": list(content),
        "time": time_info,
        **extra,
    }


def _tool_content(call_id: str, status: str, **state: Any) -> dict[str, Any]:
    state.setdefault("input", {"command": "ls"})
    return {
        "type": "tool",
        "id": call_id,
        "name": "shell",
        "state": {"status": status, **state},
        "time": {"created": 1},
    }


async def test_seed_marks_v2_history_keys() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {"id": "msg_u", "type": "user", "text": "hi", "time": {"created": 1}},
        _assistant_message(
            "msg_1",
            {"type": "reasoning", "text": "think"},
            {"type": "text", "text": "answer"},
            _tool_content("call_1", "completed", content=[{"type": "text", "text": "ok"}]),
            _tool_content("call_2", "running", metadata={}),
        ),
        "not-a-mapping",
    ]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    assert fwd.state.mark(fwd._key("user", "msg_u")) is False
    assert fwd.state.mark(fwd._key("text-final", "msg_1", "0")) is False
    assert fwd.state.mark(fwd._key("tool-call", "call_1")) is False
    assert fwd.state.mark(fwd._key("tool-out", "call_1")) is False
    # A still-running tool's output must still post live.
    assert fwd.state.mark(fwd._key("tool-out", "call_2")) is True


async def test_seed_swallows_history_errors() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def _boom(_sid: str, *, after_id: str | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("history unavailable")

    opencode.list_messages = _boom  # type: ignore[assignment]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    assert server.posts == []


async def test_seed_rebuilds_usage_and_model() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    tokens_1 = {"input": 1000, "output": 50, "reasoning": 0, "cache": {"read": 200, "write": 0}}
    tokens_2 = {"input": 2000, "output": 100, "reasoning": 0, "cache": {"read": 300, "write": 0}}
    opencode.messages = [
        _assistant_message("msg_1", cost=0.01, tokens=tokens_1),
        _assistant_message("msg_2", cost=0.02, tokens=tokens_2),
    ]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == 0.03
    assert usage["cumulative_input_tokens"] == 3000
    assert usage["cumulative_output_tokens"] == 150
    assert usage["cumulative_cache_read_input_tokens"] == 500
    # The next step on the same model is not reported as a switch.
    await fwd.handle_event(_step_started("msg_3"))
    assert "external_model_change" not in _types(server.posts)


async def test_seed_cursor_stops_at_first_unsettled_message() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {"id": "msg_u", "type": "user", "text": "hi", "time": {"created": 1}},
        _assistant_message("msg_1"),
        _assistant_message("msg_2", completed=False),
        {"id": "msg_u2", "type": "user", "text": "more", "time": {"created": 3}},
    ]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    assert fwd._last_seen_message_id == "msg_1"


async def test_seed_from_captured_history_suppresses_replay() -> None:
    """Seeding from the real fixture history must dedupe its own captured events."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    # messages.json is captured newest-first; seeding needs ascending (oldest-first) order.
    opencode.messages = list(reversed(load_messages()["data"]))
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    await fwd.seed_dedupe_from_history()
    for raw in load_events():
        # Both spawn background tasks that need a live counterpart to resolve; the
        # seeded history already covers what they would have produced.
        if raw["type"] in ("permission.asked", "form.created"):
            continue
        await fwd.handle_event(_to_event(raw))
    await _drain(fwd)
    for item in _items(server.posts):
        assert item["item_type"] not in ("function_call", "function_call_output")
        if item["item_type"] == "message":
            assert item["item_data"].get("role") not in ("assistant", "user")
