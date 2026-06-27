"""Tests for :class:`omnigent.inner.copilot_executor.CopilotExecutor`.

The copilot harness drives the GitHub Copilot SDK (``github-copilot-sdk``,
imported as ``copilot``). The SDK is replaced with an injected fake module (so
no real backing CLI, GitHub token, or network is needed), letting us exercise
the ``SessionEvent`` → ExecutorEvent mapping, the tool bridge into
``_tool_executor``, persistent-session reuse across turns, the ``databricks-*``
model fallback, usage accumulation, and the failure/lifecycle paths. Live
end-to-end coverage (a real Copilot model invoking a bridged tool) lives in the
gated e2e test.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from omnigent.inner.copilot_executor import (
    CopilotExecutor,
    _accumulate_usage,
    _ambient_github_token,
    _build_copilot_prompt,
    _coerce_args,
    _encode_tool_result,
    _event_data,
    _finalize_usage,
    _resolve_model,
)
from omnigent.inner.executor import (
    ExecutorConfig,
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


def _ev(name: str, **data: Any) -> tuple[str, dict[str, Any]]:
    """A scripted (event-type-name, data) pair."""
    return (name, data)


# ---------------------------------------------------------------------------
# Fake copilot SDK
# ---------------------------------------------------------------------------


class _FakeEvent:
    def __init__(self, name: str, data: dict[str, Any]) -> None:
        self.type = f"SessionEventType.{name}"
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": dict(self._data)}


class _FakeSession:
    def __init__(self, state: dict[str, Any], send_exc: Exception | None = None) -> None:
        self._state = state
        self._handlers: list[Any] = []
        self._send_exc = send_exc

    def on(self, handler: Any) -> Any:
        self._handlers.append(handler)

        def unsub() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        self._state["unsub_calls"] = self._state.get("unsub_calls", 0)
        return _Unsub(self, handler)

    async def send_and_wait(self, prompt: str, *, timeout: float = 60.0) -> Any:
        self._state["sent"].append(prompt)
        await asyncio.sleep(0)
        if self._send_exc is not None and self._state.get("send_exc_remaining", 0) > 0:
            self._state["send_exc_remaining"] -= 1
            raise self._send_exc
        scripts: list[list[tuple[str, dict[str, Any]]]] = self._state["turn_scripts"]
        script = scripts.pop(0) if scripts else []
        final = None
        for name, data in script:
            event = _FakeEvent(name, data)
            for handler in list(self._handlers):
                handler(event)
            if event.type.endswith("ASSISTANT_MESSAGE"):
                final = event
        return final

    async def disconnect(self) -> None:
        self._state["session_closed"] += 1

    def abort(self) -> None:
        self._state["aborted"] += 1


class _Unsub:
    def __init__(self, session: _FakeSession, handler: Any) -> None:
        self._session = session
        self._handler = handler

    def __call__(self) -> None:
        self._session._state["unsub_calls"] = self._session._state.get("unsub_calls", 0) + 1
        if self._handler in self._session._handlers:
            self._session._handlers.remove(self._handler)


class _PermissionHandler:
    approve_all = "approve_all"


def _install_fake_copilot(
    monkeypatch: pytest.MonkeyPatch,
    turn_scripts: list[list[tuple[str, dict[str, Any]]]] | None = None,
    *,
    create_exc: Exception | None = None,
    start_exc: Exception | None = None,
    send_exc: Exception | None = None,
    send_exc_times: int = 1,
) -> dict[str, Any]:
    """Install a fake ``copilot`` module; return a capture dict.

    *turn_scripts* is one list of scripted events per ``send_and_wait`` call.
    *send_exc*, when set, makes ``send_and_wait`` raise it for the first
    *send_exc_times* calls (then behave normally), so the mid-turn
    ``send_and_wait`` failure path can be exercised and recovery verified.
    """
    state: dict[str, Any] = {
        "client_kwargs": [],
        "create_kwargs": [],
        "sent": [],
        "started": 0,
        "client_closed": 0,
        "session_closed": 0,
        "aborted": 0,
        "turn_scripts": list(turn_scripts or []),
        "send_exc_remaining": send_exc_times if send_exc is not None else 0,
    }

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            state["client_kwargs"].append(kwargs)

        async def start(self) -> None:
            state["started"] += 1
            if start_exc is not None:
                raise start_exc

        async def stop(self) -> None:
            state["client_closed"] += 1

        async def create_session(self, **kwargs: Any) -> _FakeSession:
            state["create_kwargs"].append(kwargs)
            if create_exc is not None:
                raise create_exc
            return _FakeSession(state, send_exc=send_exc)

    class _Tool:
        def __init__(
            self,
            name: str,
            description: str,
            handler: Any = None,
            parameters: Any = None,
            overrides_built_in_tool: bool = False,
            skip_permission: bool = False,
        ) -> None:
            self.name = name
            self.description = description
            self.handler = handler
            self.parameters = parameters
            self.skip_permission = skip_permission

    class _ToolResult:
        def __init__(
            self,
            text_result_for_llm: str = "",
            result_type: str = "success",
            error: str | None = None,
            **_: Any,
        ) -> None:
            self.text_result_for_llm = text_result_for_llm
            self.result_type = result_type
            self.error = error

    module = types.ModuleType("copilot")
    module.CopilotClient = _FakeClient  # type: ignore[attr-defined]
    module.Tool = _Tool  # type: ignore[attr-defined]
    module.ToolResult = _ToolResult  # type: ignore[attr-defined]
    module.PermissionHandler = _PermissionHandler  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "copilot", module)
    return state


# ---------------------------------------------------------------------------
# Pure helpers (no SDK needed)
# ---------------------------------------------------------------------------


def test_resolve_model_passthrough_and_databricks_drop() -> None:
    assert _resolve_model(None) is None
    assert _resolve_model("claude-haiku-4.5") == "claude-haiku-4.5"
    assert _resolve_model("databricks-claude-opus-4-8") is None


def test_build_prompt_first_turn_history_and_latest() -> None:
    # Multi-message first turn serializes history.
    msgs = [_user("first"), {"role": "assistant", "content": "ok"}, _user("second")]
    prompt = _build_copilot_prompt(msgs, is_first_turn=True)
    assert "Conversation so far:" in prompt and "second" in prompt
    # Single message: just the latest user text.
    assert _build_copilot_prompt([_user("hi")], is_first_turn=True) == "hi"
    assert _build_copilot_prompt([_user("again")], is_first_turn=False) == "again"


def test_coerce_args() -> None:
    assert _coerce_args({"a": 1}) == {"a": 1}
    assert _coerce_args('{"a": 1}') == {"a": 1}
    assert _coerce_args("not json") == {}
    assert _coerce_args(None) == {}
    assert _coerce_args("[1,2]") == {}  # non-dict json


def test_event_data_reads_to_dict() -> None:
    assert _event_data(_FakeEvent("ASSISTANT_MESSAGE_DELTA", {"deltaContent": "x"})) == {
        "deltaContent": "x"
    }


def test_usage_accumulation_and_finalize() -> None:
    acc: dict[str, int] = {}
    _accumulate_usage(
        acc,
        {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2, "cacheWriteTokens": 7},
    )
    _accumulate_usage(acc, {"inputTokens": 3, "outputTokens": 1})
    usage = _finalize_usage(acc)
    assert usage == {
        "input_tokens": 13,
        "output_tokens": 6,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 7,
        "total_tokens": 19,
    }
    assert _finalize_usage({}) is None


def test_ambient_github_token_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert _ambient_github_token() is None
    monkeypatch.setenv("GITHUB_TOKEN", "gho_c")
    monkeypatch.setenv("GH_TOKEN", "gho_b")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_a")
    assert _ambient_github_token() == "gho_a"


def test_capabilities() -> None:
    ex = CopilotExecutor()
    assert ex.supports_streaming() is True
    assert ex.supports_tool_calling() is True
    assert ex.handles_tools_internally() is True
    assert ex.supports_live_message_queue() is False


# ---------------------------------------------------------------------------
# Tool-result encoding + bridge (needs the fake ToolResult)
# ---------------------------------------------------------------------------


def test_encode_tool_result_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_copilot(monkeypatch)
    ok = _encode_tool_result("plain text")
    assert ok.text_result_for_llm == "plain text" and ok.result_type == "success"
    err = _encode_tool_result({"error": "boom"})
    assert err.result_type == "failure" and "boom" in err.error
    blocked = _encode_tool_result({"blocked": True, "reason": "policy"})
    assert blocked.result_type == "failure"
    js = _encode_tool_result({"value": 1})
    assert js.result_type == "success" and "value" in js.text_result_for_llm


@pytest.mark.asyncio
async def test_bridged_tool_handler_routes_to_tool_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_copilot(monkeypatch)
    ex = CopilotExecutor()
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(name: str, args: dict[str, Any]) -> Any:
        seen.append((name, args))
        return {"ok": True}

    ex._tool_executor = fake_exec
    handler = ex._make_handler("sys_session_send")
    invocation = types.SimpleNamespace(arguments={"x": 1}, tool_call_id="c1")
    result = await handler(invocation)
    assert seen == [("sys_session_send", {"x": 1})]
    assert result.result_type == "success"


@pytest.mark.asyncio
async def test_bridged_tool_handler_surfaces_exception_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_copilot(monkeypatch)
    ex = CopilotExecutor()

    async def boom(name: str, args: dict[str, Any]) -> Any:
        raise RuntimeError("kaboom")

    ex._tool_executor = boom
    handler = ex._make_handler("sys_x")
    result = await handler(types.SimpleNamespace(arguments={}, tool_call_id="c"))
    assert result.result_type == "failure" and "kaboom" in result.error


# ---------------------------------------------------------------------------
# run_turn (fake SDK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_streams_text_reasoning_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("ASSISTANT_REASONING_DELTA", deltaContent="thinking…"),
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="PO"),
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="NG"),
                _ev("ASSISTANT_USAGE", model="claude-haiku-4.5", inputTokens=10, outputTokens=2),
                _ev("ASSISTANT_MESSAGE", content="PONG"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("hi")], [], "SYS")]
    texts = [e.text for e in events if isinstance(e, TextChunk)]
    reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert "".join(texts) == "PONG"
    assert reasoning and reasoning[0].delta == "thinking…"
    assert completes and completes[0].response == "PONG"
    assert completes[0].usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
    }
    # github_token threaded to the client; unsubscribed after the turn.
    assert state["client_kwargs"][0]["github_token"] == "gho_x"
    assert state["unsub_calls"] >= 1
    await ex.close()


@pytest.mark.asyncio
async def test_run_turn_tool_call_request_and_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev(
                    "TOOL_EXECUTION_START",
                    toolName="sys_session_send",
                    toolCallId="c1",
                    arguments={"to": "x"},
                ),
                _ev("TOOL_EXECUTION_COMPLETE", toolCallId="c1", success=True, result="done"),
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="ok"),
                _ev("ASSISTANT_MESSAGE", content="ok"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    comps = [e for e in events if isinstance(e, ToolCallComplete)]
    assert reqs and reqs[0].name == "sys_session_send" and reqs[0].args == {"to": "x"}
    assert comps and comps[0].name == "sys_session_send"
    assert comps[0].status == ToolCallStatus.SUCCESS
    assert comps[0].result == "done" and comps[0].error is None
    await ex.close()


@pytest.mark.asyncio
async def test_run_turn_tool_complete_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("TOOL_EXECUTION_START", toolName="sys_x", toolCallId="c1"),
                _ev(
                    "TOOL_EXECUTION_COMPLETE",
                    toolCallId="c1",
                    success=False,
                    error={"message": "tool blew up", "code": "ENOENT"},
                ),
                _ev("ASSISTANT_MESSAGE", content="done"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    comps = [e for e in events if isinstance(e, ToolCallComplete)]
    assert comps and comps[0].status == ToolCallStatus.ERROR
    # The SDK delivers ``error`` as a {"message", "code"} object; the executor
    # surfaces the message, not the dict's Python repr.
    assert comps[0].error == "tool blew up"
    await ex.close()


@pytest.mark.asyncio
async def test_run_turn_session_error_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_copilot(
        monkeypatch,
        [[_ev("SESSION_ERROR", message="model exploded")]],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors and "model exploded" in errors[0].message


@pytest.mark.asyncio
async def test_session_reused_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_copilot(
        monkeypatch,
        [
            [_ev("ASSISTANT_MESSAGE", content="one")],
            [_ev("ASSISTANT_MESSAGE", content="two")],
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    _ = [e async for e in ex.run_turn([_user("first")], [], "SYS")]
    _ = [e async for e in ex.run_turn([_user("second")], [], "SYS")]
    # One create_session for two same-config turns.
    assert len(state["create_kwargs"]) == 1
    assert state["sent"] == ["first", "second"]
    await ex.close()


@pytest.mark.asyncio
async def test_session_restart_on_system_prompt_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_copilot(
        monkeypatch,
        [
            [_ev("ASSISTANT_MESSAGE", content="one")],
            [_ev("ASSISTANT_MESSAGE", content="two")],
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    _ = [e async for e in ex.run_turn([_user("first")], [], "SYS-A")]
    _ = [e async for e in ex.run_turn([_user("second")], [], "SYS-B")]
    # System prompt changed → fresh session created, old client stopped.
    assert len(state["create_kwargs"]) == 2
    assert state["client_closed"] >= 1
    await ex.close()


@pytest.mark.asyncio
async def test_system_message_and_model_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_copilot(monkeypatch, [[_ev("ASSISTANT_MESSAGE", content="ok")]])
    ex = CopilotExecutor(github_token="gho_x", model="databricks-claude-opus-4-8")
    _ = [e async for e in ex.run_turn([_user("hi")], [], "SYS")]
    kwargs = state["create_kwargs"][0]
    # databricks-* model dropped to None (auto-select).
    assert kwargs["model"] is None
    # system prompt delivered as an append-mode system_message.
    assert kwargs["system_message"] == {"mode": "append", "content": "SYS"}
    assert kwargs["on_permission_request"] == _PermissionHandler.approve_all
    await ex.close()


@pytest.mark.asyncio
async def test_relative_cwd_resolved_to_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Copilot SDK rejects a relative working_directory; a spec / os_env can
    # hand us ``.``, so the executor must resolve it to an absolute path.
    state = _install_fake_copilot(monkeypatch, [[_ev("ASSISTANT_MESSAGE", content="ok")]])
    ex = CopilotExecutor(github_token="gho_x", cwd=".")
    _ = [e async for e in ex.run_turn([_user("hi")], [], "SYS")]
    client_wd = state["client_kwargs"][0]["working_directory"]
    session_wd = state["create_kwargs"][0]["working_directory"]
    import os as _os

    assert _os.path.isabs(client_wd) and _os.path.isabs(session_wd)
    await ex.close()


@pytest.mark.asyncio
async def test_ensure_session_failure_surfaces_executor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_copilot(monkeypatch, [], create_exc=RuntimeError("bad token"))
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors and "bad token" in errors[0].message


@pytest.mark.asyncio
async def test_start_failure_stops_client_no_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    # client.start() spawns the bundled CLI subprocess before connecting; a
    # start failure must still call client.stop() (via _safe_stop) so that
    # subprocess is reaped, not orphaned.
    state = _install_fake_copilot(monkeypatch, [], start_exc=RuntimeError("start blew up"))
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors and "start blew up" in errors[0].message
    # The started client was stopped (subprocess reaped), not leaked.
    assert state["started"] == 1
    assert state["client_closed"] >= 1


@pytest.mark.asyncio
async def test_error_after_partial_text_is_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    # A SESSION_ERROR / MODEL_CALL_FAILURE after partial text streamed, with no
    # successful final ASSISTANT_MESSAGE, must surface as an ExecutorError — not
    # a clean TurnComplete carrying the partial text (which would mask the
    # failure).
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="partial..."),
                _ev("MODEL_CALL_FAILURE", errorMessage="model call failed mid-stream"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert errors and "model call failed mid-stream" in errors[0].message
    assert not completes  # the turn is failed, not reported complete


# ---------------------------------------------------------------------------
# Policy gates (PHASE_LLM_REQUEST / PHASE_LLM_RESPONSE) — parity with the
# cursor / pi / claude-sdk executors.
# ---------------------------------------------------------------------------


def _verdict(action: str, reason: str = "") -> Any:
    return types.SimpleNamespace(action=action, reason=reason)


@pytest.mark.asyncio
async def test_policy_request_deny_blocks_before_send(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_copilot(
        monkeypatch, [[_ev("ASSISTANT_MESSAGE", content="must not send")]]
    )
    ex = CopilotExecutor(github_token="gho_x")

    async def deny_request(phase: str, ctx: dict[str, Any]) -> Any:
        if phase == "PHASE_LLM_REQUEST":
            return _verdict("POLICY_ACTION_DENY", "blocked req")
        return _verdict("POLICY_ACTION_ALLOW")

    ex._policy_evaluator = deny_request
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert errors and "call denied by policy: blocked req" in errors[0].message
    assert not completes
    assert state["sent"] == []  # denied before send_and_wait ran
    await ex.close()


@pytest.mark.asyncio
async def test_policy_response_deny_blocks_turn_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="leaked"),
                _ev("ASSISTANT_MESSAGE", content="leaked"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")

    async def deny_response(phase: str, ctx: dict[str, Any]) -> Any:
        if phase == "PHASE_LLM_RESPONSE":
            return _verdict("POLICY_ACTION_DENY", "bad output")
        return _verdict("POLICY_ACTION_ALLOW")

    ex._policy_evaluator = deny_response
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert errors and "response denied by policy: bad output" in errors[0].message
    assert not completes
    await ex.close()


@pytest.mark.asyncio
async def test_policy_allow_consults_both_phases_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="hi"),
                _ev("ASSISTANT_MESSAGE", content="hi"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    phases: list[str] = []

    async def allow(phase: str, ctx: dict[str, Any]) -> Any:
        phases.append(phase)
        return _verdict("POLICY_ACTION_ALLOW")

    ex._policy_evaluator = allow
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes and completes[0].response == "hi"
    assert phases == ["PHASE_LLM_REQUEST", "PHASE_LLM_RESPONSE"]
    await ex.close()


# ---------------------------------------------------------------------------
# Session restart triggers (tool set + model), beyond the system-prompt case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_restart_on_tools_change(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_copilot(
        monkeypatch,
        [[_ev("ASSISTANT_MESSAGE", content="one")], [_ev("ASSISTANT_MESSAGE", content="two")]],
    )
    ex = CopilotExecutor(github_token="gho_x")
    tools_a = [
        {"name": "alpha", "description": "", "parameters": {"type": "object", "properties": {}}}
    ]
    tools_b = [
        {"name": "beta", "description": "", "parameters": {"type": "object", "properties": {}}}
    ]
    _ = [e async for e in ex.run_turn([_user("first")], tools_a, "SYS")]
    _ = [e async for e in ex.run_turn([_user("second")], tools_b, "SYS")]
    # Tool set changed → fresh session, old client stopped (so removed tools
    # can't stay callable and new tools aren't missing).
    assert len(state["create_kwargs"]) == 2
    assert state["client_closed"] >= 1
    await ex.close()


@pytest.mark.asyncio
async def test_session_restart_on_model_change(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_copilot(
        monkeypatch,
        [[_ev("ASSISTANT_MESSAGE", content="one")], [_ev("ASSISTANT_MESSAGE", content="two")]],
    )
    ex = CopilotExecutor(github_token="gho_x")
    _ = [
        e
        async for e in ex.run_turn(
            [_user("first")], [], "SYS", ExecutorConfig(model="claude-haiku-4.5")
        )
    ]
    _ = [
        e
        async for e in ex.run_turn(
            [_user("second")], [], "SYS", ExecutorConfig(model="gpt-5-mini")
        )
    ]
    assert len(state["create_kwargs"]) == 2
    assert state["create_kwargs"][0]["model"] == "claude-haiku-4.5"
    assert state["create_kwargs"][1]["model"] == "gpt-5-mini"
    await ex.close()


# ---------------------------------------------------------------------------
# Mid-turn send_and_wait failure: retryable + session dropped, then recovers.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_and_wait_failure_is_retryable_and_recreates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_copilot(
        monkeypatch,
        [[_ev("ASSISTANT_MESSAGE", content="recovered")]],
        send_exc=RuntimeError("turn wedged"),
    )
    ex = CopilotExecutor(github_token="gho_x")
    first = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    errors = [e for e in first if isinstance(e, ExecutorError)]
    assert errors and "copilot-sdk turn failed: turn wedged" in errors[0].message
    assert errors[0].retryable is True
    assert state["client_closed"] >= 1  # the failed turn dropped the session
    # Next turn re-creates the session (the dropped one is gone) and succeeds.
    second = [e async for e in ex.run_turn([_user("again")], [], "SYS")]
    completes = [e for e in second if isinstance(e, TurnComplete)]
    assert completes and completes[0].response == "recovered"
    assert len(state["create_kwargs"]) == 2
    await ex.close()


# ---------------------------------------------------------------------------
# TOOL_EXECUTION_COMPLETE: SDK wire-shape unwrapping + status classification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_complete_result_unwrapped_from_sdk_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SDK wraps a tool result as {"content": ..., "detailedContent": ...};
    # the executor carries the bare content, not the wrapper dict.
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("TOOL_EXECUTION_START", toolName="sys_x", toolCallId="c1"),
                _ev(
                    "TOOL_EXECUTION_COMPLETE",
                    toolCallId="c1",
                    success=True,
                    result={"content": "the answer is 42", "detailedContent": "the answer is 42"},
                ),
                _ev("ASSISTANT_MESSAGE", content="ok"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    comps = [e for e in events if isinstance(e, ToolCallComplete)]
    assert comps and comps[0].status == ToolCallStatus.SUCCESS
    assert comps[0].result == "the answer is 42"
    await ex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,expected",
    [
        ({"blocked": True, "reason": "policy"}, ToolCallStatus.BLOCKED),
        ({"cancelled": True}, ToolCallStatus.CANCELLED),
    ],
)
async def test_tool_complete_blocked_and_cancelled_classification(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, Any], expected: ToolCallStatus
) -> None:
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("TOOL_EXECUTION_START", toolName="sys_x", toolCallId="c1"),
                _ev("TOOL_EXECUTION_COMPLETE", toolCallId="c1", success=True, result=result),
                _ev("ASSISTANT_MESSAGE", content="ok"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    comps = [e for e in events if isinstance(e, ToolCallComplete)]
    assert comps and comps[0].status == expected
    await ex.close()


# ---------------------------------------------------------------------------
# Lifecycle + edge branches: interrupt, empty prompt, no-executor, formatting.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_session_drops_and_recreates(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_copilot(
        monkeypatch,
        [[_ev("ASSISTANT_MESSAGE", content="one")], [_ev("ASSISTANT_MESSAGE", content="two")]],
    )
    ex = CopilotExecutor(github_token="gho_x")
    assert await ex.interrupt_session("unknown") is False  # no live session
    _ = [e async for e in ex.run_turn([_user("first")], [], "SYS")]
    assert await ex.interrupt_session("conv1") is True
    assert state["client_closed"] >= 1
    _ = [e async for e in ex.run_turn([_user("second")], [], "SYS")]
    assert len(state["create_kwargs"]) == 2  # session re-created after interrupt
    await ex.close()


@pytest.mark.asyncio
async def test_empty_prompt_completes_without_send(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_copilot(monkeypatch, [[_ev("ASSISTANT_MESSAGE", content="unused")]])
    ex = CopilotExecutor(github_token="gho_x")
    msgs: list[Message] = [{"role": "user", "content": "", "session_id": "conv1"}]
    events = [e async for e in ex.run_turn(msgs, [], "SYS")]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1 and completes[0].response is None
    assert state["sent"] == []  # nothing sent for an empty prompt
    await ex.close()


@pytest.mark.asyncio
async def test_bridged_tool_handler_no_executor_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_copilot(monkeypatch)
    ex = CopilotExecutor()  # _tool_executor stays None (single-process / pre-turn)
    handler = ex._make_handler("sys_x")
    result = await handler(types.SimpleNamespace(arguments={}, tool_call_id="c"))
    assert result.result_type == "failure"
    assert "no tool executor wired" in result.error


@pytest.mark.asyncio
async def test_paragraph_break_between_pre_and_post_tool_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-tool narration, a tool call, then post-tool narration must not render
    # run-on: a paragraph break is inserted between the two text segments.
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="before"),
                _ev("TOOL_EXECUTION_START", toolName="sys_x", toolCallId="c1"),
                _ev("TOOL_EXECUTION_COMPLETE", toolCallId="c1", success=True, result="done"),
                _ev("ASSISTANT_MESSAGE_DELTA", deltaContent="after"),
                _ev("ASSISTANT_MESSAGE", content="before\n\nafter"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("go")], [], "SYS")]
    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert text == "before\n\nafter"
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes and completes[0].response == "before\n\nafter"
    await ex.close()


@pytest.mark.asyncio
async def test_run_turn_usage_accumulates_cache_read_write_through_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two ASSISTANT_USAGE events (one per model call) accumulate end-to-end,
    # including cacheReadTokens / cacheWriteTokens, and total_tokens excludes
    # the cache counts.
    _install_fake_copilot(
        monkeypatch,
        [
            [
                _ev(
                    "ASSISTANT_USAGE",
                    model="claude-haiku-4.5",
                    inputTokens=10,
                    outputTokens=2,
                    cacheReadTokens=5,
                    cacheWriteTokens=8,
                ),
                _ev("ASSISTANT_USAGE", inputTokens=3, outputTokens=1, cacheReadTokens=4),
                _ev("ASSISTANT_MESSAGE", content="ok"),
            ]
        ],
    )
    ex = CopilotExecutor(github_token="gho_x")
    events = [e async for e in ex.run_turn([_user("hi")], [], "SYS")]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes and completes[0].usage == {
        "input_tokens": 13,
        "output_tokens": 3,
        "cache_read_input_tokens": 9,
        "cache_creation_input_tokens": 8,
        "total_tokens": 16,
    }
    await ex.close()
