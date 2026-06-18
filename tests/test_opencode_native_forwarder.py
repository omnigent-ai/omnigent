"""Tests for the OpenCode SSE -> Omnigent event forwarder translation."""

from __future__ import annotations

from typing import Any

import httpx

import omnigent.opencode_native_forwarder as fwd_mod
from omnigent.opencode_native_client import OpenCodeEvent

_SESSION = "ses_1"


class _RecordingServerClient:
    """httpx-shaped stub recording Omnigent event POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


class _FakeOpenCodeClient:
    """Fake OpenCode client recording permission replies + history."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.messages: list[dict[str, Any]] = []

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self.messages

    async def reply_permission(self, request_id: str, reply: dict[str, Any]) -> bool:
        self.replies.append((request_id, reply))
        return True


def _forwarder(
    server: _RecordingServerClient,
    opencode: _FakeOpenCodeClient,
    **kwargs: Any,
) -> fwd_mod.OpenCodeNativeForwarder:
    return fwd_mod.OpenCodeNativeForwarder(
        session_id="conv_1",
        opencode_session_id=_SESSION,
        opencode_client=opencode,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
        **kwargs,
    )


def _event(event_type: str, **props: Any) -> OpenCodeEvent:
    props.setdefault("sessionID", _SESSION)
    return OpenCodeEvent(id=None, type=event_type, properties=props, raw={})


def _types(posts: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [body["type"] for _url, body in posts]


async def test_text_delta_posts_output_text_delta() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.next.text.delta", textID="t1", delta="hello"))
    # A status edge (running) plus the text delta.
    assert "external_output_text_delta" in _types(server.posts)
    delta_post = next(b for _u, b in server.posts if b["type"] == "external_output_text_delta")
    assert delta_post["data"]["delta"] == "hello"


async def test_text_ended_posts_assistant_message_and_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    ev = _event("session.next.text.ended", textID="t1", text="full answer")
    await fwd.handle_event(ev)
    await fwd.handle_event(ev)  # duplicate must not re-post
    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert len(items) == 1
    assert items[0]["data"]["item_type"] == "message"
    assert items[0]["data"]["item_data"]["role"] == "assistant"
    assert items[0]["data"]["item_data"]["content"][0]["text"] == "full answer"


async def test_tool_called_posts_function_call() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.next.tool.called",
            callID="call_1",
            tool="bash",
            input={"command": "ls"},
        )
    )
    item = next(
        b
        for _u, b in server.posts
        if b["type"] == "external_conversation_item" and b["data"]["item_type"] == "function_call"
    )
    assert item["data"]["item_data"]["name"] == "bash"
    assert item["data"]["item_data"]["call_id"] == "call_1"
    assert '"command": "ls"' in item["data"]["item_data"]["arguments"]


async def test_tool_success_posts_function_call_output() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event("session.next.tool.success", callID="call_1", content="file1\nfile2")
    )
    item = next(
        b for _u, b in server.posts if b["data"].get("item_type") == "function_call_output"
    )
    assert item["data"]["item_data"]["call_id"] == "call_1"
    assert item["data"]["item_data"]["output"] == "file1\nfile2"


async def test_tool_failed_posts_error_output() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.next.tool.failed", callID="call_2", error="boom"))
    item = next(
        b for _u, b in server.posts if b["data"].get("item_type") == "function_call_output"
    )
    assert "boom" in item["data"]["item_data"]["output"]


async def test_step_lifecycle_emits_running_then_idle() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.next.step.started", assistantMessageID="msg_1"))
    await fwd.handle_event(_event("session.next.step.ended", finish="stop"))
    statuses = [
        b["data"]["status"] for _u, b in server.posts if b["type"] == "external_session_status"
    ]
    assert statuses == ["running", "idle"]


async def test_interrupt_requested_emits_cancelling() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.next.interrupt.requested"))
    statuses = [
        b["data"]["status"] for _u, b in server.posts if b["type"] == "external_session_status"
    ]
    assert "cancelling" in statuses


async def test_permission_asked_rejects_when_no_policy_wired() -> None:
    """Absent a policy evaluator the forwarder FAILS CLOSED (no auto-approve).

    The security contract: a headless OpenCode turn must never silently
    auto-approve a sensitive op just because no policy gate is wired. The
    previous ``allow_once`` default did exactly that.
    """
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)  # no policy_evaluator → fail closed
    await fwd.handle_event(
        _event("permission.v2.asked", id="per_1", action="bash", resources=[{"command": "ls"}])
    )
    assert opencode.replies == [("per_1", {"reply": "reject", "message": "omnigent-policy"})]


async def test_permission_asked_rejects_when_policy_denies() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def deny(_normalized: Any) -> dict[str, Any]:
        return {"decision": "deny"}

    fwd = _forwarder(server, opencode, policy_evaluator=deny)
    await fwd.handle_event(_event("permission.v2.asked", id="per_2", action="bash"))
    assert opencode.replies[0][1]["reply"] == "reject"


async def test_permission_asked_allows_only_on_explicit_policy_allow() -> None:
    """An explicit policy ``allow`` is the only path to ``once``."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def allow(_normalized: Any) -> dict[str, Any]:
        return {"decision": "allow"}

    fwd = _forwarder(server, opencode, policy_evaluator=allow)
    await fwd.handle_event(_event("permission.v2.asked", id="per_a", action="bash"))
    assert opencode.replies[0][1]["reply"] == "once"


async def test_permission_asked_allow_always_maps_to_always() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def allow_always(_normalized: Any) -> dict[str, Any]:
        return {"decision": "allow_always"}

    fwd = _forwarder(server, opencode, policy_evaluator=allow_always)
    await fwd.handle_event(_event("permission.v2.asked", id="per_aa", action="bash"))
    assert opencode.replies[0][1]["reply"] == "always"


async def test_permission_asked_rejects_when_policy_returns_ask() -> None:
    """An unresolved ``ask`` reaching the forwarder FAILS CLOSED, not auto-approve.

    The genuine human approval for an ``ask`` is resolved UPSTREAM by the
    policy evaluator (the server parks an approval card on
    ``/policies/evaluate`` and returns a hard allow/deny). An ``ask`` that
    still reaches the forwarder means no human resolution was obtained, so
    it must DENY — never silently approve.
    """
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def ask(_normalized: Any) -> dict[str, Any]:
        return {"decision": "ask"}

    fwd = _forwarder(server, opencode, policy_evaluator=ask)
    await fwd.handle_event(_event("permission.v2.asked", id="per_ask", action="bash"))
    assert opencode.replies[0][1]["reply"] == "reject"


async def test_permission_asked_passes_normalized_input_to_evaluator() -> None:
    """The forwarder routes through the policy gate with a normalized input.

    Proves the request is genuinely evaluated (harness + action + the
    concrete command), not decided by a hardcoded default.
    """
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    seen: list[Any] = []

    async def capture(normalized: Any) -> dict[str, Any]:
        seen.append(normalized)
        return {"decision": "deny"}

    fwd = _forwarder(server, opencode, policy_evaluator=capture, workspace="/work/repo")
    await fwd.handle_event(
        _event("permission.v2.asked", id="per_n", action="bash", resources=[{"command": "ls"}])
    )
    assert len(seen) == 1
    assert seen[0]["harness"] == "opencode-native"
    assert seen[0]["action"] == "bash"
    assert seen[0]["command"] == "ls"
    assert seen[0]["working_directory"] == "/work/repo"
    assert seen[0]["omnigent_session_id"] == "conv_1"


async def test_permission_asked_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    ev = _event("permission.v2.asked", id="per_3", action="bash")
    await fwd.handle_event(ev)
    await fwd.handle_event(ev)
    assert len(opencode.replies) == 1


async def test_event_for_other_session_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        OpenCodeEvent(
            id=None,
            type="session.next.text.ended",
            properties={"sessionID": "ses_OTHER", "textID": "t", "text": "x"},
            raw={},
        )
    )
    assert server.posts == []


async def test_unknown_event_is_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("some.unknown.event", foo="bar"))
    assert server.posts == []


async def test_run_reconnects_until_cap() -> None:
    """run() retries the SSE consume loop and stops at the reconnect cap."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    calls = {"n": 0}

    async def failing_consume() -> None:
        calls["n"] += 1
        raise httpx.ReadError("dropped", request=httpx.Request("GET", "http://x/event"))

    fwd._consume_once = failing_consume  # type: ignore[method-assign]

    # Patch sleep so the backoff doesn't slow the test.
    async def _no_sleep(_seconds: float) -> None:
        return None

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=3)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert calls["n"] == 4  # initial + 3 reconnects
