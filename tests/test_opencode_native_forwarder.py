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


async def test_part_delta_is_not_forwarded() -> None:
    """Live token deltas are intentionally dropped (see the _HANDLERS note).

    The web chat view reconciles live ``text_delta`` previews with the
    committed item via a finalize/retire handshake; emitting deltas without it
    duplicated/garbled the chat. The forwarder posts only the durable item.
    """
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "message.part.delta", field="text", partID="prt_1", messageID="msg_1", delta="hello"
        )
    )
    assert "external_output_text_delta" not in _types(server.posts)


async def test_assistant_text_part_finalized_on_idle_and_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    # The role lives on the message; the text on a text part of that message.
    await fwd.handle_event(_event("message.updated", info={"id": "msg_1", "role": "assistant"}))
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={"id": "prt_1", "messageID": "msg_1", "type": "text", "text": "full answer"},
        )
    )
    await fwd.handle_event(_event("session.idle"))
    await fwd.handle_event(_event("session.idle"))  # duplicate flush must not re-post
    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert len(items) == 1
    assert items[0]["data"]["item_type"] == "message"
    assert items[0]["data"]["item_data"]["role"] == "assistant"
    assert items[0]["data"]["item_data"]["content"][0]["text"] == "full answer"
    # The item groups under its assistant messageID (per-turn response), NOT a
    # constant session id — that constant id was what clustered every turn's
    # assistant items together and broke chat ordering.
    assert items[0]["data"]["response_id"] == "msg_1"


async def test_each_assistant_message_gets_its_own_response_id() -> None:
    """Distinct assistant messages map to distinct per-turn response groups."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    for msg in ("msg_a", "msg_b"):
        await fwd.handle_event(_event("message.updated", info={"id": msg, "role": "assistant"}))
        await fwd.handle_event(
            _event(
                "message.part.updated",
                part={"id": f"prt_{msg}", "messageID": msg, "type": "text", "text": f"t-{msg}"},
            )
        )
        await fwd.handle_event(_event("session.idle"))
    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    response_ids = [it["data"]["response_id"] for it in items]
    assert response_ids == ["msg_a", "msg_b"], "each turn must get its own response_id"


async def test_user_text_part_is_mirrored_before_the_assistant() -> None:
    """The forwarder is the transcript source: it posts the user message too.

    For native-server harnesses omnigent persists no separate user item, so the
    forwarder must mirror the user message (role=user) — posted eagerly so it
    precedes its assistant reply (correct chat ordering). Deduped by part id.
    """
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_u", "role": "user"}))
    user_part = _event(
        "message.part.updated",
        part={"id": "prt_u", "messageID": "msg_u", "type": "text", "text": "my prompt"},
    )
    await fwd.handle_event(user_part)
    await fwd.handle_event(user_part)  # snapshot repeat must not double-post
    # Then the assistant reply for the same turn.
    await fwd.handle_event(_event("message.updated", info={"id": "msg_a", "role": "assistant"}))
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={"id": "prt_a", "messageID": "msg_a", "type": "text", "text": "hello"},
        )
    )
    await fwd.handle_event(_event("session.idle"))

    items = [b["data"] for _u, b in server.posts if b["type"] == "external_conversation_item"]
    roles = [it["item_data"]["role"] for it in items if it["item_type"] == "message"]
    assert roles == ["user", "assistant"], f"expected user before assistant, got {roles}"
    user_item = next(it for it in items if it["item_data"]["role"] == "user")
    assert user_item["item_data"]["content"][0]["text"] == "my prompt"
    assert user_item["response_id"] == "msg_u"


async def test_tool_part_posts_function_call_and_output() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_1", "role": "assistant"}))
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={
                "id": "prt_t",
                "messageID": "msg_1",
                "type": "tool",
                "callID": "call_1",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls"},
                    "output": "file1\nfile2",
                },
            },
        )
    )
    call = next(b for _u, b in server.posts if b["data"].get("item_type") == "function_call")
    assert call["data"]["item_data"]["name"] == "bash"
    assert call["data"]["item_data"]["call_id"] == "call_1"
    assert '"command": "ls"' in call["data"]["item_data"]["arguments"]
    assert call["data"]["response_id"] == "msg_1"
    out = next(b for _u, b in server.posts if b["data"].get("item_type") == "function_call_output")
    assert out["data"]["item_data"]["call_id"] == "call_1"
    assert out["data"]["item_data"]["output"] == "file1\nfile2"
    assert out["data"]["response_id"] == "msg_1"


async def test_tool_part_dedupes_call_and_output_across_snapshots() -> None:
    """The same tool part as running then completed posts the call/output once each."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_1", "role": "assistant"}))
    base = {"id": "prt_t", "messageID": "msg_1", "type": "tool", "callID": "c1", "tool": "bash"}
    running = {"status": "running", "input": {"command": "ls"}}
    completed = {"status": "completed", "input": {"command": "ls"}, "output": "ok"}
    await fwd.handle_event(_event("message.part.updated", part={**base, "state": running}))
    await fwd.handle_event(_event("message.part.updated", part={**base, "state": completed}))
    calls = [b for _u, b in server.posts if b["data"].get("item_type") == "function_call"]
    outs = [b for _u, b in server.posts if b["data"].get("item_type") == "function_call_output"]
    assert len(calls) == 1
    assert len(outs) == 1


async def test_tool_part_error_posts_error_output() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_1", "role": "assistant"}))
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={
                "id": "prt_e",
                "messageID": "msg_1",
                "type": "tool",
                "callID": "call_2",
                "tool": "bash",
                "state": {"status": "error", "input": {"command": "x"}, "error": "boom"},
            },
        )
    )
    item = next(
        b for _u, b in server.posts if b["data"].get("item_type") == "function_call_output"
    )
    assert "boom" in item["data"]["item_data"]["output"]


async def test_lifecycle_emits_running_then_idle() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_1", "role": "assistant"}))
    await fwd.handle_event(_event("session.idle"))
    statuses = [
        b["data"]["status"] for _u, b in server.posts if b["type"] == "external_session_status"
    ]
    assert statuses == ["running", "idle"]


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


async def test_permission_asked_allow_always_still_replies_once() -> None:
    """An allow_always verdict must reply "once", never "always".

    Replying "always" makes opencode persist the grant and stop emitting
    permission.asked, which bypasses the server policy engine and breaks live
    policy toggles (e.g. enabling "Require Approval" mid-session). The forwarder
    always replies "once" so opencode re-asks every call; "always allow"
    persistence is the server engine's job.
    """
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def allow_always(_normalized: Any) -> dict[str, Any]:
        return {"decision": "allow_always"}

    fwd = _forwarder(server, opencode, policy_evaluator=allow_always)
    await fwd.handle_event(_event("permission.v2.asked", id="per_aa", action="bash"))
    assert opencode.replies[0][1]["reply"] == "once"


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
            type="message.part.updated",
            properties={
                "sessionID": "ses_OTHER",
                "part": {"id": "p", "messageID": "m", "type": "text", "text": "x"},
            },
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


async def test_seed_dedupe_from_history_marks_parts_and_roles() -> None:
    """Resume seeding records message roles and pre-marks text/tool part keys."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {
            "info": {"id": "msg_1", "role": "assistant"},
            "parts": [
                {"id": "prt_text", "type": "text"},
                {"id": "prt_tool", "type": "tool", "callID": "call_1"},
                "not-a-mapping",
            ],
        },
        {"info": {"id": "msg_2", "role": "user"}, "parts": []},
        "not-a-mapping-message",
    ]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    assert fwd._msg_role == {"msg_1": "assistant", "msg_2": "user"}
    # Seeded keys are pre-marked, so re-marking returns False (would be deduped).
    assert fwd.state.mark(fwd._key("text-final", "prt_text")) is False
    assert fwd.state.mark(fwd._key("tool-call", "call_1")) is False


async def test_seed_dedupe_from_history_swallows_errors() -> None:
    """A history-fetch failure leaves the dedupe empty rather than raising."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def _boom(_sid: str) -> list[dict[str, Any]]:
        raise RuntimeError("history unavailable")

    opencode.list_messages = _boom  # type: ignore[assignment]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()  # best-effort → no raise
    assert fwd._msg_role == {}


async def test_compaction_started_posts_in_progress() -> None:
    """`session.next.compaction.started` → external_compaction_status in_progress."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event("session.next.compaction.started", messageID="msg_1", reason="auto")
    )
    body = next(b for _u, b in server.posts if b["type"] == "external_compaction_status")
    assert body["data"]["status"] == "in_progress"


async def test_compaction_ended_posts_completed() -> None:
    """`session.next.compaction.ended` → external_compaction_status completed."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.next.compaction.ended",
            messageID="msg_1",
            reason="manual",
            text="summary",
            recent="tail",
        )
    )
    body = next(b for _u, b in server.posts if b["type"] == "external_compaction_status")
    assert body["data"]["status"] == "completed"


async def test_session_compacted_posts_completed() -> None:
    """Explicit /summarize emits `session.compacted` → external_compaction_status completed."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.compacted"))
    body = next(b for _u, b in server.posts if b["type"] == "external_compaction_status")
    assert body["data"]["status"] == "completed"


async def test_assistant_usage_posts_external_session_usage() -> None:
    """message.updated assistant cost/tokens → external_session_usage (cumulative)."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "message.updated",
            info={
                "id": "msg_a",
                "role": "assistant",
                "modelID": "claude-sonnet-4-5",
                "providerID": "anthropic",
                "cost": 0.012,
                "tokens": {"input": 1000, "output": 50, "cache": {"read": 200, "write": 0}},
            },
        )
    )
    usage = next(b for _u, b in server.posts if b["type"] == "external_session_usage")["data"]
    assert usage["cumulative_cost_usd"] == 0.012
    assert usage["cumulative_input_tokens"] == 1000
    assert usage["cumulative_output_tokens"] == 50
    assert usage["cumulative_cache_read_input_tokens"] == 200
    assert usage["context_tokens"] == 1200  # input + cache.read + cache.write
    assert usage["model"] == "anthropic/claude-sonnet-4-5"
    assert usage["context_window"] > 0


async def test_usage_sums_across_messages_and_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)

    def msg(mid: str, cost: float, inp: int) -> dict[str, object]:
        return {
            "id": mid,
            "role": "assistant",
            "modelID": "m",
            "providerID": "p",
            "cost": cost,
            "tokens": {"input": inp, "output": 1},
        }

    await fwd.handle_event(_event("message.updated", info=msg("m1", 0.01, 100)))
    await fwd.handle_event(_event("message.updated", info=msg("m2", 0.02, 200)))
    usages = [b["data"] for _u, b in server.posts if b["type"] == "external_session_usage"]
    assert usages[-1]["cumulative_cost_usd"] == 0.03  # 0.01 + 0.02
    assert usages[-1]["cumulative_input_tokens"] == 300
    # Re-posting the same final message must dedupe (no new identical post).
    before = len(usages)
    await fwd.handle_event(_event("message.updated", info=msg("m2", 0.02, 200)))
    after = len([b for _u, b in server.posts if b["type"] == "external_session_usage"])
    assert after == before


async def test_model_switched_mirrors_to_omnigent_and_dedupes() -> None:
    """TUI model switch → external_model_change (deduped)."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.next.model.switched", model={"providerID": "anthropic", "id": "claude-opus-4"}
        )
    )
    changes = [b["data"] for _u, b in server.posts if b["type"] == "external_model_change"]
    assert changes[-1]["model"] == "anthropic/claude-opus-4"
    # Same model again → no duplicate post.
    before = len(changes)
    await fwd.handle_event(
        _event(
            "session.next.model.switched", model={"providerID": "anthropic", "id": "claude-opus-4"}
        )
    )
    after = len([b for _u, b in server.posts if b["type"] == "external_model_change"])
    assert after == before


async def test_reasoning_part_streams_suffix_deltas() -> None:
    """opencode reasoning parts → transient reasoning deltas (suffix-only)."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_1", "role": "assistant"}))
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={"id": "prt_r", "messageID": "msg_1", "type": "reasoning", "text": "Let me"},
        )
    )
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={
                "id": "prt_r",
                "messageID": "msg_1",
                "type": "reasoning",
                "text": "Let me think",
            },
        )
    )
    deltas = [
        b["data"] for _u, b in server.posts if b["type"] == "external_output_reasoning_delta"
    ]
    # First snapshot opens the block (started); second posts only the new suffix.
    assert deltas[0] == {"delta": "Let me", "started": True}
    assert deltas[1] == {"delta": " think", "started": False}


async def test_reasoning_part_no_repost_when_unchanged() -> None:
    """A repeated identical reasoning snapshot posts no new delta."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_1", "role": "assistant"}))
    part = {"id": "prt_r", "messageID": "msg_1", "type": "reasoning", "text": "stable"}
    await fwd.handle_event(_event("message.part.updated", part=part))
    await fwd.handle_event(_event("message.part.updated", part=dict(part)))
    deltas = [b for _u, b in server.posts if b["type"] == "external_output_reasoning_delta"]
    assert len(deltas) == 1


async def test_image_file_part_posts_image_block() -> None:
    """An image ``file`` part → an input/output_image content block."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_u", "role": "user"}))
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={
                "id": "prt_f",
                "messageID": "msg_u",
                "type": "file",
                "mime": "image/png",
                "url": "data:image/png;base64,AAAA",
            },
        )
    )
    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    content = items[-1]["data"]["item_data"]["content"][0]
    assert content == {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    assert items[-1]["data"]["item_data"]["role"] == "user"


async def test_non_image_file_part_text_flattened() -> None:
    """A non-image ``file`` part → a short text reference (text-flattened)."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_a", "role": "assistant"}))
    await fwd.handle_event(
        _event(
            "message.part.updated",
            part={
                "id": "prt_f2",
                "messageID": "msg_a",
                "type": "file",
                "mime": "application/pdf",
                "url": "file:///tmp/report.pdf",
                "filename": "report.pdf",
            },
        )
    )
    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    block = items[-1]["data"]["item_data"]["content"][0]
    assert block["type"] == "output_text"
    assert "report.pdf" in block["text"]
    assert items[-1]["data"]["item_data"]["agent"] == "opencode"


async def test_file_part_dedupes_across_snapshots() -> None:
    """A file part posts once even when the part updates repeatedly."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("message.updated", info={"id": "msg_u", "role": "user"}))
    part = {
        "id": "prt_f",
        "messageID": "msg_u",
        "type": "file",
        "mime": "image/jpeg",
        "url": "data:image/jpeg;base64,ZZZZ",
    }
    await fwd.handle_event(_event("message.part.updated", part=part))
    await fwd.handle_event(_event("message.part.updated", part=dict(part)))
    items = [b for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert len(items) == 1
