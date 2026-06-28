"""Tests for OpenAIAgentsSDKExecutor with a fake Agents SDK module."""

import asyncio
import contextlib
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base64

import databricks.sdk.config as _sdk_config_mod

from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.inner.openai_agents_sdk_executor import (
    OpenAIAgentsSDKExecutor,
    _DatabricksToolStripResource,
    _normalize_content_blocks_for_chat,
    _normalize_responses_items_for_chat,
    _ReasoningBlockFilterStream,
    _sanitize_replay_item,
    _strip_strict_from_tools,
    _wrap_client_for_databricks_tools,
    _wrap_client_for_reasoning_models,
)
from omnigent.llms.errors import is_context_length_exceeded as _is_context_length_exceeded


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _collect(agen):
    """Drain an async generator into a list."""
    return [item async for item in agen]


@dataclass
class _FakeToolCallRawItem:
    name: str
    arguments: str
    call_id: str = "call_1"


@dataclass
class _FakeToolOutputRawItem:
    call_id: str = "call_1"


@dataclass
class _FakeToolCallItem:
    raw_item: object
    type: str = "tool_call_item"


@dataclass
class _FakeToolOutputItem:
    raw_item: object
    output: object
    type: str = "tool_call_output_item"


@dataclass
class _FakeRawTextDelta:
    delta: str
    type: str = "response.output_text.delta"


@dataclass
class _FakeRawEvent:
    data: object
    type: str = "raw_response_event"


@dataclass
class _FakeRunItemEvent:
    item: object
    type: str = "run_item_stream_event"


@dataclass
class _FakeModelSettings:
    parallel_tool_calls: bool | None = None
    max_tokens: int | None = None


@dataclass
class _FakePromptTokensDetails:
    """
    Minimal stand-in for OpenAI's ``prompt_tokens_details`` object.

    :param cached_tokens: Number of tokens served from the prompt cache.
    """

    cached_tokens: int = 0


@dataclass
class _FakeUsage:
    """
    Minimal stand-in for the openai-agents SDK ModelResponse.usage object.

    :param input_tokens: Number of input tokens for this LLM call.
    :param output_tokens: Number of output tokens for this LLM call.
    :param total_tokens: Sum of input and output tokens for this call.
        ``0`` means the SDK did not report a total; the executor falls
        back to ``input_tokens + output_tokens``.
    :param prompt_tokens_details: Optional breakdown of prompt tokens,
        including ``cached_tokens``. ``None`` means the SDK did not
        report cache details.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: _FakePromptTokensDetails | None = None


@dataclass
class _FakeRawResponse:
    """
    Minimal stand-in for the openai-agents SDK ModelResponse object.

    :param usage: Token usage for this LLM call.
    """

    usage: _FakeUsage


class _FakeResult:
    def __init__(
        self,
        events,
        final_output="",
        new_items=None,
        exception=None,
        state=None,
        raw_responses=None,
    ):
        self._events = events
        self.final_output = final_output
        self.new_items = new_items or []
        self._exception = exception
        self._state = state
        self.cancel_calls = []
        self.raw_responses = raw_responses or []

    async def stream_events(self):
        for event in self._events:
            yield event
        if self._exception is not None:
            raise self._exception

    def cancel(self, mode="immediate"):
        self.cancel_calls.append(mode)

    def to_state(self):
        return self._state


class _FakeRunner:
    last_calls = []
    next_result = None
    # When non-empty, each ``run_streamed`` call pops the next result from
    # the front (left). Used by the empty-turn retry tests to return a
    # different result per attempt. Falls back to ``next_result`` when
    # exhausted/empty so existing single-result tests are unaffected.
    next_results: list = []

    @classmethod
    def run_streamed(cls, agent, input, session, max_turns, run_config):
        cls.last_calls.append(
            {
                "agent": agent,
                "input": input,
                "session": session,
                "max_turns": max_turns,
                "run_config": run_config,
            }
        )
        if cls.next_results:
            return cls.next_results.pop(0)
        return cls.next_result


class _FakeFunctionTool:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@dataclass
class _FakeSessionSettings:
    """
    Minimal stand-in for ``agents.SessionSettings``.

    :param limit: Maximum number of items to retrieve from the session.
        ``None`` means no limit.
    """

    limit: int | None = None


class _FakeSQLiteSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.items = []
        self.clear_calls = 0
        self.pop_calls = 0

    async def get_items(self, limit=None):
        if limit is None:
            return list(self.items)
        return list(self.items[-limit:])

    async def add_items(self, items):
        self.items.extend(items)

    async def pop_item(self):
        if not self.items:
            return None
        self.pop_calls += 1
        return self.items.pop()

    async def clear_session(self):
        self.clear_calls += 1
        self.items.clear()


class _FakeOpenAIProvider:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeRunConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeItemHelpers:
    @staticmethod
    def text_message_outputs(items):
        return "".join(getattr(item, "text", "") for item in items)


class _FakeMaxTurnsExceeded(Exception):
    pass


def _fake_agents_sdk():
    return types.SimpleNamespace(
        Agent=_FakeAgent,
        Runner=_FakeRunner,
        RunConfig=_FakeRunConfig,
        OpenAIProvider=_FakeOpenAIProvider,
        SQLiteSession=_FakeSQLiteSession,
        FunctionTool=_FakeFunctionTool,
        ItemHelpers=_FakeItemHelpers,
        ModelSettings=_FakeModelSettings,
        MaxTurnsExceeded=_FakeMaxTurnsExceeded,
        SessionSettings=_FakeSessionSettings,
    )


def test_reasoning_filter_replaces_list_content_with_none() -> None:
    """
    List-type ``delta.content`` is replaced with ``None``.

    What breaks if this fails: reasoning-model streams crash the SDK's handler.
    """

    @dataclass
    class _Delta:
        content: object

    @dataclass
    class _Choice:
        delta: _Delta

    @dataclass
    class _Chunk:
        choices: list[_Choice]

    async def _run_inner() -> list[object]:
        async def _source():
            yield _Chunk([_Choice(_Delta(["block1", "block2"]))])
            yield _Chunk([_Choice(_Delta("normal text"))])

        return [chunk async for chunk in _ReasoningBlockFilterStream(_source())]

    chunks = _run(_run_inner())
    assert chunks[0].choices[0].delta.content is None
    assert chunks[1].choices[0].delta.content == "normal text"


def test_reasoning_filter_passes_string_content_through() -> None:
    """String ``delta.content`` is left unchanged by the filter."""

    @dataclass
    class _Delta:
        content: object

    @dataclass
    class _Choice:
        delta: _Delta

    @dataclass
    class _Chunk:
        choices: list[_Choice]

    async def _run_inner() -> list[object]:
        async def _source():
            yield _Chunk([_Choice(_Delta("hello"))])

        return [chunk async for chunk in _ReasoningBlockFilterStream(_source())]

    chunks = _run(_run_inner())
    assert chunks[0].choices[0].delta.content == "hello"


def test_reasoning_filter_context_manager_delegates() -> None:
    """
    ``__aenter__`` / ``__aexit__`` delegate to the underlying stream.
    A stream without these methods is also handled gracefully.
    """

    class _TrackingStream:
        entered = False
        exited = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def __aenter__(self):
            _TrackingStream.entered = True
            return self

        async def __aexit__(self, *args):
            _TrackingStream.exited = True

    async def _run_inner() -> None:
        stream = _TrackingStream()
        wrapper = _ReasoningBlockFilterStream(stream)
        async with wrapper:
            pass

    _run(_run_inner())
    assert _TrackingStream.entered
    assert _TrackingStream.exited


def test_wrap_client_non_streaming_create_not_wrapped() -> None:
    """
    Non-streaming ``create()`` calls are returned unwrapped (no async iterator).

    What breaks if this fails: regular chat completions are incorrectly wrapped
    and callers that expect a ``ChatCompletion`` get a ``_ReasoningBlockFilterStream``.
    """

    class _FakeResult:
        pass

    class _FakeCompletions:
        async def create(self, **kwargs) -> object:
            return _FakeResult()

        def __getattr__(self, name: str) -> object:
            raise AttributeError(name)

    class _FakeChat:
        completions = _FakeCompletions()

        def __getattr__(self, name: str) -> object:
            raise AttributeError(name)

    class _FakeClient:
        chat = _FakeChat()

        def __getattr__(self, name: str) -> object:
            raise AttributeError(name)

    async def _run_inner() -> object:
        client = _FakeClient()
        _wrap_client_for_reasoning_models(client)
        return await client.chat.completions.create(stream=False)

    result = _run(_run_inner())
    assert isinstance(result, _FakeResult)


def test_strip_strict_from_tools_all_envelope_shapes() -> None:
    """``strict`` is removed from flat, ``function``, and ``custom`` envelopes.

    What breaks if this fails: the Databricks AI Gateway 400s tool-bearing turns
    with ``tools.N.*.strict: Extra inputs are not permitted``.
    """
    tools = [
        {"type": "function", "name": "flat", "strict": True},  # Responses wire
        {"type": "function", "function": {"name": "nested", "strict": False}},  # Chat wire
        {"type": "custom", "custom": {"name": "c", "strict": True}},
    ]
    out = _strip_strict_from_tools(tools)
    assert out == [
        {"type": "function", "name": "flat"},
        {"type": "function", "function": {"name": "nested"}},
        {"type": "custom", "custom": {"name": "c"}},
    ]


def test_strip_strict_does_not_mutate_caller_or_parameters() -> None:
    """Caller tools are not aliased, and a ``strict`` property inside a JSON
    Schema ``parameters`` is preserved (only the envelope is touched)."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "f",
                "strict": True,
                "parameters": {"type": "object", "properties": {"strict": {"type": "boolean"}}},
            },
        }
    ]
    out = _strip_strict_from_tools(tools)
    # caller untouched
    assert tools[0]["function"]["strict"] is True
    # envelope strict gone, schema property named "strict" preserved
    assert "strict" not in out[0]["function"]
    assert out[0]["function"]["parameters"]["properties"]["strict"] == {"type": "boolean"}


def test_strip_strict_passthrough_non_list_and_non_dict() -> None:
    assert _strip_strict_from_tools(None) is None
    sentinel = object()
    assert _strip_strict_from_tools([sentinel]) == [sentinel]


def test_databricks_tool_strip_wraps_both_wires() -> None:
    """The Databricks client wrapper strips ``strict`` on chat AND responses."""
    chat_completions = _RecordingResource()
    responses_resource = _RecordingResource()

    # Build the fake client with instance attributes set in ``__init__`` (a real
    # closure scope) rather than class-body assignments — a class body does not
    # see enclosing-function locals when the name is also assigned there.
    class _Chat:
        def __init__(self) -> None:
            self.completions = chat_completions

    class _Client:
        def __init__(self) -> None:
            self.base_url = "https://x.databricks.com/ai-gateway/openai/v1"
            self.chat = _Chat()
            self.responses = responses_resource

    tools = [{"type": "function", "function": {"name": "f", "strict": False}}]

    async def _run_inner() -> None:
        client = _Client()
        _wrap_client_for_databricks_tools(client)
        await client.chat.completions.create(tools=list(tools), stream=False)
        await client.responses.create(tools=list(tools))

    _run(_run_inner())
    for rec in (chat_completions, responses_resource):
        assert rec.seen is not None
        assert rec.seen["tools"] == [{"type": "function", "function": {"name": "f"}}]


class _RecordingResource:
    def __init__(self) -> None:
        self.seen: dict | None = None

    async def create(self, **kwargs) -> object:
        self.seen = kwargs
        return kwargs.get("_result", "ok")

    def __getattr__(self, name: str) -> object:
        raise AttributeError(name)


def _fake_async_client(base_url: str) -> object:
    completions = _RecordingResource()
    responses = _RecordingResource()

    class _Chat:
        def __init__(self) -> None:
            self.completions = completions

        def __getattr__(self, name: str) -> object:
            raise AttributeError(name)

    class _Client:
        def __init__(self) -> None:
            self.base_url = base_url
            self.chat = _Chat()
            self.responses = responses

    return _Client()


def test_executor_installs_strip_proxy_only_for_gateway() -> None:
    """The executor wraps the client for `/ai-gateway/` and leaves it alone otherwise.

    What breaks if this fails: either tool-bearing gateway turns 400 on `strict`,
    or non-gateway OpenAI traffic loses Structured-Outputs `strict` enforcement.
    """
    gw = OpenAIAgentsSDKExecutor(
        client=_fake_async_client("https://x.databricks.com/ai-gateway/openai/v1"),
        use_responses=True,  # skip the reasoning wrapper to assert proxy identity directly
    )
    assert isinstance(gw._client.chat.completions, _DatabricksToolStripResource)
    assert isinstance(gw._client.responses, _DatabricksToolStripResource)
    assert gw._databricks is True

    plain = OpenAIAgentsSDKExecutor(
        client=_fake_async_client("https://api.openai.com/v1"),
        use_responses=True,
    )
    assert not isinstance(plain._client.chat.completions, _DatabricksToolStripResource)
    assert plain._databricks is False


def test_databricks_strip_composes_with_reasoning_wrapper_and_stream() -> None:
    """Strict-strip + reasoning-stream wrapping survive the nested proxy chain."""

    class _ListDeltaChunk:
        def __init__(self) -> None:
            self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(content=[{"x": 1}]))]

    async def _fake_stream():
        yield _ListDeltaChunk()

    completions = _RecordingResource()

    async def _create(**kwargs):
        completions.seen = kwargs
        return _fake_stream()

    completions.create = _create  # type: ignore[method-assign]

    class _Chat:
        def __init__(self) -> None:
            self.completions = completions

    class _Client:
        def __init__(self) -> None:
            self.base_url = "https://x.databricks.com/ai-gateway/openai/v1"
            self.chat = _Chat()
            self.responses = _RecordingResource()

    async def _run_inner():
        client = _Client()
        _wrap_client_for_databricks_tools(client)
        _wrap_client_for_reasoning_models(client)
        result = await client.chat.completions.create(
            tools=[{"type": "function", "function": {"name": "f", "strict": True}}],
            stream=True,
        )
        return result, completions.seen

    result, seen = _run(_run_inner())
    # strict stripped before the underlying create saw it
    assert seen["tools"] == [{"type": "function", "function": {"name": "f"}}]
    # reasoning wrapper still wrapped the stream
    assert isinstance(result, _ReasoningBlockFilterStream)


class TestOpenAIAgentsSDKExecutor(unittest.TestCase):
    def test_sanitize_replay_item_drops_long_ids(self):
        item = {
            "type": "message",
            "id": "x" * 200,
            "content": [{"type": "output_text", "text": "hello"}],
        }
        self.assertEqual(
            _sanitize_replay_item(item),
            {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
        )

    def test_build_tools_preserves_session_send_schema(self):
        executor = OpenAIAgentsSDKExecutor(client=object())
        tools = executor._build_tools(
            _fake_agents_sdk(),
            [
                {
                    "name": "sys_session_send",
                    "description": "Session send",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "session": {"type": "string"},
                            "args": {"type": "object", "additionalProperties": True},
                        },
                        "required": ["tool", "session", "args"],
                    },
                }
            ],
        )
        self.assertEqual(tools[0].name, "sys_session_send")
        self.assertEqual(
            tools[0].params_json_schema["required"],
            ["tool", "session", "args"],
        )

    def test_streams_text_and_tool_events(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(
                events=[
                    _FakeRawEvent(_FakeRawTextDelta("Hello ")),
                    _FakeRunItemEvent(
                        _FakeToolCallItem(_FakeToolCallRawItem("add", '{"a": 2, "b": 3}'))
                    ),
                    _FakeRunItemEvent(
                        _FakeToolOutputItem(_FakeToolOutputRawItem(), {"result": 5})
                    ),
                    _FakeRawEvent(_FakeRawTextDelta("world")),
                ],
                final_output="Hello world",
            )
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [{"name": "add", "description": "Add", "parameters": {"type": "object"}}],
                        "Be helpful.",
                    )
                ]

            self.assertIsInstance(events[0], TextChunk)
            self.assertEqual(events[0].text, "Hello ")
            self.assertIsInstance(events[1], ToolCallRequest)
            self.assertEqual(events[1].name, "add")
            self.assertEqual(events[1].args, {"a": 2, "b": 3})
            self.assertIsInstance(events[2], ToolCallComplete)
            self.assertEqual(events[2].status, ToolCallStatus.SUCCESS)
            self.assertEqual(events[2].result, {"result": 5})
            self.assertIsInstance(events[3], TextChunk)
            self.assertEqual(events[3].text, "world")
            self.assertIsInstance(events[4], TurnComplete)
            self.assertEqual(events[4].response, "Hello world")
            self.assertEqual(
                _FakeRunner.last_calls[0]["agent"].instructions,
                "Be helpful.",
            )
            # parallel_tool_calls defaults to None (omitted from request) so that
            # models like Kimi K2 that reject the field don't get a 400 error.
            self.assertIsNone(
                _FakeRunner.last_calls[0]["agent"].model_settings.parallel_tool_calls
            )

        _run(_t())

    def test_databricks_client_default_model_uses_databricks_model(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(events=[], final_output="done")
            client = types.SimpleNamespace(
                base_url="https://profile-host.example.com/ai-gateway/openai/v1"
            )
            executor = OpenAIAgentsSDKExecutor(client=client)
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]

            self.assertEqual(events[-1].response, "done")
            self.assertEqual(
                _FakeRunner.last_calls[0]["agent"].model,
                "databricks-gpt-5-5",
            )
            self.assertEqual(
                _FakeRunner.last_calls[0]["run_config"].kwargs["model"],
                "databricks-gpt-5-5",
            )

        _run(_t())

    def test_parallel_tool_calls_can_be_overridden(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(events=[], final_output="done")
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [{"name": "add", "description": "Add", "parameters": {"type": "object"}}],
                        "Be helpful.",
                        ExecutorConfig(
                            model="gpt-5.3-codex", extra={"parallel_tool_calls": False}
                        ),
                    )
                ]

            self.assertEqual(events[-1].response, "done")
            self.assertFalse(_FakeRunner.last_calls[0]["agent"].model_settings.parallel_tool_calls)

        _run(_t())

    def test_max_tokens_is_passed_to_model_settings(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(events=[], final_output="done")
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [],
                        "Be helpful.",
                        ExecutorConfig(extra={"max_tokens": 65536}),
                    )
                ]

            self.assertEqual(events[-1].response, "done")
            self.assertEqual(
                _FakeRunner.last_calls[0]["agent"].model_settings.max_tokens,
                65536,
            )

        _run(_t())

    def test_max_tokens_is_omitted_when_unset(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(events=[], final_output="done")
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [],
                        "Be helpful.",
                    )
                ]

            self.assertEqual(events[-1].response, "done")
            self.assertIsNone(_FakeRunner.last_calls[0]["agent"].model_settings.max_tokens)

        _run(_t())

    def test_tool_error_output_sets_error_field(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(
                events=[
                    _FakeRunItemEvent(_FakeToolCallItem(_FakeToolCallRawItem("fail", "{}"))),
                    _FakeRunItemEvent(
                        _FakeToolOutputItem(_FakeToolOutputRawItem(), {"error": "boom"})
                    ),
                ],
                final_output="tool failed",
            )
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [
                            {
                                "name": "fail",
                                "description": "Fail",
                                "parameters": {"type": "object"},
                            }
                        ],
                        "Be helpful.",
                    )
                ]

            tool_events = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].status, ToolCallStatus.ERROR)
            self.assertEqual(tool_events[0].result, {"error": "boom"})
            self.assertEqual(tool_events[0].error, "boom")

        _run(_t())

    def test_tool_blocked_output_sets_blocked_status(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(
                events=[
                    _FakeRunItemEvent(_FakeToolCallItem(_FakeToolCallRawItem("fail", "{}"))),
                    _FakeRunItemEvent(
                        _FakeToolOutputItem(
                            _FakeToolOutputRawItem(),
                            {"blocked": True, "reason": "Nope"},
                        )
                    ),
                ],
                final_output="tool blocked",
            )
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [
                            {
                                "name": "fail",
                                "description": "Fail",
                                "parameters": {"type": "object"},
                            }
                        ],
                        "Be helpful.",
                    )
                ]

            tool_events = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].status, ToolCallStatus.BLOCKED)
            self.assertEqual(tool_events[0].error, "Nope")

        _run(_t())

    def test_uses_full_history_first_then_latest_user(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(events=[], final_output="one")
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "first", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]
                self.assertEqual(events[-1].response, "one")
                self.assertIsInstance(_FakeRunner.last_calls[0]["input"], list)

                _FakeRunner.next_result = _FakeResult(events=[], final_output="two")
                events = [
                    e
                    async for e in executor.run_turn(
                        [
                            {"role": "user", "content": "first", "session_id": "s1"},
                            {"role": "assistant", "content": "one", "session_id": "s1"},
                            {"role": "user", "content": "second", "session_id": "s1"},
                        ],
                        [],
                        "",
                    )
                ]
                self.assertEqual(events[-1].response, "two")
                self.assertEqual(_FakeRunner.last_calls[1]["input"], "second")

        _run(_t())

    def test_started_session_preserves_structured_user_content(self):
        """Regression: fast path must not json.dumps a structured content list.

        input_file blocks with data: URIs are normalised to input_text before
        reaching the SDK runner (the openai-agents chatcmpl_converter and the
        Databricks endpoint don't support the ``file`` content block type).
        The key invariant is that the content arrives as a proper list, not a
        json.dumps'd string.
        """

        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(events=[], final_output="one")
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                _ = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "first", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]
                _FakeRunner.next_result = _FakeResult(events=[], final_output="two")
                structured = [
                    {"type": "input_text", "text": "read this"},
                    {
                        "type": "input_file",
                        "filename": "protocol.md",
                        "file_data": "data:text/markdown;base64,VGVzdA==",
                    },
                ]
                _ = [
                    e
                    async for e in executor.run_turn(
                        [
                            {"role": "user", "content": "first", "session_id": "s1"},
                            {"role": "assistant", "content": "one", "session_id": "s1"},
                            {"role": "user", "content": structured, "session_id": "s1"},
                        ],
                        [],
                        "",
                    )
                ]
            received = _FakeRunner.last_calls[1]["input"]
            self.assertIsInstance(received, list)
            self.assertEqual(received[0]["role"], "user")
            # input_file blocks are normalised to input_text before the SDK
            # runner sees them (data: URI decoded to plain text).
            # "VGVzdA==" decodes to "Test".
            expected_content = [
                {"type": "input_text", "text": "read this"},
                {"type": "input_text", "text": "Test"},
            ]
            self.assertEqual(received[0]["content"], expected_content)

        _run(_t())

    def test_started_session_replays_multiple_new_messages(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(events=[], final_output="one")
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                first_events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "first", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]
                self.assertEqual(first_events[-1].response, "one")

                _FakeRunner.next_result = _FakeResult(events=[], final_output="two")
                second_events = [
                    e
                    async for e in executor.run_turn(
                        [
                            {"role": "user", "content": "first", "session_id": "s1"},
                            {"role": "assistant", "content": "one", "session_id": "s1"},
                            {
                                "role": "user",
                                "content": (
                                    "[SYSTEM] The previous assistant turn was cancelled "
                                    "before completion. Continue from the next message."
                                ),
                                "session_id": "s1",
                                "metadata": {"framework": "cancellation_notice"},
                            },
                            {"role": "user", "content": "what happened", "session_id": "s1"},
                        ],
                        [],
                        "",
                    )
                ]

            self.assertEqual(second_events[-1].response, "two")
            self.assertIsInstance(_FakeRunner.last_calls[1]["input"], list)
            self.assertEqual(
                [item["content"] for item in _FakeRunner.last_calls[1]["input"]],
                [
                    (
                        "[SYSTEM] The previous assistant turn was cancelled "
                        "before completion. Continue from the next message."
                    ),
                    "what happened",
                ],
            )

        _run(_t())

    def test_transient_framework_notice_is_included_in_delta_and_not_persisted_in_cursor(self):
        """Regression for a bug where an inbox notice appended to the messages
        was advancing history_cursor past itself, and on the next turn the
        delta was empty because the cursor was at ``len(messages) + 1`` but the
        notice was never stored. The agent then ran with ``input=""`` and kept
        generating forever without seeing the notice."""

        async def _t():
            _FakeRunner.last_calls = []
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                _FakeRunner.next_result = _FakeResult(events=[], final_output="one")
                first_events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "first", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]
                self.assertEqual(first_events[-1].response, "one")

                state = executor._get_or_create_session_state(_fake_agents_sdk(), "s1")
                # After the first turn, the cursor should be past the user and
                # the assistant response (2 persisted items in context terms).
                self.assertEqual(state.history_cursor, 2)

                # Second turn: transient inbox notice appended to the tail,
                # no new persisted user message.
                notice = {
                    "role": "user",
                    "content": "[SYSTEM] There is 1 unread inbox item available.",
                    "session_id": "s1",
                    "metadata": {"framework": "inbox_notice"},
                }
                _FakeRunner.next_result = _FakeResult(events=[], final_output="two")
                second_events = [
                    e
                    async for e in executor.run_turn(
                        [
                            {"role": "user", "content": "first", "session_id": "s1"},
                            {"role": "assistant", "content": "one", "session_id": "s1"},
                            notice,
                        ],
                        [],
                        "",
                    )
                ]
                self.assertEqual(second_events[-1].response, "two")

            # The notice must actually be passed to the runner (not empty).
            # A single delta user message is passed as a raw string by the
            # executor; the critical thing is that the notice content is there.
            second_input = _FakeRunner.last_calls[1]["input"]
            if isinstance(second_input, list):
                contents = [item["content"] for item in second_input]
            else:
                contents = [second_input]
            self.assertEqual(contents, ["[SYSTEM] There is 1 unread inbox item available."])
            # It must NOT be the empty-delta fallback that the buggy version
            # produced before the fix.
            self.assertNotEqual(second_input, "")

            # And the cursor should only reflect persisted messages, not the
            # transient notice, so a future turn still sees any new real
            # messages without off-by-one skips.
            self.assertEqual(state.history_cursor, 3)

        _run(_t())

    def test_reuses_agent_across_turns_for_same_session_config(self):
        async def _t():
            _FakeRunner.last_calls = []
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                _FakeRunner.next_result = _FakeResult(events=[], final_output="one")
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "first", "session_id": "s1"}],
                        [{"name": "add", "description": "Add", "parameters": {"type": "object"}}],
                        "Be helpful.",
                    )
                ]
                self.assertEqual(events[-1].response, "one")

                _FakeRunner.next_result = _FakeResult(events=[], final_output="two")
                events = [
                    e
                    async for e in executor.run_turn(
                        [
                            {"role": "user", "content": "first", "session_id": "s1"},
                            {"role": "assistant", "content": "one", "session_id": "s1"},
                            {"role": "user", "content": "second", "session_id": "s1"},
                        ],
                        [{"name": "add", "description": "Add", "parameters": {"type": "object"}}],
                        "Be helpful.",
                    )
                ]
                self.assertEqual(events[-1].response, "two")

            self.assertIs(
                _FakeRunner.last_calls[0]["agent"],
                _FakeRunner.last_calls[1]["agent"],
            )

        _run(_t())

    def test_stepwise_internal_turns_resume_without_replaying_user_input(self):
        async def _t():
            _FakeRunner.last_calls = []
            resume_state = types.SimpleNamespace(
                _current_turn=7,
                _max_turns=99,
                _current_turn_persisted_item_count=3,
            )
            first_result = _FakeResult(
                events=[
                    _FakeRunItemEvent(
                        _FakeToolCallItem(_FakeToolCallRawItem("add", '{"a": 2, "b": 3}'))
                    ),
                    _FakeRunItemEvent(
                        _FakeToolOutputItem(_FakeToolOutputRawItem(), {"result": 5})
                    ),
                ],
                exception=_FakeMaxTurnsExceeded("Max turns (1) exceeded"),
                state=resume_state,
            )
            _FakeRunner.next_result = first_result
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                first_events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "count", "session_id": "s1"}],
                        [{"name": "add", "description": "Add", "parameters": {"type": "object"}}],
                        "Be helpful.",
                        ExecutorConfig(extra={"stepwise_internal_turns": True}),
                    )
                ]

                _FakeRunner.next_result = _FakeResult(events=[], final_output="done")
                second_events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "count", "session_id": "s1"}],
                        [{"name": "add", "description": "Add", "parameters": {"type": "object"}}],
                        "Be helpful.",
                        ExecutorConfig(extra={"stepwise_internal_turns": True}),
                    )
                ]

            self.assertTrue(first_events[-1].continue_turn)
            self.assertEqual(first_result.cancel_calls, [])
            self.assertEqual(_FakeRunner.last_calls[0]["max_turns"], 1)
            self.assertIs(_FakeRunner.last_calls[1]["input"], resume_state)
            self.assertEqual(resume_state._current_turn, 0)
            self.assertEqual(resume_state._max_turns, 1)
            self.assertEqual(resume_state._current_turn_persisted_item_count, 0)
            self.assertEqual(second_events[-1].response, "done")

        _run(_t())

    def test_stepwise_internal_turns_use_new_user_message_instead_of_resume_state(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(
                events=[
                    _FakeRunItemEvent(
                        _FakeToolCallItem(_FakeToolCallRawItem("add", '{"a": 2, "b": 3}'))
                    ),
                    _FakeRunItemEvent(
                        _FakeToolOutputItem(_FakeToolOutputRawItem(), {"result": 5})
                    ),
                ],
                exception=_FakeMaxTurnsExceeded("Max turns (1) exceeded"),
                state=object(),
            )
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                first_events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "count", "session_id": "s1"}],
                        [],
                        "",
                        ExecutorConfig(extra={"stepwise_internal_turns": True}),
                    )
                ]

                _FakeRunner.next_result = _FakeResult(events=[], final_output="stopped")
                second_events = [
                    e
                    async for e in executor.run_turn(
                        [
                            {"role": "user", "content": "count", "session_id": "s1"},
                            {"role": "user", "content": "stop", "session_id": "s1"},
                        ],
                        [],
                        "",
                        ExecutorConfig(
                            extra={
                                "stepwise_internal_turns": True,
                                "new_user_messages_flushed": True,
                            }
                        ),
                    )
                ]

            self.assertTrue(first_events[-1].continue_turn)
            self.assertEqual(_FakeRunner.last_calls[1]["input"], "stop")
            self.assertEqual(second_events[-1].response, "stopped")

        _run(_t())

    def test_interrupt_session_cancels_stream_consumer_task(self):
        """
        ``interrupt_session`` must cancel the per-turn
        ``stream_consumer_task`` so the cancel lands inside
        the SDK's ``await self._event_queue.get()`` (the SDK's
        own ``except CancelledError`` then closes the httpx
        stream — the load-bearing fix).

        Previously the executor called
        ``result.cancel(mode="immediate")``, which only sets a
        flag and does not close the network — events kept
        streaming for 15+ seconds. The new contract: cancel
        the pump task so cancellation reaches the SDK's
        documented escape hatch.
        """

        async def _t():
            executor = OpenAIAgentsSDKExecutor(client=object())
            state = executor._get_or_create_session_state(_fake_agents_sdk(), "s1")
            state.active_result = _FakeResult(events=[])
            # Set to a non-zero value so the rollback-target
            # assertion below verifies a real assignment, not
            # just two uninitialized zeros being equal.
            state.run_item_count_before = 42

            # Real task we can observe — assert it gets
            # cancelled. A bare coroutine wrapped in
            # ``asyncio.create_task`` is the right shape; we
            # never await its result, just inspect cancellation.
            async def _fake_pump() -> None:
                # Park forever until cancelled; the cancel
                # is the whole assertion target.
                await asyncio.Event().wait()

            pump_task = asyncio.create_task(_fake_pump())
            state.stream_consumer_task = pump_task

            interrupted = await executor.interrupt_session("s1")
            # Yield once so the cancel can deliver to the
            # parked task before we inspect ``cancelled()``.
            await asyncio.sleep(0)

            self.assertTrue(interrupted)
            # Sets interrupt_requested so the consumer side
            # also drains the queue without emitting (defense
            # in depth — the cancellation should already have
            # propagated, but we want the consumer loop to
            # also short-circuit if we're racing it).
            self.assertTrue(state.interrupt_requested)
            # Load-bearing assertion — cancelling the pump
            # task is what makes the SDK actually halt its
            # network stream. ``.cancelled()`` (not just
            # ``.done()``) — the regression we're guarding
            # against is "interrupt_session stops calling
            # consumer.cancel()". A normal-completed task
            # (``.done()`` but not cancelled) would mask
            # that regression; the strict ``.cancelled()``
            # assertion catches it.
            self.assertTrue(
                pump_task.cancelled(),
                "interrupt_session must cancel state.stream_consumer_task; "
                "pump_task.cancelled() was False. A regression that drops "
                "the consumer.cancel() call leaves the task parked.",
            )
            # Rollback target captured so the next run_turn
            # rewinds the SDK session to its pre-cancel
            # state. The 42 was set above; verifies the
            # assignment is the load-bearing op (not a coincidental
            # zero match).
            self.assertEqual(state.rollback_to_item_count, 42)

            # Drain the cancelled task so asyncio doesn't
            # warn about un-awaited cancellation.
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task

        _run(_t())

    def test_rebuilds_agent_when_prompt_changes(self):
        async def _t():
            _FakeRunner.last_calls = []
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                _FakeRunner.next_result = _FakeResult(events=[], final_output="one")
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "first", "session_id": "s1"}],
                        [],
                        "Be helpful.",
                    )
                ]
                self.assertEqual(events[-1].response, "one")

                _FakeRunner.next_result = _FakeResult(events=[], final_output="two")
                events = [
                    e
                    async for e in executor.run_turn(
                        [
                            {"role": "user", "content": "first", "session_id": "s1"},
                            {"role": "assistant", "content": "one", "session_id": "s1"},
                            {"role": "user", "content": "second", "session_id": "s1"},
                        ],
                        [],
                        "Be terse.",
                    )
                ]
                self.assertEqual(events[-1].response, "two")

            self.assertIsNot(
                _FakeRunner.last_calls[0]["agent"],
                _FakeRunner.last_calls[1]["agent"],
            )
            self.assertEqual(_FakeRunner.last_calls[1]["agent"].instructions, "Be terse.")

        _run(_t())

    def test_sys_tool_names_are_passed_through_in_events(self):
        async def _t():
            _FakeRunner.last_calls = []
            _FakeRunner.next_result = _FakeResult(
                events=[
                    _FakeRunItemEvent(
                        _FakeToolCallItem(
                            _FakeToolCallRawItem(
                                "sys_runtime_execute",
                                '{"code": "print(1)"}',
                            )
                        )
                    ),
                ],
                final_output="done",
            )
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [
                            {
                                "name": "sys_runtime_execute",
                                "description": "Run code",
                                "parameters": {"type": "object"},
                            }
                        ],
                        "Be helpful.",
                    )
                ]

            self.assertEqual(
                _FakeRunner.last_calls[0]["agent"].tools[0].name, "sys_runtime_execute"
            )
            self.assertEqual(_FakeRunner.last_calls[0]["agent"].tools[0].description, "Run code")
            self.assertIsInstance(events[0], ToolCallRequest)
            self.assertEqual(events[0].name, "sys_runtime_execute")

        _run(_t())

    def test_turn_usage_single_call_billing_and_context_match(self):
        """
        For a single-LLM-call turn (no tool calls), total_tokens equals
        that call's total and context_tokens matches (both are input +
        output for one call).

        What breaks if this fails: single-turn billing totals are wrong,
        or the context ring shows a stale/missing value.
        """

        async def _t():
            _FakeRunner.last_calls = []
            result = _FakeResult(events=[], final_output="hello")
            result.raw_responses = [
                _FakeRawResponse(
                    usage=_FakeUsage(input_tokens=1000, output_tokens=200, total_tokens=1200)
                )
            ]
            _FakeRunner.next_result = result
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]

            turn_complete = next(e for e in events if isinstance(e, TurnComplete))
            # Billing: single call, so sum equals that one call.
            self.assertEqual(
                turn_complete.usage["input_tokens"],
                1000,
                "input_tokens must equal the single LLM call's input count.",
            )
            self.assertEqual(
                turn_complete.usage["output_tokens"],
                200,
                "output_tokens must equal the single LLM call's output count.",
            )
            self.assertEqual(
                turn_complete.usage["total_tokens"],
                1200,
                "total_tokens must be the billing sum (input + output = 1200).",
            )
            # context_tokens is always set so the REPL and compaction
            # don't need a total_tokens fallback path.
            self.assertEqual(
                turn_complete.usage["context_tokens"],
                1200,
                "context_tokens must equal total for single-call turns.",
            )

        _run(_t())

    def test_turn_usage_multi_call_billing_sum_and_context_tokens_separate(self):
        """
        For a multi-LLM-call turn (tool calls), total_tokens is the
        billing SUM across all sub-turns, and context_tokens is set to
        the LAST sub-turn's total for accurate context ring display.

        Root cause of the observed bug: if context_tokens is absent and
        total_tokens carries the sum, the context ring spikes to an
        inflated value during tool-call turns (because each sub-turn
        repeats the full conversation history as input), then drops back
        on the next plain turn — giving a spurious "compaction" impression.

        What breaks if this fails: either billing totals are wrong
        (total_tokens not summed) or the context ring shows the inflated
        sum instead of the accurate last-call fill.
        """

        async def _t():
            _FakeRunner.last_calls = []
            result = _FakeResult(events=[], final_output="done")
            # Two sub-turns: first call issues a tool call (small output),
            # second call produces the final answer (larger output).
            # Each call receives the full conversation history as input,
            # so input_tokens grows across calls.
            result.raw_responses = [
                _FakeRawResponse(
                    usage=_FakeUsage(input_tokens=5000, output_tokens=100, total_tokens=5100)
                ),
                _FakeRawResponse(
                    usage=_FakeUsage(input_tokens=5300, output_tokens=400, total_tokens=5700)
                ),
            ]
            _FakeRunner.next_result = result
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "search for it", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]

            turn_complete = next(e for e in events if isinstance(e, TurnComplete))
            # Billing: SUM across all sub-turns.
            self.assertEqual(
                turn_complete.usage["input_tokens"],
                10300,
                "input_tokens must sum across sub-turns for billing accuracy "
                "(5000 + 5300 = 10300).",
            )
            self.assertEqual(
                turn_complete.usage["output_tokens"],
                500,
                "output_tokens must sum across sub-turns for billing accuracy (100 + 400 = 500).",
            )
            self.assertEqual(
                turn_complete.usage["total_tokens"],
                10800,
                "total_tokens must be the billing sum (5100 + 5700 = 10800). "
                "If 5700, total_tokens was incorrectly set to last-call only.",
            )
            # Context fill: LAST call's total only.
            # If this is 10800 (the sum), the context ring spikes artificially
            # during tool-call turns and drops spuriously on plain turns.
            self.assertEqual(
                turn_complete.usage["context_tokens"],
                5700,
                "context_tokens must equal the LAST call's total (5700) for correct "
                "context ring display. If 10800, context_tokens is being set to the "
                "billing sum — that inflates the ring during tool-call turns.",
            )

        _run(_t())

    def test_turn_usage_multi_call_context_tokens_falls_back_when_total_missing(self):
        """
        When total_tokens is absent (zero) on the last raw response,
        context_tokens is computed as last_input + last_output.

        What breaks if this fails: context ring shows 0 when the model
        does not populate total_tokens on the last sub-turn.
        """

        async def _t():
            _FakeRunner.last_calls = []
            result = _FakeResult(events=[], final_output="done")
            result.raw_responses = [
                _FakeRawResponse(
                    usage=_FakeUsage(input_tokens=5000, output_tokens=100, total_tokens=5100)
                ),
                # Last call: total_tokens=0, executor must fall back to in + out.
                _FakeRawResponse(
                    usage=_FakeUsage(input_tokens=5300, output_tokens=400, total_tokens=0)
                ),
            ]
            _FakeRunner.next_result = result
            executor = OpenAIAgentsSDKExecutor(client=object())
            with patch(
                "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
                return_value=_fake_agents_sdk(),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "s1"}],
                        [],
                        "",
                    )
                ]

            turn_complete = next(e for e in events if isinstance(e, TurnComplete))
            # context_tokens falls back to last_in + last_out = 5300 + 400 = 5700.
            self.assertEqual(
                turn_complete.usage["context_tokens"],
                5700,
                "When last raw_response.total_tokens is 0, context_tokens must fall "
                "back to last_input + last_output (5700).",
            )
            # total_tokens is the billing sum: 5100 + 0 = 5100.
            self.assertEqual(
                turn_complete.usage["total_tokens"],
                5100,
                "total_tokens must be the billing sum across sub-turns (5100 + 0 = 5100).",
            )

        _run(_t())


# ── _get_openai_async_client: --profile priority ──────────────
#
# Function-based tests for the profile-precedence fix. When a
# user passes ``--profile X``, the resolved Databricks credentials
# must win over any ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` the
# shell happens to have set — otherwise an old export silently
# redirects the call to ``api.openai.com`` and the user's
# requested workspace is ignored.


def test_get_openai_client_profile_uses_callback_auth(monkeypatch):
    """Explicit ``profile`` uses httpx callback auth, not a static ``api_key``.

    Verifies that when a Databricks profile is provided, the client
    is constructed with an ``http_client`` carrying a
    ``_DatabricksBearerAuth`` that refreshes tokens per-request,
    and that the ``api_key`` is a placeholder (not the real token).

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import httpx

    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.setenv("OPENAI_API_KEY", "should-not-be-used")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    class _FakeConfig:
        host = "https://profile-host.example.com"
        profile = "dev"

        def authenticate(self):
            return {"Authorization": "Bearer fresh-tok"}

    monkeypatch.setattr(_sdk_config_mod, "Config", lambda **_kw: _FakeConfig())

    captured: dict[str, Any] = {}

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai as _openai_mod

    with patch.object(_openai_mod, "AsyncOpenAI", _StubAsyncOpenAI, create=True):
        _get_openai_async_client(profile="dev")

    assert captured["base_url"] == "https://profile-host.example.com/ai-gateway/openai/v1"
    assert captured["api_key"] != "should-not-be-used"
    assert isinstance(captured["http_client"], httpx.AsyncClient)


def test_get_openai_client_host_override_uses_ucode_auth_command(monkeypatch):
    """ucode host override uses ucode auth command without profile lookup.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import httpx

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.setenv("OPENAI_API_KEY", "should-not-be-used")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(
        db_exec,
        "_resolve_databricks_auth",
        lambda _profile: (_ for _ in ()).throw(AssertionError("profile lookup used")),
    )

    captured: dict[str, Any] = {}

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai as _openai_mod

    with patch.object(_openai_mod, "AsyncOpenAI", _StubAsyncOpenAI, create=True):
        _get_openai_async_client(
            profile="missing-profile",
            host_override="https://example.databricks.com/",
            base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
            databricks_auth_command="printf token",
        )

    assert captured["base_url"] == "https://example.databricks.com/ai-gateway/codex/v1"
    assert captured["api_key"] != "should-not-be-used"
    assert isinstance(captured["http_client"], httpx.AsyncClient)


def test_get_openai_client_host_override_requires_base_url(monkeypatch):
    """ucode host override fails loud when ucode omits the base URL.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(OSError, match="GATEWAY_BASE_URL"):
        _get_openai_async_client(
            profile="missing-profile",
            host_override="https://example.databricks.com/",
            databricks_auth_command="printf token",
        )


def test_get_openai_client_host_override_requires_auth_command(monkeypatch):
    """ucode host override fails loud when ucode omits the auth command.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(OSError, match="GATEWAY_AUTH_COMMAND"):
        _get_openai_async_client(
            profile="missing-profile",
            host_override="https://example.databricks.com/",
            base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
        )


def test_get_openai_client_api_key_falls_back_to_env_base_url(monkeypatch):
    """A spec-level api_key with NO override honors ambient ``OPENAI_BASE_URL``.

    Regression for the residual gateway 401 (continuation-turn daemon
    spawns): the api_key is frequently a gateway credential (e.g. a
    Databricks AI Gateway PAT detected from ``OPENAI_API_KEY``), and the
    companion base_url can be dropped on the daemon → runner → harness
    propagation chain (the spec-auth bake omits it when ``OPENAI_BASE_URL``
    is absent at materialization time; a reused local daemon may predate the
    env var). Without the ambient fallback, the gateway PAT is sent to
    ``api.openai.com`` and 401s. The runner inherits ``OPENAI_BASE_URL``, so
    honoring it here keeps the gateway target present on every turn.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example.com/ai-gateway/openai/v1")

    captured: dict[str, Any] = {}

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai as _openai_mod

    with patch.object(_openai_mod, "AsyncOpenAI", _StubAsyncOpenAI, create=True):
        _get_openai_async_client(api_key="dapi-gateway-token", base_url_override=None)

    assert captured["api_key"] == "dapi-gateway-token"
    assert captured["base_url"] == "https://gateway.example.com/ai-gateway/openai/v1", (
        "A spec-level api_key with no base_url override must fall back to the "
        "ambient OPENAI_BASE_URL the runner inherits; routing to api.openai.com "
        "(base_url=None) sends a gateway PAT to OpenAI and 401s."
    )


def test_get_openai_client_api_key_override_wins_over_env_base_url(monkeypatch):
    """An explicit base_url override wins over the ambient ``OPENAI_BASE_URL``.

    The baked ``executor.auth.base_url`` (threaded via
    ``HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL``) is the authoritative
    per-spec target and must not be shadowed by a differing env var.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong-env.example.com/v1")

    captured: dict[str, Any] = {}

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai as _openai_mod

    with patch.object(_openai_mod, "AsyncOpenAI", _StubAsyncOpenAI, create=True):
        _get_openai_async_client(
            api_key="dapi-gateway-token",
            base_url_override="https://spec-gateway.example.com/openai/v1",
        )

    assert captured["base_url"] == "https://spec-gateway.example.com/openai/v1"


def test_get_openai_client_api_key_no_env_defaults_to_openai(monkeypatch):
    """A genuine OpenAI key with no gateway anywhere still defaults to OpenAI.

    With no override and no ambient ``OPENAI_BASE_URL``, ``base_url`` stays
    ``None`` so the AsyncOpenAI client targets ``api.openai.com`` — the
    correct behavior for a real ``sk-...`` key.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    captured: dict[str, Any] = {}

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai as _openai_mod

    with patch.object(_openai_mod, "AsyncOpenAI", _StubAsyncOpenAI, create=True):
        _get_openai_async_client(api_key="sk-real-openai-key", base_url_override=None)

    assert captured["api_key"] == "sk-real-openai-key"
    assert captured["base_url"] is None


def test_get_openai_client_no_profile_honors_env_vars(monkeypatch):
    """Without an explicit profile, the env-var branch still works.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    monkeypatch.setenv("OPENAI_BASE_URL", "https://env-host.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    captured: dict[str, str] = {}

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import openai as _openai_mod

    with patch.object(_openai_mod, "AsyncOpenAI", _StubAsyncOpenAI, create=True):
        _get_openai_async_client(profile=None)

    assert captured["base_url"] == "https://env-host.example.com/v1"
    assert captured["api_key"] == "env-key"
    assert "http_client" not in captured


def test_get_openai_client_invalid_profile_raises_auth_error(monkeypatch):
    """An invalid profile raises ``DatabricksAuthError`` with login instructions.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import pytest

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.databricks_executor import DatabricksAuthError
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    def _failing_config(**_kw):
        raise ValueError("no credentials")

    monkeypatch.setattr(_sdk_config_mod, "Config", _failing_config)
    monkeypatch.setattr(db_exec, "_read_databrickscfg", lambda _p: None)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    with pytest.raises(DatabricksAuthError, match="databricks auth login -p dogfood"):
        _get_openai_async_client(profile="dogfood")


def test_get_openai_client_invalid_profile_with_env_fallback_warns(monkeypatch, caplog):
    """Profile auth failure with OPENAI_BASE_URL available warns and falls through.

    When ``_get_openai_async_client`` is called with a named profile that
    cannot be resolved (``DatabricksAuthError``), but ``OPENAI_BASE_URL``
    is set, the function must:

      1. Log a warning mentioning the profile name and the fallback.
      2. Return a client configured from OPENAI_BASE_URL, NOT re-raise.

    This preserves backward compatibility for CI environments (OIDC tokens
    injected via OPENAI_BASE_URL) that specify a profile in the agent spec
    but rely on env-var credentials at runtime.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Pytest log capture fixture.
    """
    import logging

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    def _failing_config(**_kw):
        raise ValueError("no credentials")

    monkeypatch.setattr(_sdk_config_mod, "Config", _failing_config)
    monkeypatch.setattr(db_exec, "_read_databrickscfg", lambda _p: None)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.databricks.com/serving-endpoints")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    with caplog.at_level(logging.WARNING):
        client = _get_openai_async_client(profile="dogfood")

    # Should not raise — fell through to OPENAI_BASE_URL.
    # A warning must be emitted so the fallback is not silent.
    assert any(
        "dogfood" in record.message and "OPENAI_BASE_URL" in record.message
        for record in caplog.records
    ), (
        f"Expected a warning mentioning 'dogfood' and 'OPENAI_BASE_URL'. "
        f"Got: {[r.message for r in caplog.records]}"
    )

    # Client must use OPENAI_BASE_URL, not the (failed) profile host.
    assert client.base_url.host == "example.databricks.com", (
        f"Expected client to use OPENAI_BASE_URL host 'example.databricks.com', "
        f"got {client.base_url.host!r}. The profile-failure fallthrough may be broken."
    )


def test_get_openai_client_missing_databricks_sdk_raises_actionable_error(monkeypatch):
    """Missing ``databricks-sdk`` with no env-var fallback gives an actionable error.

    When a Databricks-hosted model is requested, ``databricks-sdk`` is not
    installed, and no ``OPENAI_API_KEY``/``OPENAI_BASE_URL`` fallback is
    available, the function must raise ``ImportError`` with install
    instructions — not crash with an opaque traceback.

    Regression test for https://github.com/omnigent-ai/omnigent/issues/123.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import pytest

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    def _import_error(*_args, **_kw):
        raise ImportError("No module named 'databricks.sdk'")

    monkeypatch.setattr(db_exec, "_resolve_databricks_auth", _import_error)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    with pytest.raises(ImportError, match="pip install"):
        _get_openai_async_client(profile=None, model="databricks-gpt-5")


def test_get_openai_client_missing_databricks_sdk_with_env_falls_through(monkeypatch, caplog):
    """Missing ``databricks-sdk`` with OPENAI_API_KEY set falls through gracefully.

    When ``databricks-sdk`` is absent but env-var credentials are available,
    the function should log a warning and return a client configured from the
    env vars — not crash.

    Regression test for https://github.com/omnigent-ai/omnigent/issues/123.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Pytest log capture fixture.
    """
    import logging

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.openai_agents_sdk_executor import _get_openai_async_client

    def _import_error(*_args, **_kw):
        raise ImportError("No module named 'databricks.sdk'")

    monkeypatch.setattr(db_exec, "_resolve_databricks_auth", _import_error)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    with caplog.at_level(logging.WARNING):
        client = _get_openai_async_client(profile="dev", model="databricks-gpt-5")

    assert any("databricks-sdk" in record.message for record in caplog.records), (
        f"Expected a warning about missing databricks-sdk. "
        f"Got: {[r.message for r in caplog.records]}"
    )
    assert client is not None


def test_run_turn_auth_error_yields_actionable_message(monkeypatch):
    """``run_turn`` yields the actionable ``DatabricksAuthError`` message,
    not the raw ``__cause__`` string.

    When the agents SDK raises a ``DatabricksAuthError`` (e.g. from
    ``_DatabricksBearerAuth.auth_flow`` when the OAuth refresh token has
    expired), the ``ExecutorError.message`` must be the high-level
    "Run: databricks auth login -p X" guidance — not the underlying SDK
    exception text like "token expired".

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.databricks_executor import DatabricksAuthError

    # DatabricksAuthError carries the actionable message; its __cause__ is
    # the raw SDK exception that is NOT suitable to show the user.
    raw_sdk_exc = ValueError("token expired")
    auth_error = DatabricksAuthError(
        "Databricks authentication failed for profile 'dev'. Run: databricks auth login -p dev"
    )
    auth_error.__cause__ = raw_sdk_exc

    _FakeRunner.last_calls = []
    _FakeRunner.next_result = _FakeResult(events=[], exception=auth_error)

    executor = OpenAIAgentsSDKExecutor(client=object())
    with patch(
        "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
        return_value=_fake_agents_sdk(),
    ):
        events = _run(
            _collect(
                executor.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    system_prompt="",
                    config=ExecutorConfig(),
                )
            )
        )

    error_events = [e for e in events if isinstance(e, ExecutorError)]
    assert len(error_events) == 1, (
        f"Expected exactly 1 ExecutorError, got {len(error_events)}. Events: {events!r}"
    )

    # The message must be the DatabricksAuthError text (actionable), not
    # the __cause__ text ("token expired"). If str(exc.__cause__) were used
    # instead of str(exc), this assertion would fail with "token expired".
    assert "databricks auth login -p dev" in error_events[0].message, (
        f"Expected actionable 'databricks auth login' guidance in message, "
        f"got: {error_events[0].message!r}. "
        f"If 'token expired' appears instead, auth_msg is reading exc.__cause__ "
        f"rather than str(exc)."
    )
    assert "token expired" not in error_events[0].message, (
        f"Raw SDK exception text leaked into the user-facing error message: "
        f"{error_events[0].message!r}. This means auth_msg is using "
        f"str(exc.__cause__) instead of str(exc)."
    )


# ---------------------------------------------------------------------------
# Tests for _normalize_content_blocks_for_chat and
# _normalize_responses_items_for_chat — input_file → input_text conversion
# ---------------------------------------------------------------------------


def test_normalize_content_blocks_input_file_data_uri_converted_to_input_text() -> None:
    """``input_file`` blocks with a ``data:`` URI are decoded to ``input_text``.

    The openai-agents SDK's chatcmpl_converter passes ``file_data`` verbatim
    to the ``file`` Chat Completions content type, which expects plain base64,
    not a data URI.  We must decode before handing off.
    """
    file_content = "# heading\nsome text"
    b64 = base64.b64encode(file_content.encode()).decode()
    blocks = [{"type": "input_file", "file_data": f"data:text/plain;base64,{b64}"}]

    result = _normalize_content_blocks_for_chat(blocks)

    assert result == [{"type": "input_text", "text": file_content}], (
        f"Expected decoded input_text block, got: {result!r}"
    )


def test_normalize_content_blocks_non_file_blocks_pass_through_unchanged() -> None:
    """Known blocks without metadata are returned as-is (identity preserved)."""
    blocks = [{"type": "input_text", "text": "hello"}]

    result = _normalize_content_blocks_for_chat(blocks)

    # No input_file or metadata present → original list object returned.
    assert result is blocks, (
        "Expected the original list object when no input_file blocks or metadata are present"
    )


def test_normalize_content_blocks_strips_filename_from_input_image() -> None:
    """``filename`` is Omnigent metadata and must not reach OpenAI."""
    blocks = [
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,abcd",
            "filename": "screenshot.png",
        }
    ]

    result = _normalize_content_blocks_for_chat(blocks)

    assert result == [{"type": "input_image", "image_url": "data:image/png;base64,abcd"}]
    assert "filename" not in result[0]
    assert result is not blocks


def test_normalize_content_blocks_preserves_input_image_detail_for_http_url() -> None:
    """Supported ``input_image.detail`` survives metadata sanitization for URLs."""
    blocks = [
        {
            "type": "input_image",
            "image_url": "https://example.com/screenshot.png",
            "detail": "high",
            "filename": "screenshot.png",
        }
    ]

    result = _normalize_content_blocks_for_chat(blocks)

    assert result == [
        {
            "type": "input_image",
            "image_url": "https://example.com/screenshot.png",
            "detail": "high",
        }
    ]


def test_normalize_content_blocks_data_uri_input_image_is_preserved() -> None:
    """Inline uploaded images are preserved as conventional base64 data URLs."""
    blocks = [
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,abcd",
            "filename": "screenshot.png",
        }
    ]

    result = _normalize_content_blocks_for_chat(blocks)

    assert result == [{"type": "input_image", "image_url": "data:image/png;base64,abcd"}]


def test_normalize_content_blocks_empty_file_data_dropped() -> None:
    """``input_file`` with empty ``file_data`` is silently dropped."""
    result = _normalize_content_blocks_for_chat([{"type": "input_file", "file_data": ""}])

    # Empty content has no value; dropping avoids sending a vacuous block.
    assert result == [], f"Expected empty list, got: {result!r}"


def test_normalize_content_blocks_malformed_base64_dropped() -> None:
    """``input_file`` with a malformed base64 payload is silently dropped."""
    result = _normalize_content_blocks_for_chat(
        [{"type": "input_file", "file_data": "data:text/plain;base64,!!!bad!!!"}]
    )

    # Decode failure → empty string → dropped, not raised.
    assert result == [], f"Expected empty list, got: {result!r}"


def test_normalize_content_blocks_binary_file_dropped() -> None:
    """``input_file`` with a binary (non-text) MIME type is silently dropped.

    Decoding a PDF or other binary payload as UTF-8 produces garbled replacement
    characters that waste tokens and provide no useful information to the model.
    """
    import base64

    b64 = base64.b64encode(b"%PDF-1.4 binary content").decode()
    result = _normalize_content_blocks_for_chat(
        [{"type": "input_file", "file_data": f"data:application/pdf;base64,{b64}"}]
    )

    # Binary MIME type → dropped, not inlined as garbage text.
    assert result == [], f"Expected binary block to be dropped, got: {result!r}"


def test_normalize_content_blocks_plain_file_data_used_as_text() -> None:
    """``input_file`` whose ``file_data`` is plain text (no data URI) is used as-is."""
    result = _normalize_content_blocks_for_chat(
        [{"type": "input_file", "file_data": "plain content here"}]
    )

    assert result == [{"type": "input_text", "text": "plain content here"}]


def test_normalize_content_blocks_mixed_blocks() -> None:
    """Mixed content lists: input_file converted, others unchanged."""
    file_content = "file body"
    b64 = base64.b64encode(file_content.encode()).decode()
    blocks = [
        {"type": "input_text", "text": "question"},
        {"type": "input_file", "file_data": f"data:text/plain;base64,{b64}"},
    ]

    result = _normalize_content_blocks_for_chat(blocks)

    assert result == [
        {"type": "input_text", "text": "question"},
        {"type": "input_text", "text": "file body"},
    ], f"Unexpected result: {result!r}"


def test_normalize_responses_items_message_item_content_normalized() -> None:
    """``message`` items have their ``content`` list normalized."""
    file_content = "uploaded file text"
    b64 = base64.b64encode(file_content.encode()).decode()
    items = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this?"},
                {"type": "input_file", "file_data": f"data:text/plain;base64,{b64}"},
            ],
        }
    ]

    result = _normalize_responses_items_for_chat(items)

    assert len(result) == 1
    assert result[0]["type"] == "message"
    assert result[0]["content"] == [
        {"type": "input_text", "text": "what is this?"},
        {"type": "input_text", "text": "uploaded file text"},
    ], f"Unexpected normalized content: {result[0]['content']!r}"


def test_normalize_responses_items_non_message_items_pass_through() -> None:
    """Non-``message`` items (function_call, function_call_output) are unchanged."""
    items = [
        {"type": "function_call", "name": "my_tool", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]

    result = _normalize_responses_items_for_chat(items)

    # Non-message items returned untouched — no normalization applied.
    assert result == items


def test_normalize_responses_items_message_without_input_file_unchanged() -> None:
    """``message`` items with no file/metadata blocks are returned as-is."""
    items = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "just text"}],
        }
    ]

    result = _normalize_responses_items_for_chat(items)

    # The content list object should be the same (identity) when unchanged.
    assert result[0]["content"] is items[0]["content"], (
        "Expected original content list object when no file/metadata blocks present"
    )


def test_normalize_responses_items_message_image_filename_stripped() -> None:
    """Image attachment filenames are stripped from message payloads."""
    items = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,abcd",
                    "filename": "screenshot.png",
                }
            ],
        }
    ]

    result = _normalize_responses_items_for_chat(items)

    assert result[0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_image", "image_url": "data:image/png;base64,abcd"}],
    }
    assert result[0] is not items[0]


def test_is_context_length_exceeded_direct_code() -> None:
    """
    ``_is_context_length_exceeded`` returns ``True`` for a direct
    ``BadRequestError``-like exception with ``code='context_length_exceeded'``.

    What breaks if this fails: reactive compression never fires; the error
    is treated as a permanent failure instead of triggering a retry.
    """

    class _FakeBadRequest(Exception):
        code = "context_length_exceeded"

    assert _is_context_length_exceeded(_FakeBadRequest())


def test_is_context_length_exceeded_false_for_other_errors() -> None:
    """
    ``_is_context_length_exceeded`` returns ``False`` for unrelated errors.

    What breaks if this fails: non-context errors are incorrectly treated
    as compression triggers, masking the real failure.
    """
    assert not _is_context_length_exceeded(ValueError("something else"))
    assert not _is_context_length_exceeded(RuntimeError("network error"))


def test_context_length_exceeded_re_raises() -> None:
    """
    ``context_length_exceeded`` from the SDK propagates as an exception
    rather than being swallowed into an ``ExecutorError``.

    The runtime compaction layer handles context overflow; the executor
    must re-raise so the exception reaches the ``ExecutorAdapter``'s
    error classifier and surfaces as ``code="context_length_exceeded"``
    on the harness wire.

    What breaks if this fails: context overflow is swallowed inside
    the executor and the runtime's reactive compaction never fires.
    """

    class _CtxExceeded(Exception):
        code = "context_length_exceeded"

    async def _t() -> None:
        executor = OpenAIAgentsSDKExecutor(client=object())
        session_id = "s_ctx_reraise"

        _FakeRunner.last_calls = []
        _FakeRunner.next_result = _FakeResult(events=[], final_output="", exception=_CtxExceeded())
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            with pytest.raises(_CtxExceeded):
                await _collect(
                    executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": session_id}],
                        [],
                        "Be helpful.",
                    )
                )

    _run(_t())


# ── LLM_REQUEST policy evaluation wiring ─────────────────────────────────────


def test_policy_evaluator_deny_yields_executor_error() -> None:
    """
    When ``_policy_evaluator`` returns ``POLICY_ACTION_DENY``, the
    executor yields ``ExecutorError`` and returns immediately —
    ``Runner.run_streamed`` is never called.

    What breaks if this fails: policy denials on the
    ``openai-agents`` harness silently pass through to the LLM
    instead of blocking the call.
    """

    async def _deny_evaluator(
        phase: str,
        data: dict[str, Any],
    ) -> Any:
        """Stub that always denies.

        :param phase: The policy phase, e.g. ``"PHASE_LLM_REQUEST"``.
        :param data: The request data dict.
        :returns: A DENY verdict.
        """
        return _FakeDenyVerdict(reason=f"denied by test: {data.get('model', '?')}")

    @dataclass
    class _FakeDenyVerdict:
        """Minimal verdict stub returning DENY.

        :param action: Always ``"POLICY_ACTION_DENY"``.
        :param reason: Human-readable reason.
        :param data: Not used.
        """

        action: str = "POLICY_ACTION_DENY"
        reason: str = "denied"
        data: dict[str, Any] | None = None

    async def _t() -> None:
        executor = OpenAIAgentsSDKExecutor(client=object())
        executor._policy_evaluator = _deny_evaluator  # type: ignore[attr-defined]

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "hi", "session_id": "test-session"},
        ]

        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = await _collect(executor.run_turn(messages, [], "Be helpful."))

        # Should yield exactly one ExecutorError, no text or tool events.
        assert len(events) == 1, f"Expected 1 event, got {len(events)}: {events}"
        assert isinstance(events[0], ExecutorError), (
            f"Expected ExecutorError, got {type(events[0]).__name__}"
        )
        assert "denied by test" in events[0].message

    _run(_t())


def test_policy_evaluator_allow_proceeds_to_run() -> None:
    """
    When ``_policy_evaluator`` returns ``POLICY_ACTION_ALLOW``, the
    executor proceeds normally — ``Runner.run_streamed`` is called
    and events stream through.

    What breaks if this fails: ALLOW verdicts are mistakenly
    treated as denials, blocking all LLM calls.
    """

    async def _allow_evaluator(
        phase: str,
        data: dict[str, Any],
    ) -> Any:
        """Stub that always allows.

        :param phase: The policy phase.
        :param data: The request data dict.
        :returns: An ALLOW verdict.
        """
        return _FakeAllowVerdict()

    @dataclass
    class _FakeAllowVerdict:
        """Minimal verdict stub returning ALLOW.

        :param action: Always ``"POLICY_ACTION_ALLOW"``.
        :param reason: ``None`` for ALLOW.
        :param data: ``None`` for ALLOW.
        """

        action: str = "POLICY_ACTION_ALLOW"
        reason: str | None = None
        data: dict[str, Any] | None = None

    async def _t() -> None:
        executor = OpenAIAgentsSDKExecutor(client=object())
        executor._policy_evaluator = _allow_evaluator  # type: ignore[attr-defined]

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "hi", "session_id": "test-session"},
        ]

        _FakeRunner.next_result = _FakeResult(
            events=[
                _FakeRawEvent(_FakeRawTextDelta("hello")),
            ],
            final_output="hello",
        )

        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = await _collect(executor.run_turn(messages, [], "Be helpful."))

        # Should contain text events and a TurnComplete — not an ExecutorError.
        error_events = [e for e in events if isinstance(e, ExecutorError)]
        assert not error_events, f"Unexpected ExecutorError: {error_events}"
        text_events = [e for e in events if isinstance(e, TextChunk)]
        assert len(text_events) >= 1, "Expected at least one TextChunk"
        assert text_events[0].text == "hello"

    _run(_t())


# ── Cache-token normalization ───────────────────────────────────
#
# OpenAI's ``input_tokens`` (aka ``prompt_tokens``) is the TOTAL
# input including cached tokens.  ``compute_llm_cost`` expects
# Anthropic semantics where ``input_tokens`` = non-cached only and
# ``cache_read_input_tokens`` is additive.  The executor must
# subtract cached tokens and expose them separately.


def test_turn_usage_subtracts_cached_tokens_from_input() -> None:
    """
    When ``prompt_tokens_details.cached_tokens`` is present, the
    executor must subtract cached tokens from ``input_tokens`` and
    report them as ``cache_read_input_tokens``.

    Without this, ``compute_llm_cost`` bills cached tokens at the
    full input rate instead of the cheaper cache-read rate.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        result = _FakeResult(events=[], final_output="hello")
        result.raw_responses = [
            _FakeRawResponse(
                usage=_FakeUsage(
                    input_tokens=10000,
                    output_tokens=500,
                    total_tokens=10500,
                    prompt_tokens_details=_FakePromptTokensDetails(cached_tokens=8000),
                )
            )
        ]
        _FakeRunner.next_result = result
        executor = OpenAIAgentsSDKExecutor(client=object())
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        turn_complete = next(e for e in events if isinstance(e, TurnComplete))
        usage = turn_complete.usage
        assert usage is not None
        # Non-cached portion: 10000 - 8000 = 2000.
        assert usage["input_tokens"] == 2000, (
            "input_tokens must be the non-cached portion (total input minus cached)."
        )
        assert usage["cache_read_input_tokens"] == 8000, (
            "cache_read_input_tokens must carry the cached token count."
        )
        assert usage["output_tokens"] == 500
        # total_tokens is the billing sum and stays unchanged.
        assert usage["total_tokens"] == 10500

    _run(_t())


def test_turn_usage_no_cached_tokens_omits_cache_key() -> None:
    """
    When no ``prompt_tokens_details`` is present, the usage dict
    must NOT contain ``cache_read_input_tokens`` — the executor
    degrades gracefully to the pre-cache behavior.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        result = _FakeResult(events=[], final_output="hello")
        result.raw_responses = [
            _FakeRawResponse(
                usage=_FakeUsage(
                    input_tokens=5000,
                    output_tokens=300,
                    total_tokens=5300,
                )
            )
        ]
        _FakeRunner.next_result = result
        executor = OpenAIAgentsSDKExecutor(client=object())
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        turn_complete = next(e for e in events if isinstance(e, TurnComplete))
        usage = turn_complete.usage
        assert usage is not None
        assert usage["input_tokens"] == 5000
        assert "cache_read_input_tokens" not in usage

    _run(_t())


def test_turn_usage_cached_tokens_multi_call_sums_across_responses() -> None:
    """
    Across multiple raw responses in a single turn, cached tokens
    must be summed and subtracted from the total ``input_tokens``.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        result = _FakeResult(events=[], final_output="done")
        result.raw_responses = [
            _FakeRawResponse(
                usage=_FakeUsage(
                    input_tokens=6000,
                    output_tokens=100,
                    total_tokens=6100,
                    prompt_tokens_details=_FakePromptTokensDetails(cached_tokens=4000),
                )
            ),
            _FakeRawResponse(
                usage=_FakeUsage(
                    input_tokens=7000,
                    output_tokens=200,
                    total_tokens=7200,
                    prompt_tokens_details=_FakePromptTokensDetails(cached_tokens=5000),
                )
            ),
        ]
        _FakeRunner.next_result = result
        executor = OpenAIAgentsSDKExecutor(client=object())
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "search", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        turn_complete = next(e for e in events if isinstance(e, TurnComplete))
        usage = turn_complete.usage
        assert usage is not None
        # Sum of input: 6000 + 7000 = 13000.
        # Sum of cached: 4000 + 5000 = 9000.
        # Non-cached: 13000 - 9000 = 4000.
        assert usage["input_tokens"] == 4000
        assert usage["cache_read_input_tokens"] == 9000

    _run(_t())


# ── Empty-turn retry / fail-loud ────────────────────────────────────
# The Databricks gateway occasionally returns a completed turn with no
# text, no tool calls, and no output items. ``run_turn`` retries such a
# turn once (``_EMPTY_TURN_MAX_ATTEMPTS``) and, if still empty AND the
# gateway billed zero output tokens, surfaces a loud retryable
# ``ExecutorError`` instead of a silent empty ``TurnComplete``.


@dataclass
class _FakeReasoningItem:
    """A run item the SDK emits for reasoning models. Bookkeeping, not
    user-visible output, so it must not count toward the emptiness check.

    :param type: The SDK discriminator, always ``"reasoning_item"``.
    """

    type: str = "reasoning_item"


@dataclass
class _FakeMessageOutputItem:
    """A run item carrying assistant text. Counts as output.

    :param text: The assistant text the item carries, e.g. ``"hello"``.
    :param type: The SDK discriminator, always ``"message_output_item"``.
    """

    text: str = ""
    type: str = "message_output_item"


def _empty_raw_response() -> _FakeRawResponse:
    """A raw model response reporting zero output tokens (gateway hiccup)."""
    return _FakeRawResponse(usage=_FakeUsage(input_tokens=10, output_tokens=0))


def _nonempty_raw_response() -> _FakeRawResponse:
    """A raw model response reporting one output token (model actually ran)."""
    return _FakeRawResponse(usage=_FakeUsage(input_tokens=10, output_tokens=1))


def _make_databricks_executor() -> OpenAIAgentsSDKExecutor:
    """An executor whose client points at the Databricks gateway base URL.

    Matches the production path the empty-output bug occurs on.
    """
    client = types.SimpleNamespace(
        base_url="https://profile-host.example.com/ai-gateway/openai/v1"
    )
    return OpenAIAgentsSDKExecutor(client=client)


def test_empty_turn_retries_then_succeeds() -> None:
    """
    An empty first attempt is retried; the second attempt's text is the
    only thing surfaced (no double-emit), and ``run_streamed`` runs twice.

    What breaks if this fails: a transient empty gateway response would
    surface as a silent empty turn instead of being retried.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = None
        _FakeRunner.next_results = [
            # Attempt 1: completely empty (no events, no items, 0 out tokens).
            _FakeResult(
                events=[],
                final_output="",
                new_items=[],
                raw_responses=[_empty_raw_response()],
            ),
            # Attempt 2: real text.
            _FakeResult(
                events=[_FakeRawEvent(_FakeRawTextDelta("recovered"))],
                final_output="recovered",
                raw_responses=[_nonempty_raw_response()],
            ),
        ]
        executor = _make_databricks_executor()
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = await _collect(
                executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "Be helpful.",
                )
            )

        # Two run_streamed calls = one retry. If 1, the empty turn was
        # not detected; if 3+, the loop over-retried.
        assert len(_FakeRunner.last_calls) == 2, (
            f"Expected 2 run_streamed calls (1 retry), got {len(_FakeRunner.last_calls)}"
        )
        # Exactly one TextChunk("recovered") + one TurnComplete. The empty
        # attempt streamed nothing, so there is no stray empty TextChunk
        # and no double-emit of the recovered text.
        text_chunks = [e for e in events if isinstance(e, TextChunk)]
        assert [c.text for c in text_chunks] == ["recovered"], (
            f"Expected only the recovered text, got {[c.text for c in text_chunks]!r}"
        )
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        # Exactly one TurnComplete for the whole turn. If 2, the empty
        # first attempt also emitted a (spurious) TurnComplete.
        assert len(turn_completes) == 1
        assert turn_completes[0].response == "recovered"
        assert not any(isinstance(e, ExecutorError) for e in events)

    _run(_t())


def test_empty_turn_retry_exhausted_yields_retryable_error() -> None:
    """
    When every attempt is empty and the gateway billed zero output
    tokens, the executor yields a loud ``ExecutorError(retryable=True)``
    and no ``TurnComplete``.

    What breaks if this fails: a persistent gateway hiccup would surface
    as a silent completed-but-empty turn that callers cannot distinguish
    from a real answer.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = None
        _FakeRunner.next_results = [
            _FakeResult(events=[], final_output="", raw_responses=[_empty_raw_response()]),
            _FakeResult(events=[], final_output="", raw_responses=[_empty_raw_response()]),
        ]
        executor = _make_databricks_executor()
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = await _collect(
                executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "Be helpful.",
                )
            )

        # Both attempts ran (initial + 1 retry) before giving up.
        assert len(_FakeRunner.last_calls) == 2, (
            f"Expected 2 run_streamed calls before fail-loud, got {len(_FakeRunner.last_calls)}"
        )
        errors = [e for e in events if isinstance(e, ExecutorError)]
        assert len(errors) == 1, f"Expected exactly 1 ExecutorError, got {events!r}"
        # retryable=True so the workflow raises RetryableLLMError and the
        # surrounding retry policy can reissue the turn. If False, the
        # workflow would treat it as a permanent harness bug and not retry.
        assert errors[0].retryable is True
        # Message names the empty-completion condition (matches the
        # production logger.error text) so the failure is diagnosable.
        assert "empty completion" in errors[0].message
        # No silent TurnComplete masking the failure.
        assert not any(isinstance(e, TurnComplete) for e in events)

    _run(_t())


def test_tool_call_without_text_is_not_retried() -> None:
    """
    A turn that called a tool but produced no final text is NOT empty —
    tool activity is legitimate output, so it runs once and completes.

    What breaks if this fails: tool-only turns (e.g. parallel sub-agent
    spawns that emit no assistant prose) would be wrongly retried.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = None
        _FakeRunner.next_results = [
            _FakeResult(
                events=[
                    _FakeRunItemEvent(
                        _FakeToolCallItem(_FakeToolCallRawItem("add", '{"a": 1, "b": 2}'))
                    ),
                    _FakeRunItemEvent(
                        _FakeToolOutputItem(_FakeToolOutputRawItem(), {"result": 3})
                    ),
                ],
                final_output="",
                raw_responses=[_nonempty_raw_response()],
            ),
        ]
        executor = _make_databricks_executor()
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = await _collect(
                executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [{"name": "add", "description": "Add", "parameters": {"type": "object"}}],
                    "Be helpful.",
                )
            )

        # Single attempt: tool activity means the turn is not empty.
        assert len(_FakeRunner.last_calls) == 1, (
            f"Expected 1 run_streamed call (no retry), got {len(_FakeRunner.last_calls)}"
        )
        assert any(isinstance(e, ToolCallRequest) for e in events)
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        # Tool-only turn completes normally (one TurnComplete), not errored.
        assert len(turn_completes) == 1
        assert not any(isinstance(e, ExecutorError) for e in events)

    _run(_t())


def test_reasoning_only_turn_is_treated_as_empty() -> None:
    """
    A turn whose only new item is a reasoning item (no text, no tools)
    is treated as empty and retried.

    What breaks if this fails: reasoning-model ghost turns would count
    their reasoning bookkeeping as output and never retry.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = None
        _FakeRunner.next_results = [
            _FakeResult(
                events=[],
                final_output="",
                new_items=[_FakeReasoningItem()],
                raw_responses=[_empty_raw_response()],
            ),
            _FakeResult(
                events=[_FakeRawEvent(_FakeRawTextDelta("answer"))],
                final_output="answer",
                raw_responses=[_nonempty_raw_response()],
            ),
        ]
        executor = _make_databricks_executor()
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = await _collect(
                executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "Be helpful.",
                )
            )

        # Retry fired: the reasoning-only item did not count as output.
        assert len(_FakeRunner.last_calls) == 2, (
            f"Expected 2 run_streamed calls (reasoning-only retried), "
            f"got {len(_FakeRunner.last_calls)}"
        )
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        # One TurnComplete, carrying the recovered text from attempt 2.
        assert len(turn_completes) == 1
        assert turn_completes[0].response == "answer"

    _run(_t())


def test_empty_turn_with_output_tokens_is_not_errored() -> None:
    """
    A final empty turn that DID bill output tokens is a deliberate empty
    answer, not a gateway hiccup: it completes silently as before, with
    no ``ExecutorError``.

    What breaks if this fails: a model that legitimately answers with an
    empty string would be turned into a spurious retryable error.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = None
        # Both attempts empty of text/items, but output_tokens > 0 means
        # the model ran and chose to emit nothing.
        _FakeRunner.next_results = [
            _FakeResult(events=[], final_output="", raw_responses=[_nonempty_raw_response()]),
            _FakeResult(events=[], final_output="", raw_responses=[_nonempty_raw_response()]),
        ]
        executor = _make_databricks_executor()
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = await _collect(
                executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "Be helpful.",
                )
            )

        # Retried once (both empty), then completed silently — the
        # output-token gate suppresses the fail-loud error.
        assert not any(isinstance(e, ExecutorError) for e in events), (
            f"Empty-but-billed turn should NOT error, got {events!r}"
        )
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        # Completes silently with one empty TurnComplete (today's behavior
        # for a deliberate empty answer), not an ExecutorError.
        assert len(turn_completes) == 1
        assert turn_completes[0].response == ""

    _run(_t())


def test_empty_turn_retry_rewinds_sdk_session() -> None:
    """
    Before retrying an empty turn, the SDK session is rewound to its
    pre-turn item count so the retry re-runs from an identical state.

    What breaks if this fails: a stray empty assistant item appended by
    the first attempt would accumulate, so the retry would re-run with a
    polluted session.
    """

    async def _t() -> None:
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = None

        # Attempt 1 appends a stray item to the session, mimicking the SDK
        # persisting an empty assistant turn before we detect emptiness.
        appended = {"stray": "empty-assistant-item"}

        class _AppendingEmptyResult(_FakeResult):
            def __init__(self, session):
                super().__init__(
                    events=[],
                    final_output="",
                    raw_responses=[_empty_raw_response()],
                )
                self._session = session

            async def stream_events(self):
                await self._session.add_items([appended])
                for event in self._events:
                    yield event

        captured: dict[str, object] = {}

        def _runner(agent, input, session, max_turns, run_config):
            _FakeRunner.last_calls.append({"session": session})
            captured["session"] = session
            if len(_FakeRunner.last_calls) == 1:
                return _AppendingEmptyResult(session)
            return _FakeResult(
                events=[_FakeRawEvent(_FakeRawTextDelta("ok"))],
                final_output="ok",
                raw_responses=[_nonempty_raw_response()],
            )

        fake_sdk = _fake_agents_sdk()
        fake_sdk.Runner = types.SimpleNamespace(run_streamed=_runner)
        executor = _make_databricks_executor()
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=fake_sdk,
        ):
            events = await _collect(
                executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "Be helpful.",
                )
            )

        # state.sdk_session may be wrapped by OpenAIResponsesCompactionSession
        # (which has .underlying_session) around _SanitizingSession (which has
        # ._underlying). Traverse both layers to reach the fake.
        _session = captured["session"]
        if hasattr(_session, "underlying_session"):
            _session = _session.underlying_session
        underlying = _session._underlying
        # The stray item appended in attempt 1 was popped before attempt 2,
        # so the session is back to its pre-turn (empty) state. pop_calls
        # records the rewind. If 0, the rewind never happened and the
        # retry would have re-run with a polluted session.
        assert underlying.pop_calls == 1, (
            f"Expected exactly 1 pop (rewind of the stray item), got {underlying.pop_calls}"
        )
        assert appended not in underlying.items
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        # One TurnComplete: the rewound retry produced the recovered text.
        assert len(turn_completes) == 1
        assert turn_completes[0].response == "ok"

    _run(_t())


# ---------------------------------------------------------------------------
# Tests: Compaction
# ---------------------------------------------------------------------------


@dataclass
class _FakeCompactionItem:
    """Stand-in for agents.items.CompactionItem."""

    type: str = "compaction_item"


def test_compaction_item_emits_compaction_complete() -> None:
    """When a compaction_item appears in result.new_items, a CompactionComplete
    event is yielded before TurnComplete."""
    from omnigent.inner.executor import CompactionComplete

    async def _t():
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = _FakeResult(
            events=[],
            final_output="compacted",
            new_items=[_FakeCompactionItem()],
            raw_responses=[
                _FakeRawResponse(_FakeUsage(input_tokens=100, output_tokens=50, total_tokens=150))
            ],
        )
        executor = OpenAIAgentsSDKExecutor(client=object())
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "Be helpful.",
                )
            ]

        compaction_events = [e for e in events if isinstance(e, CompactionComplete)]
        assert len(compaction_events) == 1
        assert compaction_events[0].token_count == 150  # context_tokens = last total
        # compacted_messages should contain the session items
        assert compaction_events[0].compacted_messages is not None
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_completes) == 1
        # CompactionComplete must come before TurnComplete
        ci = events.index(compaction_events[0])
        ti = events.index(turn_completes[0])
        assert ci < ti

    _run(_t())


def test_no_compaction_item_no_compaction_event() -> None:
    """When no compaction_item is in new_items, no CompactionComplete is yielded."""
    from omnigent.inner.executor import CompactionComplete

    async def _t():
        _FakeRunner.last_calls = []
        _FakeRunner.next_result = _FakeResult(
            events=[],
            final_output="normal",
        )
        executor = OpenAIAgentsSDKExecutor(client=object())
        with patch(
            "omnigent.inner.openai_agents_sdk_executor._ensure_agents_sdk",
            return_value=_fake_agents_sdk(),
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "Be helpful.",
                )
            ]

        compaction_events = [e for e in events if isinstance(e, CompactionComplete)]
        assert len(compaction_events) == 0

    _run(_t())
