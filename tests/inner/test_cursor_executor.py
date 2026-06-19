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
    _normalize_cursor_usage,
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
    TurnCancelled,
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
        "custom_tool_results": [],
        "launch_kwargs": [],
        "sent": [],
        "closed": 0,
        "client_closed": 0,
        "agent_closed": 0,
    }

    class _FakeRun:
        def __init__(self, script: dict[str, Any]) -> None:
            self._script = script

        async def events(self) -> Any:
            for message in self._script.get("messages", []):
                yield SimpleNamespace(sdk_message=message, interaction_update=None)
            for iu in self._script.get("interaction_updates", []):
                yield SimpleNamespace(sdk_message=None, interaction_update=iu)

        async def cancel(self) -> None:
            pass  # no-op for tests

        async def wait(self) -> Any:
            return SimpleNamespace(
                status=self._script.get("status", "finished"),
                result=self._script.get("result", ""),
            )

    class _FakeAgent:
        def __init__(self, custom_tools: dict[str, Any]) -> None:
            self._custom_tools = custom_tools

        async def send(self, prompt: str, **kwargs: Any) -> _FakeRun:
            state["sent"].append(prompt)
            script = scripts.pop(0)
            # Invoke on_delta for interaction_updates (mirrors real SDK
            # which dispatches TurnEndedUpdate via on_delta, not events).
            options = kwargs.get("options")
            on_delta = getattr(options, "on_delta", None) if options else None
            if on_delta and "interaction_updates" in script:
                for iu in script["interaction_updates"]:
                    on_delta(iu)
            for call in script.get("custom_tool_calls", []):
                tool = self._custom_tools[call["name"]]
                result = await asyncio.to_thread(
                    tool.execute,
                    call.get("args", {}),
                    call.get("ctx"),
                )
                state["custom_tool_results"].append(result)
            return _FakeRun(script)

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
            return _FakeAgent(dict(local.custom_tools or {}))

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

    class _FakeSendOptions:
        def __init__(self, on_delta: Any = None, **_kw: Any) -> None:
            self.on_delta = on_delta

    fake = types.ModuleType("cursor_sdk")
    fake.AsyncClient = _FakeClient  # type: ignore[attr-defined]
    fake.AsyncAgent = _FakeAsyncAgent  # type: ignore[attr-defined]
    fake.CustomTool = _FakeCustomTool  # type: ignore[attr-defined]
    fake.LocalAgentOptions = _FakeLocalAgentOptions  # type: ignore[attr-defined]
    fake.SendOptions = _FakeSendOptions  # type: ignore[attr-defined]
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


def test_resolve_model_maps_cursor_display_labels_to_sdk_ids() -> None:
    """The in-chat / `/model` switcher can carry a Cursor display LABEL
    (``Composer``) rather than the SDK id (``composer-2.5``). The SDK rejects
    the label with ``invalid_argument``, so _resolve_model must map known
    display labels to their SDK ids before agent creation (issue #547)."""
    # The reported case: the friendly label maps to its SDK id, case-insensitively.
    assert _resolve_model("Composer") == "composer-2.5"
    assert _resolve_model("composer") == "composer-2.5"
    assert _resolve_model("  COMPOSER  ") == "composer-2.5"
    # Capitalized "Auto"/"Default" normalize to the SDK's lowercase ids.
    assert _resolve_model("Auto") == "auto"
    assert _resolve_model("Default") == "default"
    # A real SDK id passes through unchanged (no drift-prone allow-list that
    # would silently downgrade a valid-but-unlisted model to auto).
    assert _resolve_model("composer-2.5") == "composer-2.5"
    assert _resolve_model("claude-opus-4-8") == "claude-opus-4-8"
    assert _resolve_model("gpt-5.4") == "gpt-5.4"


def test_resolve_model_warns_when_dropping_a_pinned_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping an explicit (non-cursor) model must warn, not whisper at debug —
    otherwise a user who pinned a non-Cursor model has no idea it was ignored."""
    import logging

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.cursor_executor"):
        assert _resolve_model("databricks-claude-opus-4-8") == "auto"
    assert any(
        r.levelno == logging.WARNING and "not a Cursor model" in r.getMessage()
        for r in caplog.records
    )
    # No warning when there was no explicit model to honor.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="omnigent.inner.cursor_executor"):
        assert _resolve_model(None) == "auto"
    assert not caplog.records


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
    assert done[0].metadata == {"call_id": "c1", "is_bridged": True}

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


async def test_run_turn_separates_text_across_a_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-tool and post-tool narration are distinct segments: a paragraph break
    is inserted so they don't render as one run-on string. (Streamed deltas with
    no tool between — see the test above — still concatenate seamlessly.)"""
    script = {
        "messages": [
            _assistant("Let me check that."),
            _tool("sys_x", "t1", "running", args={}),
            _tool("sys_x", "t1", "completed", result="ok"),
            _assistant("Done - exit 0."),
        ],
        "result": "",
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    texts = [e.text for e in events if isinstance(e, TextChunk)]
    assert texts == ["Let me check that.", "\n\nDone - exit 0."]  # post-tool text separated
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes[0].response == "Let me check that.\n\nDone - exit 0."


async def test_run_turn_separator_guarantees_blank_line_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The break must be a real blank line even when the pre-tool text already
    ends in a single space or newline (which previously suppressed the separator,
    leaving a run-on or a single-newline join)."""
    scripts = [
        {  # pre-tool text ends with a trailing space
            "messages": [
                _assistant("Checking. "),
                _tool("x", "t1", "running", args={}),
                _tool("x", "t1", "completed", result="ok"),
                _assistant("Done."),
            ],
            "result": "",
        },
        {  # pre-tool text ends with a single newline
            "messages": [
                _assistant("Checking.\n"),
                _tool("x", "t2", "running", args={}),
                _tool("x", "t2", "completed", result="ok"),
                _assistant("Done."),
            ],
            "result": "",
        },
    ]
    _install_fake_sdk(monkeypatch, scripts)
    executor = CursorExecutor(api_key="crsr_x")
    try:
        ev_space = [e async for e in executor.run_turn([_user("a", "s1")], [], "SYS")]
        ev_newline = [e async for e in executor.run_turn([_user("b", "s2")], [], "SYS")]
    finally:
        await executor.close()
    resp_space = next(e.response for e in ev_space if isinstance(e, TurnComplete))
    resp_newline = next(e.response for e in ev_newline if isinstance(e, TurnComplete))
    assert resp_space == "Checking. \n\nDone."  # trailing space -> still a blank line
    assert resp_newline == "Checking.\n\nDone."  # single \n upgraded to a blank line


async def test_run_turn_final_response_prefers_separated_streamed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TurnComplete.response must use the separator-corrected streamed text, not
    the SDK's aggregate ``result`` (which lacks the paragraph break) — so direct
    consumers of the final response see the same separation as the stream."""
    script = {
        "messages": [
            _assistant("Pre."),
            _tool("x", "t1", "running", args={}),
            _tool("x", "t1", "completed", result="ok"),
            _assistant("Post."),
        ],
        "result": "Pre.Post.",  # the SDK's glued aggregate, with no separator
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes[0].response == "Pre.\n\nPost."  # separated, not the glued "Pre.Post."


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


def _bridged_execute(tool_executor: Any) -> Any:
    """Wire *tool_executor* onto a CursorExecutor and return its sync ``execute``."""
    executor = CursorExecutor(api_key="crsr_x")
    executor._tool_executor = tool_executor
    return executor._make_execute("sys_session_send", asyncio.get_running_loop())


async def test_custom_tool_execute_flags_error_dict_with_iserror() -> None:
    """A dispatch failure ({"error": ...}) must surface to the model as an SDK
    error (isError), not an apparently-successful result."""

    async def err(name: str, args: dict[str, Any]) -> Any:
        return {"error": "dispatch failed"}

    result = await asyncio.to_thread(_bridged_execute(err), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "dispatch failed" in result["content"][0]["text"]


async def test_custom_tool_execute_flags_blocked_dict_with_iserror() -> None:
    """A policy-blocked result ({"blocked": True}) is delivered as an error."""

    async def blocked(name: str, args: dict[str, Any]) -> Any:
        return {"blocked": True, "reason": "policy"}

    result = await asyncio.to_thread(_bridged_execute(blocked), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "policy" in result["content"][0]["text"]


async def test_custom_tool_execute_success_dict_is_not_flagged() -> None:
    """An ordinary result is returned as text (a str the SDK treats as success),
    never flagged as an error."""

    async def ok(name: str, args: dict[str, Any]) -> Any:
        return {"ok": True, "value": 42}

    result = await asyncio.to_thread(_bridged_execute(ok), {}, None)
    assert isinstance(result, str)
    assert json.loads(result) == {"ok": True, "value": 42}


@pytest.mark.parametrize(
    ("tool_result", "expected_text"),
    [
        ({"cancelled": True, "reason": "user aborted"}, "user aborted"),
        ({"content": [{"error": "inner failure"}]}, "inner failure"),
        ({"result": {"blocked": True, "reason": "nested policy"}}, "nested policy"),
    ],
)
async def test_run_turn_custom_tool_callback_flags_classifier_failures_as_iserror(
    monkeypatch: pytest.MonkeyPatch,
    tool_result: dict[str, Any],
    expected_text: str,
) -> None:
    """End-to-end executor coverage for the Cursor SDK custom-tool callback.

    The fake SDK drives ``run_turn`` through agent creation, registered custom
    tools, the off-loop sync ``execute`` callback, and ``_encode_tool_result``.
    That pins the bridge contract that Cursor receives an SDK ``isError``
    payload for every non-SUCCESS shape recognized by ``classify_tool_result``.
    """
    script = {
        "messages": [_assistant("Done.")],
        "custom_tool_calls": [
            {"name": "sys_session_send", "args": {"message": "go"}},
        ],
        "status": "finished",
        "result": "Done.",
    }
    state = _install_fake_sdk(monkeypatch, [script])
    tools = [
        {
            "name": "sys_session_send",
            "description": "dispatch",
            "parameters": {"type": "object"},
        }
    ]

    async def fake_tool_executor(name: str, args: dict[str, Any]) -> Any:
        assert name == "sys_session_send"
        assert args == {"message": "go"}
        return tool_result

    executor = CursorExecutor(api_key="crsr_x")
    executor._tool_executor = fake_tool_executor
    try:
        events = [e async for e in executor.run_turn([_user("hi")], tools, "SYS")]
    finally:
        await executor.close()

    assert any(isinstance(e, TurnComplete) for e in events)
    assert len(state["custom_tool_results"]) == 1
    encoded = state["custom_tool_results"][0]
    assert isinstance(encoded, dict) and encoded["isError"] is True
    assert expected_text in encoded["content"][0]["text"]


async def test_custom_tool_execute_flags_cancelled_dict_with_iserror() -> None:
    """A cancelled result ({"cancelled": True}) is non-SUCCESS per
    ``classify_tool_result`` and must surface as an error - the old top-level
    error/blocked check let it through as an apparently-successful result."""

    async def cancelled(name: str, args: dict[str, Any]) -> Any:
        return {"cancelled": True, "reason": "user aborted"}

    result = await asyncio.to_thread(_bridged_execute(cancelled), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "user aborted" in result["content"][0]["text"]


async def test_custom_tool_execute_flags_nested_error_with_iserror() -> None:
    """An error nested inside a ``content`` envelope (not a top-level ``error``
    key) is classified non-SUCCESS and must surface as an error - parity with
    ``classify_tool_result``, which the top-level-only check diverged from."""

    async def nested(name: str, args: dict[str, Any]) -> Any:
        return {"content": [{"error": "inner failure"}]}

    result = await asyncio.to_thread(_bridged_execute(nested), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "inner failure" in result["content"][0]["text"]


async def test_custom_tool_execute_flags_nested_blocked_with_iserror() -> None:
    """A policy block nested under ``result`` is surfaced as an error, matching
    ``classify_tool_result``'s recursion into envelope keys."""

    async def nested(name: str, args: dict[str, Any]) -> Any:
        return {"result": {"blocked": True, "reason": "nested policy"}}

    result = await asyncio.to_thread(_bridged_execute(nested), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "nested policy" in result["content"][0]["text"]


async def test_custom_tool_execute_flags_top_level_list_error_with_iserror() -> None:
    """A top-level list whose element carries an ``error`` is classified
    non-SUCCESS — ``classify_tool_result`` recurses through list elements, so the
    list-shaped payload must surface as an error too."""

    async def list_err(name: str, args: dict[str, Any]) -> Any:
        return [{"error": "list element failure"}]

    result = await asyncio.to_thread(_bridged_execute(list_err), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "list element failure" in result["content"][0]["text"]


async def test_custom_tool_execute_flags_nested_list_error_with_iserror() -> None:
    """An error inside a list nested under an envelope key (``content``) is
    classified non-SUCCESS, matching ``classify_tool_result``'s recursion through
    both envelope keys and list elements."""

    async def nested_list(name: str, args: dict[str, Any]) -> Any:
        return {"content": [{"error": "nested list failure"}]}

    result = await asyncio.to_thread(_bridged_execute(nested_list), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "nested list failure" in result["content"][0]["text"]


async def test_custom_tool_execute_times_out_to_iserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool that never completes must not block the daemon thread forever — the
    bounded wait surfaces a timeout tool error instead of hanging."""
    monkeypatch.setattr("omnigent.inner.cursor_executor._TOOL_CALL_TIMEOUT_S", 0.05)

    async def slow(name: str, args: dict[str, Any]) -> Any:
        await asyncio.sleep(30)
        return "never"

    result = await asyncio.to_thread(_bridged_execute(slow), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "timed out" in result["content"][0]["text"]


async def test_custom_tool_execute_surfaces_coroutine_exception_as_iserror() -> None:
    """A raising coroutine becomes a structured tool error, not an uncaught
    exception on the SDK's daemon callback thread."""

    async def boom(name: str, args: dict[str, Any]) -> Any:
        raise RuntimeError("kaboom")

    result = await asyncio.to_thread(_bridged_execute(boom), {}, None)
    assert isinstance(result, dict) and result["isError"] is True
    assert "kaboom" in result["content"][0]["text"]


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


async def test_mid_turn_expired_status_is_retryable_and_drops_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``expired`` terminal status (Cursor-side timeout / usage cap / quota)
    must surface as a retryable ExecutorError and drop the session — never a
    TurnComplete committing whatever partial text streamed."""
    scripts = [
        {"messages": [_assistant("partial")], "status": "expired", "result": "quota hit"},
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
    assert "expired" in errors[0].message
    # No TurnComplete — the partial text must not be committed as a success.
    assert not any(isinstance(e, TurnComplete) for e in turn1)
    # Session was dropped on expiry, so turn 2 creates a fresh agent.
    assert len(state["create_models"]) == 2
    assert any(isinstance(e, TurnComplete) for e in turn2)


async def test_mid_turn_cancelled_status_emits_turn_cancelled_and_drops_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``cancelled`` terminal status must surface as a TurnCancelled (not a
    TurnComplete) and drop the session, so partial text isn't persisted as a
    legitimate assistant message."""
    scripts = [
        {"messages": [_assistant("partial")], "status": "cancelled", "result": "stopped"},
        {"messages": [_assistant("recovered")], "result": "recovered"},
    ]
    state = _install_fake_sdk(monkeypatch, scripts)
    executor = CursorExecutor(api_key="crsr_x")
    try:
        turn1 = [e async for e in executor.run_turn([_user("first")], [], "SYS")]
        turn2 = [e async for e in executor.run_turn([_user("second")], [], "SYS")]
    finally:
        await executor.close()

    cancels = [e for e in turn1 if isinstance(e, TurnCancelled)]
    assert len(cancels) == 1
    # Cancellation is not an error, and must not be committed as a completed turn.
    assert not any(isinstance(e, ExecutorError) for e in turn1)
    assert not any(isinstance(e, TurnComplete) for e in turn1)
    # Session was dropped on cancellation, so turn 2 creates a fresh agent.
    assert len(state["create_models"]) == 2
    assert any(isinstance(e, TurnComplete) for e in turn2)


async def test_mid_turn_unknown_non_finished_status_is_retryable_and_drops_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``finished`` is allowed to produce TurnComplete. If the SDK adds a
    new terminal status, fail loud and retry instead of silently committing
    partial streamed text as a successful assistant turn."""
    scripts = [
        {"messages": [_assistant("partial")], "status": "paused", "result": "new state"},
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
    assert "non-finished status 'paused'" in errors[0].message
    assert not any(isinstance(e, TurnComplete) for e in turn1)
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


# ---------------------------------------------------------------------------
# Usage / cost tracking
# ---------------------------------------------------------------------------


def test_normalize_cursor_usage_camel_case() -> None:
    raw = {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150}
    result = _normalize_cursor_usage(raw, "cursor-fast")
    assert result == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "model": "cursor-fast",
    }


def test_normalize_cursor_usage_snake_case() -> None:
    raw = {"input_tokens": 200, "output_tokens": 80}
    result = _normalize_cursor_usage(raw, "auto")
    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 80
    assert result["total_tokens"] == 280  # computed from in + out


def test_normalize_cursor_usage_includes_cache_fields() -> None:
    raw = {
        "inputTokens": 500,
        "outputTokens": 100,
        "totalTokens": 600,
        "cacheReadInputTokens": 300,
        "cacheCreationInputTokens": 50,
    }
    result = _normalize_cursor_usage(raw, "auto")
    assert result["cache_read_input_tokens"] == 300
    assert result["cache_creation_input_tokens"] == 50


async def test_run_turn_captures_usage_from_turn_ended_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a TurnEndedUpdate with usage appears in the event stream, the
    TurnComplete event carries the normalized usage dict and
    _notify_usage_from_dict is called."""
    turn_ended = SimpleNamespace(
        type="turn-ended",
        usage={"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200},
    )
    script = {
        "messages": [_assistant("Hello")],
        "interaction_updates": [turn_ended],
        "status": "finished",
        "result": "Hello",
    }
    _install_fake_sdk(monkeypatch, [script])

    notified: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.inner.cursor_executor._notify_usage_from_dict",
        lambda *, model, usage: notified.append({"model": model, "usage": usage}),
    )

    executor = CursorExecutor(api_key="crsr_x")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    usage = completes[0].usage
    assert usage is not None
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 200
    assert usage["total_tokens"] == 1200
    assert usage["model"] == "auto"

    # _notify_usage_from_dict was called with the same data.
    assert len(notified) == 1
    assert notified[0]["model"] == "auto"
    assert notified[0]["usage"] == usage


async def test_run_turn_usage_none_when_no_turn_ended_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a TurnEndedUpdate, usage stays None (backward-compatible)."""
    script = {
        "messages": [_assistant("Hi")],
        "status": "finished",
        "result": "Hi",
    }
    _install_fake_sdk(monkeypatch, [script])

    notified: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.inner.cursor_executor._notify_usage_from_dict",
        lambda *, model, usage: notified.append({"model": model, "usage": usage}),
    )

    executor = CursorExecutor(api_key="crsr_x")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    assert completes[0].usage is None
    assert notified == []  # not called when there is no usage


def test_normalize_cursor_usage_camel_takes_priority_over_snake() -> None:
    raw = {"inputTokens": 100, "input_tokens": 999, "outputTokens": 50, "output_tokens": 888}
    result = _normalize_cursor_usage(raw, "auto")
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 50


def test_normalize_cursor_usage_zero_tokens_preserved() -> None:
    raw = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
    result = _normalize_cursor_usage(raw, "auto")
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["total_tokens"] == 0


# ---------------------------------------------------------------------------
# PHASE_TOOL_CALL policy for native tools
# ---------------------------------------------------------------------------


def test_sdk_message_to_events_marks_native_tool_not_bridged() -> None:
    """A plain (non-MCP-wrapped) tool call has ``is_bridged=False`` in metadata."""
    events = _sdk_message_to_events(_tool("bash", "t1", "running", args={"cmd": "ls"}))
    assert len(events) == 1
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].metadata["is_bridged"] is False

    # Completed status too.
    done = _sdk_message_to_events(
        _tool("bash", "t1", "completed", args={"cmd": "ls"}, result="ok")
    )
    assert isinstance(done[0], ToolCallComplete)
    assert done[0].metadata["is_bridged"] is False


def test_sdk_message_to_events_marks_mcp_tool_bridged() -> None:
    """An MCP-wrapped tool call has ``is_bridged=True`` in metadata."""
    envelope = SimpleNamespace(
        type="tool_call",
        name="mcp",
        call_id="c1",
        status="running",
        args={
            "providerIdentifier": "custom-user-tools",
            "toolName": "sys_session_send",
            "args": {"session": "s1"},
        },
        result=None,
    )
    events = _sdk_message_to_events(envelope)
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].metadata["is_bridged"] is True

    # Completed too.
    envelope_done = SimpleNamespace(
        type="tool_call",
        name="mcp",
        call_id="c1",
        status="completed",
        args={
            "providerIdentifier": "custom-user-tools",
            "toolName": "sys_session_send",
            "args": {"session": "s1"},
        },
        result="ok",
    )
    done = _sdk_message_to_events(envelope_done)
    assert isinstance(done[0], ToolCallComplete)
    assert done[0].metadata["is_bridged"] is True


async def test_run_turn_native_tool_denied_by_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native tool call triggers PHASE_TOOL_CALL. On DENY the run emits
    ToolCallRequest then ExecutorError and the turn ends."""
    script = {
        "messages": [
            _assistant("Let me run that."),
            _tool("bash", "t1", "running", args={"cmd": "rm -rf /"}),
        ],
        "status": "finished",
        "result": "",
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy("PHASE_TOOL_CALL")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    # The ToolCallRequest is emitted so observers see what was attempted.
    reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    assert len(reqs) == 1
    assert reqs[0].name == "bash"

    # Then an ExecutorError with the denial reason.
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert "denied by policy" in errors[0].message
    assert "bash" in errors[0].message

    # ToolCallRequest appears before ExecutorError, and nothing follows the error.
    req_idx = next(i for i, e in enumerate(events) if isinstance(e, ToolCallRequest))
    err_idx = next(i for i, e in enumerate(events) if isinstance(e, ExecutorError))
    assert req_idx < err_idx
    assert err_idx == len(events) - 1  # error is the last event

    # No TurnComplete — the turn was aborted.
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_run_turn_bridged_tool_skips_tool_call_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridged (MCP-wrapped) tool does NOT trigger PHASE_TOOL_CALL — it's
    already gated server-side via the dispatch bridge."""
    # Build an MCP-envelope tool call (bridged).
    mcp_running = SimpleNamespace(
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
    mcp_done = SimpleNamespace(
        type="tool_call",
        name="mcp",
        call_id="c1",
        status="completed",
        args={
            "providerIdentifier": "custom-user-tools",
            "toolName": "sys_session_send",
            "args": {"session": "s1", "message": "go"},
        },
        result="ok",
    )
    script = {
        "messages": [_assistant("Dispatching."), mcp_running, mcp_done, _assistant("Done.")],
        "status": "finished",
        "result": "Done.",
    }
    _install_fake_sdk(monkeypatch, [script])

    # Wire a policy that denies PHASE_TOOL_CALL — if it fires, the turn would abort.
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy("PHASE_TOOL_CALL")
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    # Verify the bridged tool call was actually observed (not silently dropped).
    reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    assert len(reqs) == 1 and reqs[0].name == "sys_session_send"

    # The turn completes normally — the bridged tool was NOT policy-gated here.
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ExecutorError) for e in events)


async def test_run_turn_native_tool_allowed_by_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """When PHASE_TOOL_CALL returns ALLOW, the turn proceeds normally."""
    script = {
        "messages": [
            _assistant("Running."),
            _tool("bash", "t1", "running", args={"cmd": "echo hi"}),
            _tool("bash", "t1", "completed", result="hi"),
            _assistant("Done."),
        ],
        "status": "finished",
        "result": "Done.",
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy(None)  # never denies
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ExecutorError) for e in events)
    # The tool call went through.
    reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    assert len(reqs) == 1 and reqs[0].name == "bash"


def _policy_ask(ask_phase: str) -> Any:
    """Build a fake policy evaluator that returns ASK on *ask_phase*, else ALLOW."""

    async def evaluator(phase: str, _data: dict[str, Any]) -> Any:
        action = "POLICY_ACTION_ASK" if phase == ask_phase else "POLICY_ACTION_ALLOW"
        return SimpleNamespace(action=action, reason="approval required by test")

    return evaluator


async def test_run_turn_native_tool_ask_no_handler_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASK with no elicitation handler is fail-closed to DENY."""
    script = {
        "messages": [
            _assistant("Let me check."),
            _tool("bash", "t1", "running", args={"cmd": "ls"}),
        ],
        "status": "finished",
        "result": "",
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy_ask("PHASE_TOOL_CALL")
    # No _elicitation_handler set → fail closed.
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert "auto-denied" in errors[0].message
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_run_turn_native_tool_ask_user_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASK with elicitation handler that approves → turn continues."""
    script = {
        "messages": [
            _assistant("Running."),
            _tool("bash", "t1", "running", args={"cmd": "ls"}),
            _tool("bash", "t1", "completed", result="file.txt"),
            _assistant("Done."),
        ],
        "status": "finished",
        "result": "Done.",
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy_ask("PHASE_TOOL_CALL")

    async def _approve(_name: str, _args: dict[str, Any]) -> bool:
        return True

    executor._elicitation_handler = _approve
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ExecutorError) for e in events)


async def test_run_turn_native_tool_ask_user_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASK with elicitation handler that denies → turn aborted."""
    script = {
        "messages": [
            _assistant("Running."),
            _tool("bash", "t1", "running", args={"cmd": "rm -rf /"}),
        ],
        "status": "finished",
        "result": "",
    }
    _install_fake_sdk(monkeypatch, [script])
    executor = CursorExecutor(api_key="crsr_x")
    executor._policy_evaluator = _policy_ask("PHASE_TOOL_CALL")

    async def _deny(_name: str, _args: dict[str, Any]) -> bool:
        return False

    executor._elicitation_handler = _deny
    try:
        events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert not any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# preToolUse hook: .cursor/hooks.json writing and cleanup
# ---------------------------------------------------------------------------


async def test_ensure_session_writes_hooks_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """After _ensure_session, .cursor/hooks.json exists in the workspace with the
    correct preToolUse config pointing at the hook script."""
    _install_fake_sdk(monkeypatch, [{"messages": [_assistant("ok")], "result": "ok"}])
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setattr("sys.argv", ["runner", "--conversation-id", "conv_test123"])
    cwd = str(tmp_path)
    executor = CursorExecutor(api_key="crsr_x", cwd=cwd)
    _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]

    # Assert BEFORE close (close cleans up the file).
    hooks_file = tmp_path / ".cursor" / "hooks.json"
    assert hooks_file.exists()
    config = json.loads(hooks_file.read_text())
    assert "hooks" in config
    assert "preToolUse" in config["hooks"]
    hooks = config["hooks"]["preToolUse"]
    assert len(hooks) == 1
    assert hooks[0]["timeout"] == 30
    cmd = hooks[0]["command"]
    # The command points to the wrapper shell script, not the Python hook directly.
    assert "omnigent-hook.sh" in cmd

    # Verify the wrapper script exists and contains the env vars + exec.
    wrapper = tmp_path / ".cursor" / "omnigent-hook.sh"
    assert wrapper.exists()
    wrapper_text = wrapper.read_text()
    assert "_OMNIGENT_SERVER_URL='http://127.0.0.1:6767'" in wrapper_text
    assert "_OMNIGENT_SESSION_ID='conv_test123'" in wrapper_text
    assert "cursor_policy_hook.py" in wrapper_text

    await executor.close()
    # Both files are cleaned up on close.
    assert not hooks_file.exists()
    assert not wrapper.exists()


async def test_hooks_json_not_written_without_server_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Without RUNNER_SERVER_URL in env, no hooks.json is written."""
    _install_fake_sdk(monkeypatch, [{"messages": [_assistant("ok")], "result": "ok"}])
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    cwd = str(tmp_path)
    executor = CursorExecutor(api_key="crsr_x", cwd=cwd)
    try:
        _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    finally:
        await executor.close()

    hooks_file = tmp_path / ".cursor" / "hooks.json"
    assert not hooks_file.exists()


async def test_hooks_json_cleaned_up_on_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """hooks.json is removed when the session is closed."""
    _install_fake_sdk(
        monkeypatch,
        [{"messages": [_assistant("ok")], "result": "ok"}],
    )
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setattr("sys.argv", ["runner", "--conversation-id", "conv_cleanup"])
    cwd = str(tmp_path)
    executor = CursorExecutor(api_key="crsr_x", cwd=cwd)
    _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]

    hooks_file = tmp_path / ".cursor" / "hooks.json"
    wrapper = tmp_path / ".cursor" / "omnigent-hook.sh"
    assert hooks_file.exists()
    assert wrapper.exists()

    await executor.close()
    assert not hooks_file.exists()
    assert not wrapper.exists()


# ---------------------------------------------------------------------------
# cursor_policy_hook.py unit tests
# ---------------------------------------------------------------------------


def test_cursor_policy_hook_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook script returns allow when the server responds with ALLOW."""
    import io
    from unittest.mock import patch

    monkeypatch.setenv("_OMNIGENT_SERVER_URL", "http://localhost:6767")
    monkeypatch.setenv("_OMNIGENT_SESSION_ID", "conv_test")

    stdin_data = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    server_response = json.dumps({"result": "POLICY_ACTION_ALLOW", "reason": ""}).encode()

    from omnigent.inner import cursor_policy_hook

    fake_resp = io.BytesIO(server_response)
    fake_resp.read = fake_resp.read  # type: ignore[assignment]
    fake_resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
    fake_resp.__exit__ = lambda s, *a: None  # type: ignore[attr-defined]

    stdout = io.StringIO()
    with (
        patch.object(sys, "stdin", io.StringIO(stdin_data)),
        patch.object(sys, "stdout", stdout),
        patch("urllib.request.urlopen", return_value=fake_resp),
    ):
        cursor_policy_hook.main()

    result = json.loads(stdout.getvalue())
    assert result["permission"] == "allow"


def test_cursor_policy_hook_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook script returns deny when the server responds with DENY."""
    import io
    from unittest.mock import patch

    monkeypatch.setenv("_OMNIGENT_SERVER_URL", "http://localhost:6767")
    monkeypatch.setenv("_OMNIGENT_SESSION_ID", "conv_test")

    stdin_data = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
    server_response = json.dumps(
        {"result": "POLICY_ACTION_DENY", "reason": "dangerous command"}
    ).encode()

    from omnigent.inner import cursor_policy_hook

    fake_resp = io.BytesIO(server_response)
    fake_resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
    fake_resp.__exit__ = lambda s, *a: None  # type: ignore[attr-defined]

    stdout = io.StringIO()
    with (
        patch.object(sys, "stdin", io.StringIO(stdin_data)),
        patch.object(sys, "stdout", stdout),
        patch("urllib.request.urlopen", return_value=fake_resp),
    ):
        cursor_policy_hook.main()

    result = json.loads(stdout.getvalue())
    assert result["permission"] == "deny"
    assert "dangerous command" in result["agent_message"]
    assert "Bash" in result["agent_message"]


def test_cursor_policy_hook_network_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """On network error, the hook script fails open (allows)."""
    import io
    from unittest.mock import patch

    monkeypatch.setenv("_OMNIGENT_SERVER_URL", "http://localhost:6767")
    monkeypatch.setenv("_OMNIGENT_SESSION_ID", "conv_test")

    stdin_data = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})

    from omnigent.inner import cursor_policy_hook

    stdout = io.StringIO()
    with (
        patch.object(sys, "stdin", io.StringIO(stdin_data)),
        patch.object(sys, "stdout", stdout),
        patch("urllib.request.urlopen", side_effect=OSError("connection refused")),
    ):
        cursor_policy_hook.main()

    result = json.loads(stdout.getvalue())
    assert result["permission"] == "allow"


def test_cursor_policy_hook_no_env_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without server URL / session ID env vars, the hook allows."""
    import io
    from unittest.mock import patch

    monkeypatch.delenv("_OMNIGENT_SERVER_URL", raising=False)
    monkeypatch.delenv("_OMNIGENT_SESSION_ID", raising=False)

    from omnigent.inner import cursor_policy_hook

    stdout = io.StringIO()
    with (
        patch.object(sys, "stdin", io.StringIO("{}")),
        patch.object(sys, "stdout", stdout),
    ):
        cursor_policy_hook.main()

    result = json.loads(stdout.getvalue())
    assert result["permission"] == "allow"


def test_cursor_policy_hook_ask_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ASK verdict (unresolved approval) fails closed with deny."""
    import io
    from unittest.mock import patch

    monkeypatch.setenv("_OMNIGENT_SERVER_URL", "http://localhost:6767")
    monkeypatch.setenv("_OMNIGENT_SESSION_ID", "conv_test")

    stdin_data = json.dumps({"tool_name": "Write", "tool_input": {}})
    server_response = json.dumps(
        {"result": "POLICY_ACTION_ASK", "reason": "needs approval"}
    ).encode()

    from omnigent.inner import cursor_policy_hook

    fake_resp = io.BytesIO(server_response)
    fake_resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
    fake_resp.__exit__ = lambda s, *a: None  # type: ignore[attr-defined]

    stdout = io.StringIO()
    with (
        patch.object(sys, "stdin", io.StringIO(stdin_data)),
        patch.object(sys, "stdout", stdout),
        patch("urllib.request.urlopen", return_value=fake_resp),
    ):
        cursor_policy_hook.main()

    result = json.loads(stdout.getvalue())
    assert result["permission"] == "deny"
    assert "requires approval" in result["agent_message"]
