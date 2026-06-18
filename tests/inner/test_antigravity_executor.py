"""
Unit tests for :class:`omnigent.inner.antigravity_executor.AntigravityExecutor`.

The fakes here mirror the real ``google.antigravity`` streaming surface the
executor depends on: ``agent.conversation`` yields :class:`Step` objects from
``receive_steps()`` (text / reasoning deltas, tool calls, status, usage) as the
turn runs, a registered ``PostToolCallHook`` fires per tool completion with a
``ToolResult``, and ``conversation.cancel()`` aborts a running turn. They let
the streaming / tool-pairing / cancellation logic be tested without the SDK
package or network.
"""

from __future__ import annotations

import asyncio
import collections
import enum
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.inner import antigravity_executor as ag
from omnigent.inner.antigravity_executor import AntigravityExecutor, _latest_user_text
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnCancelled,
    TurnComplete,
)
from omnigent.llms._usage_observer import add_observer

# ── Fakes mirroring the real SDK streaming shapes ───────────────────────


class _StepType(enum.Enum):
    """Subset of ``google.antigravity.types.StepType`` the executor reads."""

    TEXT_RESPONSE = "TEXT_RESPONSE"
    TOOL_CALL = "TOOL_CALL"
    FINISH = "FINISH"


class _StepStatus(enum.Enum):
    """Subset of ``google.antigravity.types.StepStatus`` the executor reads."""

    ACTIVE = "ACTIVE"
    DONE = "DONE"
    CANCELED = "CANCELED"
    ERROR = "ERROR"
    TERMINAL_ERROR = "TERMINAL_ERROR"


class _StepSource(enum.Enum):
    """Subset of ``google.antigravity.types.StepSource``."""

    SYSTEM = "SYSTEM"
    USER = "USER"
    MODEL = "MODEL"


class _StepTarget(enum.Enum):
    """Subset of ``google.antigravity.types.StepTarget``."""

    USER = "USER"
    ENVIRONMENT = "ENVIRONMENT"


class _AntigravityCancelledError(Exception):
    """Stand-in for ``google.antigravity.types.AntigravityCancelledError``."""


class _FakeToolCall:
    def __init__(self, name: str, args: dict[str, Any], call_id: str | None = None) -> None:
        self.name = name
        self.args = args
        self.id = call_id


class _FakeToolResult:
    def __init__(
        self,
        name: str,
        result: Any = None,
        error: str | None = None,
        call_id: str | None = None,
    ) -> None:
        self.name = name
        self.result = result
        self.error = error
        self.id = call_id
        self.exception = None


class _FakeUsage:
    def __init__(self) -> None:
        self.prompt_token_count = 11
        self.candidates_token_count = 7
        self.total_token_count = 18
        self.cached_content_token_count = 2


class _FakeStep:
    """Mirror of ``google.antigravity.types.Step`` (the fields the executor reads)."""

    def __init__(
        self,
        *,
        step_type: _StepType | None = None,
        status: _StepStatus | None = None,
        content_delta: str = "",
        thinking_delta: str = "",
        tool_calls: list[_FakeToolCall] | None = None,
        error: str = "",
        usage_metadata: _FakeUsage | None = None,
        source: _StepSource = _StepSource.MODEL,
        target: _StepTarget = _StepTarget.USER,
    ) -> None:
        self.type = step_type
        self.status = status
        self.content_delta = content_delta
        self.thinking_delta = thinking_delta
        self.tool_calls = tool_calls or []
        self.error = error
        self.usage_metadata = usage_metadata
        # Default MODEL->USER (assistant-facing); set source=USER to model the
        # SDK echoing the user's own input back in the step stream.
        self.source = source
        self.target = target


@dataclass
class _YieldStep:
    """Turn-script action: ``receive_steps`` yields this step."""

    step: _FakeStep


@dataclass
class _FireToolResult:
    """Turn-script action: the SDK invokes each PostToolCallHook with this result."""

    tool_result: _FakeToolResult


@dataclass
class _RaiseCancelled:
    """Turn-script action: ``receive_steps`` raises the SDK's cancellation error."""


@dataclass
class _RaiseGeneric:
    """Turn-script action: ``receive_steps`` raises a generic (non-cancel) error."""

    message: str = "boom"


# A turn script is the ordered list of actions one ``receive_steps()`` replays.
_TurnAction = _YieldStep | _FireToolResult | _RaiseCancelled | _RaiseGeneric


class _FakeConversation:
    """Mirror of ``google.antigravity.conversation.Conversation`` (read paths)."""

    def __init__(self, hooks: list[Any], scripts: collections.deque[list[_TurnAction]]) -> None:
        self._hooks = hooks
        self._scripts = scripts
        self.sends: list[str] = []
        self.cancel_called = 0

    async def send(self, prompt: Any, **_kwargs: Any) -> None:
        self.sends.append(prompt)

    async def receive_steps(self) -> Any:
        script = self._scripts.popleft() if self._scripts else []
        for action in script:
            if isinstance(action, _YieldStep):
                yield action.step
            elif isinstance(action, _FireToolResult):
                for hook in self._hooks:
                    await hook.run(SimpleNamespace(), action.tool_result)
            elif isinstance(action, _RaiseCancelled):
                raise _AntigravityCancelledError("cancelled")
            elif isinstance(action, _RaiseGeneric):
                raise RuntimeError(action.message)

    async def cancel(self) -> None:
        self.cancel_called += 1


class _FakeAgent:
    def __init__(self, config: Any, scripts: collections.deque[list[_TurnAction]]) -> None:
        self.config = config
        self._conversation = _FakeConversation(list(getattr(config, "hooks", []) or []), scripts)
        self.closed = False

    @property
    def conversation(self) -> _FakeConversation:
        return self._conversation

    async def __aenter__(self) -> _FakeAgent:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True


class _FakeLocalAgentConfig:
    """Mirror of ``LocalAgentConfig`` — accepts exactly the fields the executor sets."""

    def __init__(
        self,
        *,
        system_instructions: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        vertex: bool | None = None,
        project: str | None = None,
        location: str | None = None,
        tools: Any = None,
        hooks: Any = None,
    ) -> None:
        self.system_instructions = system_instructions
        self.model = model
        self.api_key = api_key
        self.vertex = vertex
        self.project = project
        self.location = location
        self.tools = tools
        self.hooks = hooks


class _FakePostToolCallHook:
    """Sub-classable stand-in for ``google.antigravity.hooks.PostToolCallHook``."""

    async def run(self, context: Any, data: Any) -> None:
        return None


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scripts: list[list[_TurnAction]],
) -> dict[str, Any]:
    """Patch ``_ensure_antigravity_sdk`` to return a fake module.

    :param monkeypatch: pytest monkeypatch fixture.
    :param scripts: One turn-script (list of actions) per ``receive_steps`` call,
        consumed front-to-back across turns / agent rebuilds.
    :returns: A ``captured`` dict exposing the agents / configs built, so tests
        can assert on what the executor passed to the SDK.
    """
    queue: collections.deque[list[_TurnAction]] = collections.deque(scripts)
    captured: dict[str, Any] = {"agents": [], "configs": []}

    class _FakeHooks:
        PostToolCallHook = _FakePostToolCallHook

    class _FakeTypes:
        AntigravityCancelledError = _AntigravityCancelledError

    class _FakeModule:
        LocalAgentConfig = _FakeLocalAgentConfig
        hooks = _FakeHooks
        types = _FakeTypes

        @staticmethod
        def Agent(config: Any) -> _FakeAgent:
            agent = _FakeAgent(config, queue)
            captured["agents"].append(agent)
            captured["configs"].append(config)
            return agent

    monkeypatch.setattr(ag, "_ensure_antigravity_sdk", lambda: _FakeModule())
    return captured


async def _drain(
    executor: AntigravityExecutor,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    config: ExecutorConfig | None = None,
    system_prompt: str = "sys",
) -> list[Any]:
    events: list[Any] = []
    async for event in executor.run_turn(
        messages, tools=tools or [], system_prompt=system_prompt, config=config
    ):
        events.append(event)
    return events


def _text_step(delta: str) -> _YieldStep:
    return _YieldStep(
        _FakeStep(
            step_type=_StepType.TEXT_RESPONSE, status=_StepStatus.ACTIVE, content_delta=delta
        )
    )


def _tool_call_step(call: _FakeToolCall, status: _StepStatus = _StepStatus.ACTIVE) -> _YieldStep:
    return _YieldStep(_FakeStep(step_type=_StepType.TOOL_CALL, status=status, tool_calls=[call]))


# ── Tests ───────────────────────────────────────────────────────────────


def test_latest_user_text_prefers_last_user_message() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    ]
    assert _latest_user_text(messages) == "second"


@pytest.mark.asyncio
async def test_streaming_maps_text_reasoning_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text/reasoning stream as separate deltas; usage + final text land on TurnComplete."""
    script: list[_TurnAction] = [
        _YieldStep(_FakeStep(status=_StepStatus.ACTIVE, thinking_delta="thinking...")),
        _text_step("Hello "),
        _text_step("world"),
        _YieldStep(
            _FakeStep(
                step_type=_StepType.FINISH, status=_StepStatus.DONE, usage_metadata=_FakeUsage()
            )
        ),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor(model="gemini-3-pro", api_key="k")

    events = await _drain(executor, [{"role": "user", "content": "hi", "session_id": "s1"}])

    # Two TextChunks prove deltas stream incrementally rather than as one blob —
    # if the executor reverted to a one-shot agent.chat() this would be 1 (or 0).
    texts = [e.text for e in events if isinstance(e, TextChunk)]
    assert texts == ["Hello ", "world"]
    reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
    assert len(reasoning) == 1 and reasoning[0].delta == "thinking..."
    assert reasoning[0].event_type == "reasoning_text"

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    # Final text is the accumulation of the streamed deltas.
    assert completes[0].response == "Hello world"
    # Usage maps the SDK's UsageMetadata field names onto Omnigent's keys.
    assert completes[0].usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cache_read_input_tokens": 2,
    }


@pytest.mark.asyncio
async def test_user_echoed_step_not_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A USER-source step (the SDK echoing the prompt) must not leak into the output.

    Regression guard for a real bug a live turn surfaced: the SDK streams the
    user's own input back as a ``source=USER`` step; mapping its content_delta
    to a TextChunk put the prompt into the assistant's response.
    """
    script: list[_TurnAction] = [
        _YieldStep(
            _FakeStep(
                step_type=_StepType.TEXT_RESPONSE,
                status=_StepStatus.ACTIVE,
                content_delta="echoed user prompt",
                source=_StepSource.USER,
                target=_StepTarget.USER,
            )
        ),
        _text_step("the real reply"),  # MODEL->USER by default
        _YieldStep(_FakeStep(step_type=_StepType.FINISH, status=_StepStatus.DONE)),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    texts = [e.text for e in events if isinstance(e, TextChunk)]
    # Only the MODEL->USER reply — the USER-source echo is filtered out.
    assert texts == ["the real reply"]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes[0].response == "the real reply"


@pytest.mark.asyncio
async def test_tool_request_and_completion_paired(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool call yields a request, then the PostToolCallHook yields a paired completion."""
    script: list[_TurnAction] = [
        _tool_call_step(_FakeToolCall("sys_shell", {"cmd": "ls"}, call_id="t1")),
        _FireToolResult(_FakeToolResult("sys_shell", result={"ok": True}, call_id="t1")),
        _text_step("done"),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    requests = [e for e in events if isinstance(e, ToolCallRequest)]
    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    assert len(requests) == 1 and len(completes) == 1
    assert requests[0].name == "sys_shell"
    assert requests[0].args == {"cmd": "ls"}
    assert requests[0].metadata == {"call_id": "t1"}
    # Completion is paired to the request by call_id, carries the real result,
    # and is classified SUCCESS (no error on the ToolResult).
    assert completes[0].metadata == {"call_id": "t1"}
    assert completes[0].name == "sys_shell"
    assert completes[0].result == {"ok": True}
    assert completes[0].status == ToolCallStatus.SUCCESS
    # duration_ms is computed from the recorded request start; >= 0 proves the
    # pending-tool table was populated by the request and read by the hook.
    assert completes[0].duration_ms >= 0.0
    # Request precedes completion in the stream.
    assert events.index(requests[0]) < events.index(completes[0])


@pytest.mark.asyncio
async def test_tool_completion_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ToolResult carrying an error maps to a ToolCallComplete with ERROR status."""
    script: list[_TurnAction] = [
        _tool_call_step(_FakeToolCall("sys_shell", {}, call_id="t1")),
        _FireToolResult(_FakeToolResult("sys_shell", error="permission denied", call_id="t1")),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    assert len(completes) == 1
    # ERROR (not SUCCESS) because the ToolResult.error was set; the message is
    # surfaced so the transcript shows why the tool failed.
    assert completes[0].status == ToolCallStatus.ERROR
    assert completes[0].error == "permission denied"


@pytest.mark.asyncio
async def test_tool_result_payload_error_classified_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ToolResult whose *payload* carries an error (not ToolResult.error) → ERROR."""
    script: list[_TurnAction] = [
        _tool_call_step(_FakeToolCall("sys_shell", {}, call_id="t1")),
        # error=None, but the result payload self-describes as an error — this
        # exercises classify_tool_result's payload branch, not the .error path.
        _FireToolResult(_FakeToolResult("sys_shell", result={"error": "boom"}, call_id="t1")),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    assert len(completes) == 1
    assert completes[0].status == ToolCallStatus.ERROR


@pytest.mark.asyncio
async def test_tool_call_without_id_still_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """An id-less tool call still emits one request and one (unpaired) completion."""
    script: list[_TurnAction] = [
        _tool_call_step(_FakeToolCall("sys_shell", {"cmd": "ls"}, call_id=None)),
        _FireToolResult(_FakeToolResult("sys_shell", result={"ok": True}, call_id=None)),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    requests = [e for e in events if isinstance(e, ToolCallRequest)]
    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    # Exactly one of each: the request gets a synthetic id (so it's still shown);
    # the id-less completion can't pair back, so its metadata is empty and it
    # falls back to the ToolResult's own name. The tool must still "close".
    assert len(requests) == 1
    assert len(completes) == 1
    assert completes[0].name == "sys_shell"
    assert completes[0].metadata == {}
    assert completes[0].duration_ms == 0.0


@pytest.mark.asyncio
async def test_tool_error_step_completes_without_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a TOOL_CALL step errors and the hook never fires, the step closes the tool."""
    call = _FakeToolCall("sys_shell", {"cmd": "ls"}, call_id="t1")
    script: list[_TurnAction] = [
        _tool_call_step(call, status=_StepStatus.ACTIVE),
        # No _FireToolResult: simulate the SDK surfacing the tool error outside
        # PostToolCallHook. The terminal TOOL_CALL ERROR step must still close it.
        _tool_call_step(call, status=_StepStatus.ERROR),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    # Without the step-stream fallback the tool would stay "open" (0 completions);
    # the fallback emits exactly one ERROR completion paired by call_id.
    assert len(completes) == 1
    assert completes[0].status == ToolCallStatus.ERROR
    assert completes[0].metadata == {"call_id": "t1"}
    # The turn itself is not failed — a tool error is not a turn-level error.
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ExecutorError) for e in events)


@pytest.mark.asyncio
async def test_tool_completion_not_double_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both the hook and a terminal step fire, the tool completes exactly once."""
    call = _FakeToolCall("sys_shell", {"cmd": "ls"}, call_id="t1")
    script: list[_TurnAction] = [
        _tool_call_step(call, status=_StepStatus.ACTIVE),
        _FireToolResult(_FakeToolResult("sys_shell", result={"ok": True}, call_id="t1")),
        # A trailing DONE step for the same call — the fallback must see it as
        # already-completed (popped by the hook) and NOT emit a second event.
        _tool_call_step(call, status=_StepStatus.DONE),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    completes = [e for e in events if isinstance(e, ToolCallComplete)]
    # 1, not 2: the hook completed it (with the real result) and popped the
    # pending entry, so the DONE-step fallback no-ops.
    assert len(completes) == 1
    assert completes[0].result == {"ok": True}


@pytest.mark.asyncio
async def test_tool_request_deduped_across_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same tool-call id appearing in multiple steps yields exactly one request."""
    call = _FakeToolCall("sys_shell", {"cmd": "ls"}, call_id="dup")
    script: list[_TurnAction] = [
        _tool_call_step(call, status=_StepStatus.ACTIVE),
        _tool_call_step(call, status=_StepStatus.DONE),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    requests = [e for e in events if isinstance(e, ToolCallRequest)]
    # 1, not 2: the SDK re-emits the same ToolCall across dispatch/execution
    # step transitions; the seen-id set must suppress the duplicate request.
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_terminal_error_step_yields_executor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TERMINAL_ERROR step surfaces an ExecutorError and suppresses TurnComplete."""
    script: list[_TurnAction] = [
        _text_step("partial"),
        _YieldStep(
            _FakeStep(
                step_type=_StepType.FINISH,
                status=_StepStatus.TERMINAL_ERROR,
                error="model exploded",
            )
        ),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert errors[0].message == "model exploded"
    # TERMINAL_ERROR is non-retryable (a plain ERROR would be retryable).
    assert errors[0].retryable is False
    # No TurnComplete after a turn-level error — the workflow treats it as failed.
    assert not any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_error_step_without_message_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ERROR step with no error text still yields an ExecutorError (not a silent success)."""
    script: list[_TurnAction] = [
        _YieldStep(_FakeStep(step_type=_StepType.FINISH, status=_StepStatus.ERROR, error="")),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    errors = [e for e in events if isinstance(e, ExecutorError)]
    # An empty error string must not be reported as a successful (empty) turn;
    # the executor substitutes a generic message and a plain ERROR is retryable.
    assert len(errors) == 1
    assert errors[0].message
    assert errors[0].retryable is True
    assert not any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_empty_turn_yields_turn_complete_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that streams no text ends as TurnComplete(response=None), not ''."""
    script: list[_TurnAction] = [
        _YieldStep(_FakeStep(step_type=_StepType.FINISH, status=_StepStatus.DONE))
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    # None (not "") is the load-bearing "produced nothing" signal documented on
    # TurnComplete; a regression to "" would change how the empty turn renders.
    assert completes[0].response is None
    assert not any(isinstance(e, TextChunk) for e in events)


@pytest.mark.asyncio
async def test_canceled_step_yields_turn_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CANCELED step surfaces TurnCancelled and no TurnComplete."""
    script: list[_TurnAction] = [
        _text_step("starting"),
        _YieldStep(_FakeStep(status=_StepStatus.CANCELED)),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    assert any(isinstance(e, TurnCancelled) for e in events)
    assert not any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_sdk_cancelled_error_yields_turn_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AntigravityCancelledError`` from the SDK maps to TurnCancelled, not ExecutorError."""
    script: list[_TurnAction] = [_text_step("starting"), _RaiseCancelled()]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    # The cancellation exception is caught specifically (via _cancelled_error_type
    # resolving the SDK's type) and reported as a clean cancel, not a failure.
    assert any(isinstance(e, TurnCancelled) for e in events)
    assert not any(isinstance(e, ExecutorError) for e in events)


@pytest.mark.asyncio
async def test_generic_turn_failure_yields_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-cancel exception from the SDK becomes a retryable ExecutorError."""
    script: list[_TurnAction] = [_text_step("partial"), _RaiseGeneric("kaboom")]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert "kaboom" in errors[0].message
    # retryable=True (unlike TERMINAL_ERROR) so the workflow picks RetryableLLMError;
    # also distinguishes a generic failure from a clean cancel (TurnCancelled).
    assert errors[0].retryable is True
    assert not any(isinstance(e, (TurnComplete, TurnCancelled)) for e in events)


@pytest.mark.asyncio
async def test_missing_sdk_yields_executor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Any:
        raise ImportError("no google-antigravity")

    monkeypatch.setattr(ag, "_ensure_antigravity_sdk", _raise)
    executor = AntigravityExecutor()

    events = await _drain(executor, [{"role": "user", "content": "q"}])

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "google-antigravity" in events[0].message


@pytest.mark.asyncio
async def test_sys_tools_exposed_as_callables_routing_through_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omnigent tools become callable SDK tools whose calls hit ``_tool_executor``.

    This is what lets an Antigravity agent drive Omnigent's sys / sub-agent
    tools under policy (needed to run Polly / Debby).
    """
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("done")]])
    executor = AntigravityExecutor()

    calls: list[dict[str, Any]] = []

    async def _fake_tool_executor(name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append({"name": name, "args": args})
        return {"ok": True}

    # The harness ExecutorAdapter assigns this in production; set it directly.
    executor._tool_executor = _fake_tool_executor

    tool_specs = [
        {
            "name": "sys_shell",
            "description": "Run a shell command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        }
    ]

    await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}], tool_specs)

    sdk_tools = captured["configs"][0].tools
    assert sdk_tools is not None and len(sdk_tools) == 1
    sdk_tool = sdk_tools[0]
    # LocalAgentConfig.tools is list[Callable]; the SDK reads __name__/__doc__.
    assert callable(sdk_tool)
    assert sdk_tool.__name__ == "sys_shell"
    assert sdk_tool.__doc__ == "Run a shell command"

    # Invoking the callable (kwargs form) routes back through the bridge.
    assert await sdk_tool(cmd="ls") == {"ok": True}
    # Single-dict argument form also works (SDK arg-shape tolerance).
    assert await sdk_tool({"cmd": "pwd"}) == {"ok": True}
    assert calls == [
        {"name": "sys_shell", "args": {"cmd": "ls"}},
        {"name": "sys_shell", "args": {"cmd": "pwd"}},
    ]


@pytest.mark.asyncio
async def test_no_tool_executor_means_no_sdk_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a tool-executor bridge, no SDK tools are built (agent uses native)."""
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("done")]])
    executor = AntigravityExecutor()  # _tool_executor stays None

    await _drain(
        executor,
        [{"role": "user", "content": "go"}],
        [{"name": "sys_shell", "description": "", "parameters": {}}],
    )

    assert captured["configs"][0].tools is None


@pytest.mark.asyncio
async def test_agent_reused_across_turns_same_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second turn on the same session reuses the cached agent + conversation."""
    captured = _install_fake_sdk(
        monkeypatch, scripts=[[_text_step("one-reply")], [_text_step("two-reply")]]
    )
    executor = AntigravityExecutor()

    await _drain(executor, [{"role": "user", "content": "one", "session_id": "s1"}])
    await _drain(executor, [{"role": "user", "content": "two", "session_id": "s1"}])

    # Exactly one agent built across two turns — the signature was unchanged so
    # the cached agent (and its SDK conversation state) was reused.
    assert len(captured["agents"]) == 1
    assert captured["agents"][0].conversation.sends == ["one", "two"]


@pytest.mark.asyncio
async def test_fresh_session_replays_prior_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FRESH agent seeds prior user/assistant turns into its first send().

    Models a rebuilt/restarted session: the turn arrives with prior history but
    the SDK conversation is brand new (and the SDK has no history-injection
    API). The prior turns must ride into the single send() as a context prefix,
    so the agent doesn't lose them. Without the seeding fix the agent would only
    ever see the latest user text.
    """
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("reply")]])
    executor = AntigravityExecutor()

    messages = [
        {"role": "user", "content": "what is 2+2?", "session_id": "s1"},
        {"role": "assistant", "content": "4", "session_id": "s1"},
        {"role": "user", "content": "and times 3?", "session_id": "s1"},
    ]
    await _drain(executor, messages)

    # One agent, one send. That send must carry the prior turns AND the latest
    # user text — not just the latest text (the pre-fix behavior).
    sends = captured["agents"][0].conversation.sends
    assert len(sends) == 1
    seeded = sends[0]
    assert "what is 2+2?" in seeded  # prior user turn replayed
    assert "assistant: 4" in seeded  # prior assistant turn replayed
    assert "and times 3?" in seeded  # latest user input still present
    # The latest input is not the whole prompt — proves a prefix was prepended.
    assert seeded != "and times 3?"


@pytest.mark.asyncio
async def test_rebuilt_session_replays_history_after_signature_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model switch rebuilds the agent; the rebuild re-seeds prior history.

    A signature change (model/system-prompt/tools) discards the live SDK
    conversation, so the *rebuilt* agent is fresh and must be re-seeded with the
    history it just lost — the same context-loss bug a server restart causes.
    """
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("a")], [_text_step("b")]])
    executor = AntigravityExecutor(model="gemini-3-pro")

    await _drain(executor, [{"role": "user", "content": "first", "session_id": "s1"}])
    # Second turn carries the accumulated history AND switches model, forcing a
    # rebuild of the (now fresh) agent.
    second = [
        {"role": "user", "content": "first", "session_id": "s1"},
        {"role": "assistant", "content": "answer one", "session_id": "s1"},
        {"role": "user", "content": "second", "session_id": "s1"},
    ]
    await _drain(executor, second, config=ExecutorConfig(model="gemini-3-flash"))

    # Two agents (model changed). The rebuilt agent's first send must replay the
    # prior turns, not just the latest "second".
    assert len(captured["agents"]) == 2
    rebuilt_sends = captured["agents"][1].conversation.sends
    assert len(rebuilt_sends) == 1
    assert "first" in rebuilt_sends[0]
    assert "assistant: answer one" in rebuilt_sends[0]
    assert "second" in rebuilt_sends[0]


@pytest.mark.asyncio
async def test_reused_session_does_not_reseed_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """A REUSED agent must NOT re-seed history (it already holds it).

    The live SDK conversation accumulated the prior turns itself, so re-seeding
    on reuse would duplicate them. The second turn's send must be exactly the
    latest user text, with no transcript prefix.
    """
    captured = _install_fake_sdk(
        monkeypatch, scripts=[[_text_step("one-reply")], [_text_step("two-reply")]]
    )
    executor = AntigravityExecutor()

    await _drain(executor, [{"role": "user", "content": "one", "session_id": "s1"}])
    # Second turn on the same session/signature reuses the agent. It carries the
    # prior turn in its messages, but the reused conversation already has it.
    second = [
        {"role": "user", "content": "one", "session_id": "s1"},
        {"role": "assistant", "content": "one-reply", "session_id": "s1"},
        {"role": "user", "content": "two", "session_id": "s1"},
    ]
    await _drain(executor, second)

    assert len(captured["agents"]) == 1  # reused, not rebuilt
    # The reused turn's send is the bare latest text — no "Conversation so far:"
    # prefix and no duplicated prior turn.
    sends = captured["agents"][0].conversation.sends
    assert sends == ["one", "two"]


@pytest.mark.asyncio
async def test_usage_observer_notified_on_turn_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """The usage observer is notified with the turn's tokens before TurnComplete.

    Peers notify in-process usage subscribers on every turn; without the fix,
    antigravity turns fire nothing, so observers see no usage for them.
    """
    script: list[_TurnAction] = [
        _text_step("hi"),
        _YieldStep(
            _FakeStep(
                step_type=_StepType.FINISH, status=_StepStatus.DONE, usage_metadata=_FakeUsage()
            )
        ),
    ]
    _install_fake_sdk(monkeypatch, scripts=[script])
    executor = AntigravityExecutor(model="gemini-3-pro")

    seen: list[dict[str, Any]] = []

    def _observer(
        *, model: str | None, input_tokens: int, output_tokens: int, total_tokens: int
    ) -> None:
        seen.append(
            {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }
        )

    remove = add_observer(_observer)
    try:
        events = await _drain(executor, [{"role": "user", "content": "go", "session_id": "s1"}])
    finally:
        remove()

    # Exactly one notification, carrying the model and the mapped token counts
    # from the turn's UsageMetadata (_FakeUsage: prompt=11, candidates=7, total=18).
    assert len(seen) == 1
    assert seen[0] == {
        "model": "gemini-3-pro",
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
    # The notification accompanies a normal TurnComplete.
    assert any(isinstance(e, TurnComplete) for e in events)


@pytest.mark.asyncio
async def test_model_switch_rebuilds_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-turn model override changes the signature and rebuilds the agent."""
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("a")], [_text_step("b")]])
    executor = AntigravityExecutor(model="gemini-3-pro")

    await _drain(executor, [{"role": "user", "content": "one", "session_id": "s1"}])
    await _drain(
        executor,
        [{"role": "user", "content": "two", "session_id": "s1"}],
        config=ExecutorConfig(model="gemini-3-flash"),
    )

    # Two agents: the model changed (gemini-3-pro -> gemini-3-flash), which is
    # part of the agent signature, so the executor rebuilt rather than reused.
    assert len(captured["agents"]) == 2
    assert captured["configs"][0].model == "gemini-3-pro"
    assert captured["configs"][1].model == "gemini-3-flash"


@pytest.mark.asyncio
async def test_default_model_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no model on the executor or per-turn config, the built-in default is pinned."""
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("ok")]])
    executor = AntigravityExecutor()  # no model anywhere

    await _drain(executor, [{"role": "user", "content": "hi", "session_id": "s1"}])

    # Pins _ANTIGRAVITY_DEFAULT_MODEL; changing the default must update this.
    assert captured["configs"][0].model == "gemini-3.5-flash"


@pytest.mark.asyncio
async def test_system_prompt_change_rebuilds_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed system_prompt is part of the agent signature, forcing a rebuild."""
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("a")], [_text_step("b")]])
    executor = AntigravityExecutor()

    await _drain(
        executor, [{"role": "user", "content": "one", "session_id": "s1"}], system_prompt="first"
    )
    await _drain(
        executor, [{"role": "user", "content": "two", "session_id": "s1"}], system_prompt="second"
    )

    # Two agents: system_prompt is in the (model, system_prompt, tools) signature,
    # so changing it rebuilds. A regression dropping system_prompt would be 1.
    assert len(captured["agents"]) == 2


@pytest.mark.asyncio
async def test_api_key_and_vertex_threaded_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """api_key and Vertex (project/location) reach LocalAgentConfig; base_url never does."""
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("ok")], [_text_step("ok")]])

    key_exec = AntigravityExecutor(api_key="gem-key")
    await _drain(key_exec, [{"role": "user", "content": "hi", "session_id": "s1"}])
    cfg = captured["configs"][0]
    assert cfg.api_key == "gem-key"
    # Vertex left unset on the API-key path.
    assert cfg.vertex is None

    vertex_exec = AntigravityExecutor(vertex=True, project="my-proj", location="us-central1")
    await _drain(vertex_exec, [{"role": "user", "content": "hi", "session_id": "s2"}])
    vcfg = captured["configs"][1]
    assert vcfg.vertex is True
    assert vcfg.project == "my-proj"
    assert vcfg.location == "us-central1"
    # The SDK config has no base_url field — the executor must never set one.
    assert not hasattr(vcfg, "base_url")


@pytest.mark.asyncio
async def test_close_session_closes_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """close_session() tears down the cached SDK agent for that session."""
    captured = _install_fake_sdk(monkeypatch, scripts=[[_text_step("ok")]])
    executor = AntigravityExecutor()

    await _drain(executor, [{"role": "user", "content": "hi", "session_id": "s1"}])
    agent = captured["agents"][0]
    assert agent.closed is False  # still open after the turn

    await executor.close_session("s1")
    # _close_agent awaited the agent's __aexit__, releasing the SDK connection.
    assert agent.closed is True


# ── Interrupt (cancellation) tests — real deterministic sync gates ──────


class _BlockingConversation:
    """Conversation that streams one delta, then blocks until cancel() releases it.

    :param raise_on_release: when True, ``receive_steps`` raises the SDK
        cancellation error after the gate opens (the "SDK reports a cancel"
        path); when False it simply ends the stream cleanly (the "cancel ended
        the turn quietly" path that exercises the ``interrupt_requested`` gate).
    """

    def __init__(self, gate: asyncio.Event, raise_on_release: bool) -> None:
        self._gate = gate
        self._raise_on_release = raise_on_release
        self.sends: list[str] = []
        self.cancel_called = 0

    async def send(self, prompt: Any, **_kw: Any) -> None:
        self.sends.append(prompt)

    async def receive_steps(self) -> Any:
        yield _FakeStep(
            step_type=_StepType.TEXT_RESPONSE, status=_StepStatus.ACTIVE, content_delta="streaming"
        )
        await self._gate.wait()  # blocked until cancel() releases us
        if self._raise_on_release:
            raise _AntigravityCancelledError("cancelled")

    async def cancel(self) -> None:
        self.cancel_called += 1
        self._gate.set()


def _install_blocking_sdk(
    monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event, *, raise_on_release: bool
) -> dict[str, Any]:
    """Install a fake SDK whose conversation blocks mid-turn until cancelled."""
    captured: dict[str, Any] = {}

    class _BlockingAgent:
        def __init__(self, config: Any) -> None:
            self.config = config
            self._conversation = _BlockingConversation(gate, raise_on_release)
            captured["conversation"] = self._conversation

        @property
        def conversation(self) -> _BlockingConversation:
            return self._conversation

        async def __aenter__(self) -> _BlockingAgent:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    class _FakeHooks:
        PostToolCallHook = _FakePostToolCallHook

    class _FakeTypes:
        AntigravityCancelledError = _AntigravityCancelledError

    class _FakeModule:
        LocalAgentConfig = _FakeLocalAgentConfig
        hooks = _FakeHooks
        types = _FakeTypes

        @staticmethod
        def Agent(config: Any) -> _BlockingAgent:
            return _BlockingAgent(config)

    monkeypatch.setattr(ag, "_ensure_antigravity_sdk", lambda: _FakeModule())
    return captured


async def _drive_until_first_text(
    executor: AntigravityExecutor, collected: list[Any], first_text: asyncio.Event
) -> None:
    async for event in executor.run_turn(
        [{"role": "user", "content": "go", "session_id": "s1"}], tools=[], system_prompt="sys"
    ):
        collected.append(event)
        if isinstance(event, TextChunk):
            first_text.set()


@pytest.mark.parametrize("raise_on_release", [True, False])
@pytest.mark.asyncio
async def test_interrupt_session_cancels_running_turn(
    monkeypatch: pytest.MonkeyPatch, raise_on_release: bool
) -> None:
    """interrupt_session cancels an in-flight turn -> TurnCancelled, no TurnComplete.

    Deterministic race: the conversation blocks inside ``receive_steps`` after
    streaming one delta; we interrupt only after observing that delta (so the
    turn is provably mid-flight). The two parametrized cases cover both ways the
    SDK can react to ``cancel()``: raising ``AntigravityCancelledError``
    (raise_on_release=True), or ending the stream cleanly so the
    ``interrupt_requested`` gate in run_turn must convert it to TurnCancelled
    (raise_on_release=False).
    """
    gate = asyncio.Event()
    first_text = asyncio.Event()
    captured = _install_blocking_sdk(monkeypatch, gate, raise_on_release=raise_on_release)
    executor = AntigravityExecutor()

    collected: list[Any] = []
    task = asyncio.create_task(_drive_until_first_text(executor, collected, first_text))
    # Wait until the turn has streamed its first delta (provably mid-flight)
    # before interrupting — this is the deterministic race window.
    await asyncio.wait_for(first_text.wait(), timeout=5)

    interrupted = await executor.interrupt_session("s1")
    # Assert the cancel landed BEFORE awaiting the task: a broken interrupt that
    # skips conversation.cancel() leaves the producer parked on the gate forever,
    # so checking here fails crisply ("cancel never called") instead of as an
    # opaque 5s task timeout below.
    assert interrupted is True  # a live conversation was found and asked to cancel
    assert captured["conversation"].cancel_called == 1  # cancel reached the SDK boundary

    await asyncio.wait_for(task, timeout=5)

    # Either path must surface a clean cancel and never a TurnComplete.
    assert any(isinstance(e, TurnCancelled) for e in collected)
    assert not any(isinstance(e, TurnComplete) for e in collected)


@pytest.mark.asyncio
async def test_interrupt_session_unknown_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """interrupt_session on a session with no open conversation returns False."""
    _install_fake_sdk(monkeypatch, scripts=[])
    executor = AntigravityExecutor()
    assert await executor.interrupt_session("never-started") is False
