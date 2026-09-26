"""Tests for the OpenCode v2 event -> Omnigent event forwarder translation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from tests.opencode_v2_fixtures import events_of_type

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
    return [body["data"] for _url, body in posts if body["type"] == event_type]


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
