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
