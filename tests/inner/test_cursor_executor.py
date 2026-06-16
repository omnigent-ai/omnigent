"""Tests for :class:`omnigent.inner.cursor_executor.CursorExecutor`.

The cursor harness drives the Cursor Python SDK (``cursor-sdk``). The SDK is
replaced with an injected fake module (so no real bridge subprocess, API key, or
network is needed), letting us exercise the ``SDKMessage`` → ExecutorEvent
mapping, the ``custom_tools`` tool bridge into ``_tool_executor``,
persistent-agent reuse across turns, the ``databricks-*`` model fallback, and
the failure/lifecycle paths. Live end-to-end coverage (a real cursor model
invoking a bridged tool) lives in the gated e2e test.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.inner.cursor_executor import (
    CursorExecutor,
    _build_cursor_prompt,
    _resolve_model,
    _sdk_message_to_events,
)
from omnigent.inner.executor import (
    ExecutorError,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)


def _user(content: str, session_id: str = "conv1") -> Message:
    return {"role": "user", "content": content, "session_id": session_id}


# ---------------------------------------------------------------------------
# Fake cursor_sdk
# ---------------------------------------------------------------------------


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    scripts: list[dict[str, Any]] | None = None,
    *,
    create_exc: Exception | None = None,
) -> dict[str, Any]:
    """Install a fake ``cursor_sdk`` module and return a capture dict.

    *scripts* is one dict per ``agent.send`` — ``{messages: [...], status,
    result}``. ``create_exc`` makes ``AsyncAgent.create`` raise (after the
    bridge launches), to exercise the setup-failure path.
    """
    scripts = scripts if scripts is not None else []
    state: dict[str, Any] = {
        "create_models": [],
        "create_api_keys": [],
        "custom_tools": [],
        "launch_kwargs": [],
        "sent": [],
        "closed": 0,
        "client_closed": 0,
        "agent_closed": 0,
    }

    class _FakeRun:
        def __init__(self, script: dict[str, Any]) -> None:
            self._script = script

        async def messages(self) -> Any:
            for message in self._script.get("messages", []):
                yield message

        async def wait(self) -> Any:
            return SimpleNamespace(
                status=self._script.get("status", "finished"),
                result=self._script.get("result", ""),
            )

    class _FakeAgent:
        async def send(self, prompt: str) -> _FakeRun:
            state["sent"].append(prompt)
            return _FakeRun(scripts.pop(0))

        # AsyncAgent exposes close() (a CloseAgent RPC + tool unregister).
        async def close(self) -> None:
            state["closed"] += 1
            state["agent_closed"] += 1

    class _FakeClient:
        @classmethod
        async def launch_bridge(cls, **kwargs: Any) -> _FakeClient:
            state["launch_kwargs"].append(kwargs)
            return cls()

        # The real AsyncClient exposes ONLY aclose() (no close()); it owns the
        # bridge subprocess + the daemon tool-callback server, both torn down
        # there. Deliberately no close() here so a regression that closes the
        # client via close() fails (AttributeError -> swallowed -> leak).
        async def aclose(self) -> None:
            state["closed"] += 1
            state["client_closed"] += 1

    class _FakeAsyncAgent:
        @classmethod
        async def create(
            cls, *, client: Any, model: Any, api_key: Any, name: Any, local: Any
        ) -> _FakeAgent:
            state["create_models"].append(model)
            state["create_api_keys"].append(api_key)
            state["custom_tools"].append(dict(local.custom_tools or {}))
            if create_exc is not None:
                raise create_exc
            return _FakeAgent()

    class _FakeCustomTool:
        def __init__(
            self, execute: Any, description: Any = None, input_schema: Any = None
        ) -> None:
            self.execute = execute
            self.description = description
            self.input_schema = input_schema

    class _FakeLocalAgentOptions:
        def __init__(self, cwd: Any = None, custom_tools: Any = None, **_kw: Any) -> None:
            self.cwd = cwd
            self.custom_tools = custom_tools

    fake = types.ModuleType("cursor_sdk")
    fake.AsyncClient = _FakeClient  # type: ignore[attr-defined]
    fake.AsyncAgent = _FakeAsyncAgent  # type: ignore[attr-defined]
    fake.CustomTool = _FakeCustomTool  # type: ignore[attr-defined]
    fake.LocalAgentOptions = _FakeLocalAgentOptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cursor_sdk", fake)
    return state


def _assistant(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="assistant",
        message=SimpleNamespace(content=[SimpleNamespace(type="text", text=text)]),
    )


def _thinking(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", text=text)


def _tool(
    name: str, call_id: str, status: str, args: Any = None, result: Any = None
) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_call", name=name, call_id=call_id, status=status, args=args, result=result
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_resolve_model_drops_databricks_and_defaults_to_auto() -> None:
    assert _resolve_model("gpt-5") == "gpt-5"
    assert _resolve_model("databricks-claude-sonnet-4-6") == "auto"
    assert _resolve_model("databricks/kimi") == "auto"
    assert _resolve_model(None) == "auto"


def test_sdk_message_to_events_maps_text_thinking_and_tools() -> None:
    assert isinstance(_sdk_message_to_events(_assistant("hi"))[0], TextChunk)
    think = _sdk_message_to_events(_thinking("hmm"))
    assert isinstance(think[0], ReasoningChunk) and think[0].event_type == "reasoning_text"

    req = _sdk_message_to_events(_tool("Read", "t1", "running", args={"p": 1}))
    assert (
        isinstance(req[0], ToolCallRequest) and req[0].name == "Read" and req[0].args == {"p": 1}
    )

    done = _sdk_message_to_events(
        _tool("Read", "t1", "completed", result=[{"type": "text", "text": "ok"}])
    )
    assert isinstance(done[0], ToolCallComplete)

    err = _sdk_message_to_events(_tool("Read", "t1", "error", result="boom"))
    assert isinstance(err[0], ToolCallComplete) and err[0].status == ToolCallStatus.ERROR

    # Status / unknown messages surface nothing.
    assert _sdk_message_to_events(SimpleNamespace(type="status", status="x")) == []


def test_sdk_message_to_events_unwraps_cursor_custom_tool_envelope() -> None:
    # Cursor surfaces host custom tools wrapped: name == "mcp", with the real
    # tool nested in args. The mapping must unwrap to the actual tool + args.
    envelope = SimpleNamespace(
        type="tool_call",
        name="mcp",
        call_id="c1",
        status="running",
        args={
            "providerIdentifier": "custom-user-tools",
            "toolName": "sys_session_send",
            "args": {"session": "s1", "message": "go"},
        },
        result=None,
    )
    events = _sdk_message_to_events(envelope)
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].name == "sys_session_send"
    assert events[0].args == {"session": "s1", "message": "go"}


def test_sdk_message_to_events_unwraps_envelope_on_completion_and_error() -> None:
    # The same mcp envelope (name == "mcp", real tool nested in args) also arrives
    # on the completed/error branch. The unwrap must apply there too so the
    # ToolCallComplete carries the real tool name (not "mcp") — otherwise any
    # name-keyed request<->complete correlation in policy/UI would break.
    def _envelope(status: str, result: Any) -> SimpleNamespace:
        return SimpleNamespace(
            type="tool_call",
            name="mcp",
            call_id="c1",
            status=status,
            args={
                "providerIdentifier": "custom-user-tools",
                "toolName": "sys_session_send",
                "args": {"session": "s1"},
            },
            result=result,
        )

    done = _sdk_message_to_events(_envelope("completed", [{"type": "text", "text": "ok"}]))
    assert isinstance(done[0], ToolCallComplete)
    assert done[0].name == "sys_session_send"  # unwrapped, not "mcp"
    assert done[0].metadata == {"call_id": "c1"}

    err = _sdk_message_to_events(_envelope("error", "boom"))
    assert isinstance(err[0], ToolCallComplete)
    assert err[0].name == "sys_session_send"
    assert err[0].status == ToolCallStatus.ERROR


def test_build_cursor_prompt_prepends_system_then_drops_it() -> None:
    msgs = [_user("hello")]
    first = _build_cursor_prompt(msgs, is_first_turn=True, system_prompt="SYS")
    assert first == "SYS\n\nhello"
    later = _build_cursor_prompt([_user("again")], is_first_turn=False, system_prompt="SYS")
    assert later == "again"
    empty = _build_cursor_prompt(
        [{"role": "assistant", "content": "x"}], is_first_turn=True, system_prompt=""
    )
    assert empty == ""


def test_capabilities() -> None:
    executor = CursorExecutor()
    assert executor.supports_streaming() is True
    assert executor.supports_tool_calling() is True
    # Tools execute in-band via the SDK custom_tools callback, so the adapter
    # must not re-dispatch — same contract as claude-sdk.
    assert executor.handles_tools_internally() is True
    assert executor.supports_live_message_queue() is False


# ---------------------------------------------------------------------------
# run_turn
# ---------------------------------------------------------------------------


async def test_run_turn_streams_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    script = {
        "messages": [_thinking("planning"), _assistant("Hello "), _assistant("world")],
        "status": "finished",
        "result": "Hello world",
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Hello ", "world"]
    reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
    assert len(reasoning) == 1 and reasoning[0].delta == "planning"
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1 and completes[0].response == "Hello world"
    assert completes[0].usage is None


async def test_session_reused_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = [
        {"messages": [_assistant("one")], "result": "one"},
        {"messages": [_assistant("two")], "result": "two"},
    ]
    state = _install_fake_sdk(monkeypatch, scripts)
    executor = CursorExecutor(api_key="crsr_x")
    try:
        _ = [e async for e in executor.run_turn([_user("first")], [], "SYS")]
        _ = [e async for e in executor.run_turn([_user("second")], [], "SYS")]
    finally:
        await executor.close()
    # The agent is created once and reused on turn 2.
    assert len(state["create_models"]) == 1
    assert len(state["sent"]) == 2


async def test_session_restart_on_system_prompt_change(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = [
        {"messages": [_assistant("one")], "result": "one"},
        {"messages": [_assistant("two")], "result": "two"},
    ]
    state = _install_fake_sdk(monkeypatch, scripts)
    executor = CursorExecutor(api_key="crsr_x")
    try:
        _ = [e async for e in executor.run_turn([_user("first")], [], "SYS-A")]
        _ = [e async for e in executor.run_turn([_user("second")], [], "SYS-B")]
    finally:
        await executor.close()
    # A changed system prompt rebuilds the agent (prompt is baked at creation).
    assert len(state["create_models"]) == 2
    assert state["closed"] >= 1


async def test_databricks_model_resolved_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_sdk(monkeypatch, [{"messages": [_assistant("ok")], "result": "ok"}])
    executor = CursorExecutor(model="databricks-claude-sonnet-4-6", api_key="crsr_x")
    try:
        _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    assert state["create_models"] == ["auto"]


async def test_api_key_threaded_to_create(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_sdk(monkeypatch, [{"messages": [_assistant("ok")], "result": "ok"}])
    executor = CursorExecutor(api_key="crsr_secret")
    try:
        _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    assert state["create_api_keys"] == ["crsr_secret"]


async def test_custom_tools_built_from_tool_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_sdk(monkeypatch, [{"messages": [_assistant("ok")], "result": "ok"}])
    tools = [
        {"name": "sys_session_send", "description": "dispatch", "parameters": {"type": "object"}},
        {"description": "no name — skipped"},
    ]
    executor = CursorExecutor(api_key="crsr_x")
    try:
        _ = [e async for e in executor.run_turn([_user("hi")], tools, "SYS")]
    finally:
        await executor.close()
    registered = state["custom_tools"][0]
    assert list(registered.keys()) == ["sys_session_send"]
    assert registered["sys_session_send"].description == "dispatch"


async def test_custom_tool_execute_bridges_to_tool_executor() -> None:
    """The SDK callback (a sync ``execute`` on a worker thread) must hop back to
    the main loop and resolve Omnigent's async ``_tool_executor``."""
    executor = CursorExecutor(api_key="crsr_x")
    seen: dict[str, Any] = {}

    async def fake_tool_executor(name: str, args: dict[str, Any]) -> Any:
        seen["name"] = name
        seen["args"] = args
        return {"ok": True, "echo": args}

    executor._tool_executor = fake_tool_executor
    loop = asyncio.get_running_loop()
    execute = executor._make_execute("sys_session_send", loop)
    # Call execute off-loop (as the SDK callback thread would); the main loop
    # stays free to resolve the coroutine.
    result = await asyncio.to_thread(execute, {"x": 1}, None)
    assert seen == {"name": "sys_session_send", "args": {"x": 1}}
    assert json.loads(result) == {"ok": True, "echo": {"x": 1}}


async def test_setup_failure_closes_client_and_drops_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_sdk(monkeypatch, [], create_exc=RuntimeError("bad CURSOR_API_KEY"))
    executor = CursorExecutor(api_key="crsr_bad")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and "bad CURSOR_API_KEY" in errors[0].message
    assert "conv1" not in executor._session_states  # session dropped
    # The launched bridge client was torn down via aclose() → no orphaned bridge.
    assert state["closed"] == 1
    assert state["client_closed"] == 1


async def test_close_session_tears_down_bridge_client_via_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal session close must tear the bridge-owning AsyncClient down via
    ``aclose()`` — its only teardown path (it owns the bridge subprocess + the
    daemon tool-callback thread). The real SDK client has no ``close()``, so
    closing via ``close()`` silently leaks; this pins the client to aclose()."""
    state = _install_fake_sdk(monkeypatch, [{"messages": [_assistant("ok")], "result": "ok"}])
    executor = CursorExecutor(api_key="crsr_x")
    _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    assert state["client_closed"] == 0  # still live mid-conversation
    await executor.close()
    # Both the agent (close) and the bridge-owning client (aclose) are released.
    assert state["agent_closed"] == 1
    assert state["client_closed"] == 1


async def test_mid_turn_error_status_drops_session(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = [
        {"messages": [_assistant("partial")], "status": "error", "result": "model exploded"},
        {"messages": [_assistant("recovered")], "result": "recovered"},
    ]
    state = _install_fake_sdk(monkeypatch, scripts)
    executor = CursorExecutor(api_key="crsr_x")
    try:
        turn1 = [e async for e in executor.run_turn([_user("first")], [], "SYS")]
        turn2 = [e async for e in executor.run_turn([_user("second")], [], "SYS")]
    finally:
        await executor.close()

    errors = [e for e in turn1 if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and errors[0].retryable is True
    assert "model exploded" in errors[0].message
    # Session was dropped on the error, so turn 2 creates a fresh agent.
    assert len(state["create_models"]) == 2
    assert any(isinstance(e, TurnComplete) for e in turn2)


async def test_empty_prompt_completes_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_sdk(monkeypatch, [])
    executor = CursorExecutor(api_key="crsr_x")
    try:
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "assistant", "content": "x", "session_id": "conv1"}], [], ""
            )
        ]
    finally:
        await executor.close()
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete) and events[0].response is None
    assert state["sent"] == []  # nothing sent to the agent


async def test_missing_sdk_surfaces_executor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate cursor-sdk not installed: importing it raises ImportError.
    monkeypatch.setitem(sys.modules, "cursor_sdk", None)
    executor = CursorExecutor(api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and "cursor-sdk" in errors[0].message


# ---------------------------------------------------------------------------
# Policy enforcement (PHASE_LLM_REQUEST / PHASE_LLM_RESPONSE)
# ---------------------------------------------------------------------------


def _policy(deny_phase: str | None) -> Any:
    """Build a fake policy evaluator that DENIES on *deny_phase*, else ALLOWs."""

    async def evaluator(phase: str, data: dict[str, Any]) -> Any:
        action = "POLICY_ACTION_DENY" if phase == deny_phase else "POLICY_ACTION_ALLOW"
        return SimpleNamespace(action=action, reason="blocked by test")

    return evaluator


async def test_policy_request_deny_blocks_before_send(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_sdk(monkeypatch, [{"messages": [_assistant("hi")], "result": "hi"}])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy("PHASE_LLM_REQUEST")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and "call denied by policy" in errors[0].message
    assert state["sent"] == []  # blocked before the LLM call
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_policy_response_deny_blocks_turn_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_sdk(monkeypatch, [{"messages": [_assistant("hi")], "result": "hi"}])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy("PHASE_LLM_RESPONSE")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and "response denied by policy" in errors[0].message
    assert state["sent"] != []  # the call happened; the response was blocked after
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_policy_allow_completes_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch, [{"messages": [_assistant("hi")], "result": "hi"}])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy(None)  # never denies
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ExecutorError) for e in events)


# ---------------------------------------------------------------------------
# Tool-set fingerprint invalidation + passed-history serialization
# ---------------------------------------------------------------------------


async def test_changed_tool_set_rebuilds_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = [
        {"messages": [_assistant("one")], "result": "one"},
        {"messages": [_assistant("two")], "result": "two"},
    ]
    state = _install_fake_sdk(monkeypatch, scripts)
    executor = CursorExecutor(api_key="crsr_x")
    tools_a = [{"name": "alpha", "parameters": {"type": "object"}}]
    tools_b = [{"name": "beta", "parameters": {"type": "object"}}]
    try:
        _ = [e async for e in executor.run_turn([_user("first")], tools_a, "SYS")]
        _ = [e async for e in executor.run_turn([_user("second")], tools_b, "SYS")]
    finally:
        await executor.close()
    # A changed tool set must rebuild the agent (custom_tools are fixed at create).
    assert len(state["create_models"]) == 2


def test_build_cursor_prompt_serializes_single_user_history() -> None:
    # pass_history sub-agent: one user message plus prior assistant context.
    messages = [
        {"role": "assistant", "content": "earlier context"},
        {"role": "user", "content": "follow up"},
    ]
    prompt = _build_cursor_prompt(messages, is_first_turn=True, system_prompt="SYS")
    assert "Conversation so far:" in prompt
    assert "earlier context" in prompt and "follow up" in prompt
