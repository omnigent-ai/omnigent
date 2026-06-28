"""Tests for OpenResponsesExecutor with a fake OpenAI Responses client."""

import asyncio
import json
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    TextChunk,
    ToolCallRequest,
    TurnComplete,
)
from omnigent.inner.open_responses_sdk import (
    OpenResponsesExecutor,
    _convert_messages_to_responses,
    _convert_tools_to_responses,
    _databricks_openai_base_url,
    _is_databricks_gateway_base_url,
    _normalize_response_output_items,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@dataclass
class FakeTextPart:
    text: str
    type: str = "output_text"


@dataclass
class FakeMessageItem:
    content: list[FakeTextPart] = field(default_factory=list)
    type: str = "message"


@dataclass
class FakeFunctionCallItem:
    name: str
    arguments: str
    call_id: str = "call_1"
    id: str = "fc_1"
    type: str = "function_call"


@dataclass
class FakeIncomplete:
    reason: str


@dataclass
class FakeResponse:
    output: list[Any] = field(default_factory=list)
    output_text: str = ""
    error: Any = None
    incomplete_details: Any = None
    id: str = "resp_1"


class FakeResponsesAPI:
    def __init__(self, response: Any):
        self._response = response
        self.last_kwargs: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.responses = FakeResponsesAPI(response)


class TestConvertTools(unittest.TestCase):
    def test_basic_tool(self):
        tools = [
            {
                "name": "calc",
                "description": "Calculate",
                "parameters": {"type": "object", "properties": {"x": {"type": "number"}}},
            }
        ]
        result = _convert_tools_to_responses(tools)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["name"], "calc")
        self.assertFalse(result[0]["strict"])

    def test_strict_omitted_for_databricks_gateway(self):
        """include_strict=False omits the key entirely (gateway rejects it)."""
        tools = [{"name": "calc", "description": "Calculate", "parameters": {}}]
        result = _convert_tools_to_responses(tools, include_strict=False)
        self.assertNotIn("strict", result[0])
        # default still emits strict: False (non-gateway providers unchanged)
        self.assertIn("strict", _convert_tools_to_responses(tools)[0])

    def test_databricks_gateway_base_url_detection(self):
        class _C:
            base_url = "https://x.databricks.com/ai-gateway/openai/v1"

        class _Legacy:
            base_url = "https://x.databricks.com/serving-endpoints"

        self.assertTrue(_is_databricks_gateway_base_url(_C()))
        self.assertFalse(_is_databricks_gateway_base_url(_Legacy()))

    def test_executor_omit_tool_strict_flag_tracks_gateway(self):
        class _GatewayClient:
            base_url = "https://x.databricks.com/ai-gateway/openai/v1"

        class _PlainClient:
            base_url = "https://api.openai.com/v1"

        self.assertTrue(OpenResponsesExecutor(client=_GatewayClient())._omit_tool_strict)
        self.assertFalse(OpenResponsesExecutor(client=_PlainClient())._omit_tool_strict)

    def test_invalid_tool_name_is_normalized_for_provider(self):
        tools = [{"name": "sys_runtime_execute", "description": "Run code"}]
        result = _convert_tools_to_responses(tools)
        self.assertEqual(result[0]["name"], "sys_runtime_execute")
        self.assertEqual(result[0]["description"], "Run code")

    def test_preserves_required_args_in_async_tool_schema(self):
        tools = [
            {
                "name": "sys_call_async",
                "description": "Async call",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "args": {
                            "type": "object",
                            "default": {},
                            "properties": {
                                "example_key": {
                                    "anyOf": [{"type": "string"}],
                                },
                            },
                            "additionalProperties": {
                                "anyOf": [{"type": "string"}],
                            },
                        },
                    },
                    "required": ["tool", "args"],
                },
            }
        ]
        result = _convert_tools_to_responses(tools)
        self.assertEqual(result[0]["parameters"]["required"], ["tool", "args"])
        self.assertIn(
            "example_key",
            result[0]["parameters"]["properties"]["args"]["properties"],
        )

    def test_preserves_required_args_in_session_send_schema(self):
        tools = [
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
        ]
        result = _convert_tools_to_responses(tools)
        self.assertEqual(
            result[0]["parameters"]["required"],
            ["tool", "session", "args"],
        )


class TestConvertMessages(unittest.TestCase):
    def test_user_and_assistant(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = _convert_messages_to_responses(msgs)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")

    def test_tool_call_and_result_pair(self):
        msgs = [
            {"role": "tool_call", "content": {"tool": "calc", "args": {"x": 1}}},
            {"role": "tool_result", "content": {"result": 2}},
        ]
        result = _convert_messages_to_responses(msgs)
        self.assertEqual(result[0]["type"], "function_call")
        self.assertEqual(result[0]["name"], "calc")
        self.assertEqual(result[1]["type"], "function_call_output")

    def test_invalid_tool_name_is_normalized_in_history_replay(self):
        msgs = [
            {
                "role": "tool_call",
                "content": {"tool": "sys_runtime_execute", "args": {"code": "print(1)"}},
            },
            {"role": "tool_result", "content": {"stdout": "1\n"}},
        ]
        result = _convert_messages_to_responses(msgs)
        self.assertEqual(result[0]["name"], "sys_runtime_execute")
        self.assertEqual(result[1]["call_id"], result[0]["call_id"])

    def test_orphan_tool_result_becomes_user_message(self):
        msgs = [{"role": "tool_result", "content": "ok"}]
        result = _convert_messages_to_responses(msgs)
        self.assertEqual(result[0]["role"], "user")
        self.assertIn("tool result", result[0]["content"])

    def test_user_message_preserves_structured_content_list(self):
        # Regression: str()-ing the list flattens file attachments.
        content_parts = [
            {"type": "input_text", "text": "read this"},
            {
                "type": "input_file",
                "filename": "protocol.md",
                "file_data": "data:text/markdown;base64,VGVzdA==",
            },
        ]
        msgs = [{"role": "user", "content": content_parts}]
        result = _convert_messages_to_responses(msgs)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[0]["content"], content_parts)
        self.assertIsInstance(result[0]["content"], list)

    def test_assistant_message_preserves_structured_content_list(self):
        content_parts = [{"type": "output_text", "text": "answer"}]
        msgs = [{"role": "assistant", "content": content_parts}]
        result = _convert_messages_to_responses(msgs)
        self.assertEqual(result[0]["role"], "assistant")
        self.assertEqual(result[0]["content"], content_parts)
        self.assertIsInstance(result[0]["content"], list)

    def test_user_message_empty_content_substitutes_placeholder(self):
        for empty in (None, "", []):
            msgs = [{"role": "user", "content": empty}]
            result = _convert_messages_to_responses(msgs)
            self.assertEqual(result[0]["content"], "(empty)")


class TestNormalizeResponseOutput(unittest.TestCase):
    def test_strips_provider_only_fields(self):
        items = [
            {
                "type": "reasoning",
                "id": "x" * 200,
                "encrypted_content": "ciphertext",
                "summary": [],
                "status": "completed",
            },
            {
                "type": "function_call",
                "id": "y" * 200,
                "call_id": "call_1",
                "name": "calc",
                "arguments": '{"x": 2}',
                "status": "completed",
            },
            {
                "type": "message",
                "id": "z" * 200,
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}],
                "status": "completed",
            },
        ]

        result = _normalize_response_output_items(items)

        self.assertEqual(
            result[0],
            {
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "ciphertext",
            },
        )
        self.assertEqual(
            result[1],
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "calc",
                "arguments": '{"x": 2}',
            },
        )
        self.assertEqual(
            result[2],
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}],
            },
        )

    def test_reasoning_replay_always_includes_summary(self):
        # Regression: Responses API rejects reasoning input items
        # without a ``summary`` field ("Missing required parameter:
        # 'input[N].summary'"). Default to ``[]`` when the model
        # emitted no summary parts.
        items_missing_summary = [{"type": "reasoning", "encrypted_content": "ct"}]
        items_empty_summary = [
            {"type": "reasoning", "encrypted_content": "ct", "summary": []},
        ]
        items_with_summary = [
            {
                "type": "reasoning",
                "encrypted_content": "ct",
                "summary": [{"type": "summary_text", "text": "thinking"}],
            },
        ]
        for items in (items_missing_summary, items_empty_summary):
            result = _normalize_response_output_items(items)
            self.assertIn("summary", result[0])
            self.assertEqual(result[0]["summary"], [])
        result = _normalize_response_output_items(items_with_summary)
        self.assertEqual(
            result[0]["summary"],
            [{"type": "summary_text", "text": "thinking"}],
        )


class TestOpenResponsesExecutor(unittest.TestCase):
    def test_simple_text_response(self):
        async def _t():
            @dataclass
            class FakeTextDeltaEvent:
                delta: str
                item_id: str = "msg_1"
                output_index: int = 0
                type: str = "response.output_text.delta"

            @dataclass
            class FakeCompletedEvent:
                response: Any
                type: str = "response.completed"

            response = FakeResponse(output=[], output_text="Hello from Codex", id="resp_1")
            executor = OpenResponsesExecutor(client=FakeClient([]))
            executor._client = FakeClient(
                [
                    FakeTextDeltaEvent(delta="Hello "),
                    FakeTextDeltaEvent(delta="from Codex"),
                    FakeCompletedEvent(response=response),
                ]
            )
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "You are helpful.",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]
            self.assertIsInstance(events[0], TextChunk)
            self.assertEqual(events[0].text, "Hello ")
            self.assertIsInstance(events[1], TextChunk)
            self.assertEqual(events[1].text, "from Codex")
            self.assertIsInstance(events[2], TurnComplete)
            self.assertEqual(events[2].response, "Hello from Codex")
            state = executor._get_or_create_session_state("default")
            self.assertEqual(state.previous_response_id, "resp_1")
            self.assertEqual(
                executor._client.responses.last_kwargs["include"],
                ["reasoning.encrypted_content"],
            )

        _run(_t())

    def test_function_call_response(self):
        async def _t():
            @dataclass
            class FakeFunctionCallDoneEvent:
                name: str
                arguments: str
                item_id: str = "fc_1"
                type: str = "response.function_call_arguments.done"

            @dataclass
            class FakeCompletedEvent:
                response: Any
                type: str = "response.completed"

            response = FakeResponse(
                output=[
                    FakeFunctionCallItem(
                        name="calc", arguments=json.dumps({"x": 2}), call_id="call_1"
                    ),
                ],
                id="resp_1",
            )
            executor = OpenResponsesExecutor(
                client=FakeClient(
                    [
                        FakeFunctionCallDoneEvent(name="calc", arguments=json.dumps({"x": 2})),
                        FakeCompletedEvent(response=response),
                    ]
                )
            )
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "double 2"}],
                    [
                        {
                            "name": "calc",
                            "description": "Calculate",
                            "parameters": {
                                "type": "object",
                                "properties": {"x": {"type": "number"}},
                            },
                        }
                    ],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ToolCallRequest)
            self.assertEqual(events[0].name, "calc")
            self.assertEqual(events[0].args, {"x": 2})
            self.assertEqual(events[0].metadata["item_id"], "fc_1")
            state = executor._get_or_create_session_state("default")
            self.assertEqual(state.previous_response_id, "resp_1")
            self.assertEqual(state.history_cursor, 1)
            self.assertEqual(state.pending_function_calls[0]["call_id"], "call_1")
            self.assertTrue(executor._client.responses.last_kwargs["parallel_tool_calls"])

        _run(_t())

    def test_follow_up_tool_result_uses_previous_response_id(self):
        async def _t():
            @dataclass
            class FakeFunctionCallDoneEvent:
                name: str
                arguments: str
                item_id: str = "fc_1"
                type: str = "response.function_call_arguments.done"

            @dataclass
            class FakeOutputItemDoneEvent:
                item: Any
                output_index: int = 0
                type: str = "response.output_item.done"

            @dataclass
            class FakeCompletedEvent:
                response: Any
                type: str = "response.completed"

            first_stream = [
                FakeFunctionCallDoneEvent(name="calc", arguments=json.dumps({"x": 2})),
                FakeOutputItemDoneEvent(
                    item=FakeFunctionCallItem(
                        name="calc",
                        arguments=json.dumps({"x": 2}),
                        call_id="call_1",
                    ),
                ),
                FakeCompletedEvent(
                    response=FakeResponse(
                        output=[
                            FakeFunctionCallItem(
                                name="calc", arguments=json.dumps({"x": 2}), call_id="call_1"
                            )
                        ],
                        id="resp_tool",
                    )
                ),
            ]
            second_stream = [
                FakeCompletedEvent(
                    response=FakeResponse(output=[], output_text="4", id="resp_done")
                ),
            ]
            client = FakeClient(first_stream)
            executor = OpenResponsesExecutor(client=client)

            first_events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "double 2"}],
                    [
                        {
                            "name": "calc",
                            "description": "Calculate",
                            "parameters": {
                                "type": "object",
                                "properties": {"x": {"type": "number"}},
                            },
                        }
                    ],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]
            self.assertEqual(len(first_events), 1)
            self.assertEqual(first_events[0].metadata["item_id"], "fc_1")

            client.responses._response = second_stream
            second_events = [
                e
                async for e in executor.run_turn(
                    [
                        {"role": "user", "content": "double 2"},
                        {"role": "tool_call", "content": {"tool": "calc", "args": {"x": 2}}},
                        {"role": "tool_result", "content": {"result": 4}},
                    ],
                    [
                        {
                            "name": "calc",
                            "description": "Calculate",
                            "parameters": {
                                "type": "object",
                                "properties": {"x": {"type": "number"}},
                            },
                        }
                    ],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]

            self.assertEqual(len(second_events), 2)
            self.assertIsInstance(second_events[0], TextChunk)
            self.assertEqual(second_events[0].text, "4")
            self.assertIsInstance(second_events[1], TurnComplete)
            self.assertEqual(second_events[1].response, "4")
            self.assertEqual(client.responses.last_kwargs["previous_response_id"], "resp_tool")
            self.assertEqual(
                client.responses.last_kwargs["input"],
                [
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": '{"result": 4}',
                    },
                ],
            )

        _run(_t())

    def test_transient_framework_notice_is_included_in_delta_and_not_persisted_in_cursor(self):
        """Regression: a trailing framework notice appended to the messages
        must appear in the delta input AND must not advance history_cursor
        past itself, or the model will stop seeing the notice on the next
        turn and any self-wake loop will run forever."""

        async def _t():
            @dataclass
            class FakeTextDeltaEvent:
                delta: str
                item_id: str = "msg_1"
                output_index: int = 0
                type: str = "response.output_text.delta"

            @dataclass
            class FakeCompletedEvent:
                response: Any
                type: str = "response.completed"

            first_resp = FakeResponse(output=[], output_text="one", id="resp_1")
            client = FakeClient(
                [
                    FakeTextDeltaEvent(delta="one"),
                    FakeCompletedEvent(response=first_resp),
                ]
            )
            executor = OpenResponsesExecutor(client=client)

            [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "first", "session_id": "s1"}],
                    [],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]

            state = executor._get_or_create_session_state("s1")
            # First persisted user message consumed; cursor at len(persisted)+1.
            self.assertEqual(state.history_cursor, 2)
            self.assertEqual(state.previous_response_id, "resp_1")

            second_resp = FakeResponse(output=[], output_text="two", id="resp_2")
            client.responses._response = [
                FakeTextDeltaEvent(delta="two"),
                FakeCompletedEvent(response=second_resp),
            ]

            notice = {
                "role": "user",
                "content": "[SYSTEM] There is 1 unread inbox item available.",
                "session_id": "s1",
                "metadata": {"framework": "inbox_notice"},
            }
            events = [
                e
                async for e in executor.run_turn(
                    [
                        {"role": "user", "content": "first", "session_id": "s1"},
                        {"role": "assistant", "content": "one", "session_id": "s1"},
                        notice,
                    ],
                    [],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]

            self.assertEqual(events[-1].response, "two")

            # The notice text must be present in what was sent to the API,
            # not swallowed by an empty-delta bug.
            sent_input = client.responses.last_kwargs["input"]
            sent_text = json.dumps(sent_input)
            self.assertIn("unread inbox item", sent_text)

            # Cursor should reflect persisted messages only (2 so far), plus
            # the +1 bump the executor does after the final response. It must
            # not include the transient notice.
            self.assertEqual(state.history_cursor, 3)

        _run(_t())

    def test_falls_back_when_backend_rejects_previous_response_id(self):
        async def _t():
            @dataclass
            class FakeCompletedEvent:
                response: Any
                type: str = "response.completed"

            client = FakeClient(
                [
                    FakeCompletedEvent(
                        response=FakeResponse(output=[], output_text="hello", id="resp_1")
                    ),
                ]
            )
            executor = OpenResponsesExecutor(client=client)
            first_events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]
            self.assertIsInstance(first_events[-1], TurnComplete)

            def flaky_create(**kwargs):
                client.responses.last_kwargs = kwargs
                client.responses.calls.append(kwargs)
                if "previous_response_id" in kwargs:
                    raise RuntimeError(
                        "BAD_REQUEST: Databricks does not support the "
                        "`previous_response_id` parameter"
                    )
                return [
                    FakeCompletedEvent(
                        response=FakeResponse(output=[], output_text="TestBot42", id="resp_2")
                    )
                ]

            client.responses.create = flaky_create
            second_events = [
                e
                async for e in executor.run_turn(
                    [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hello"},
                        {"role": "user", "content": "What is my name?"},
                    ],
                    [],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]

            self.assertEqual(second_events[-1].response, "TestBot42")
            self.assertFalse(executor._supports_previous_response_id)
            self.assertEqual(len(client.responses.calls), 3)
            replay_input = client.responses.calls[-1]["input"]
            self.assertEqual(replay_input[0]["role"], "user")
            self.assertEqual(replay_input[-1]["content"], "What is my name?")

        _run(_t())

    def test_conversation_state_is_scoped_by_session_id(self):
        async def _t():
            @dataclass
            class FakeFunctionCallDoneEvent:
                name: str
                arguments: str
                item_id: str = "fc_1"
                type: str = "response.function_call_arguments.done"

            @dataclass
            class FakeCompletedEvent:
                response: Any
                type: str = "response.completed"

            parent_stream = [
                FakeFunctionCallDoneEvent(name="calc", arguments=json.dumps({"x": 2})),
                FakeCompletedEvent(
                    response=FakeResponse(
                        output=[
                            FakeFunctionCallItem(
                                name="calc", arguments=json.dumps({"x": 2}), call_id="call_parent"
                            )
                        ],
                        id="resp_parent",
                    )
                ),
            ]
            child_stream = [
                FakeCompletedEvent(
                    response=FakeResponse(output=[], output_text="child ok", id="resp_child")
                ),
            ]

            client = FakeClient(parent_stream)
            executor = OpenResponsesExecutor(client=client)
            tools = [
                {
                    "name": "calc",
                    "description": "Calculate",
                    "parameters": {"type": "object", "properties": {"x": {"type": "number"}}},
                }
            ]

            _ = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "double 2", "session_id": "parent"}],
                    tools,
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]

            client.responses._response = child_stream
            child_events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "say child", "session_id": "child"}],
                    [],
                    "",
                    ExecutorConfig(model="gpt-5.3-codex"),
                )
            ]

            self.assertEqual(child_events[-1].response, "child ok")
            self.assertNotIn("previous_response_id", client.responses.last_kwargs)
            self.assertEqual(
                client.responses.last_kwargs["input"],
                [{"type": "message", "role": "user", "content": "say child"}],
            )

            parent_state = executor._get_or_create_session_state("parent")
            child_state = executor._get_or_create_session_state("child")
            self.assertEqual(parent_state.previous_response_id, "resp_parent")
            self.assertEqual(child_state.previous_response_id, "resp_child")
            self.assertEqual(parent_state.pending_function_calls[0]["call_id"], "call_parent")
            self.assertEqual(len(child_state.pending_function_calls), 0)

        _run(_t())

    def test_incomplete_without_text_surfaces_error(self):
        async def _t():
            @dataclass
            class FakeCompletedEvent:
                response: Any
                type: str = "response.completed"

            response = FakeResponse(incomplete_details=FakeIncomplete(reason="max_output_tokens"))
            executor = OpenResponsesExecutor(
                client=FakeClient(
                    [
                        FakeCompletedEvent(response=response),
                    ]
                )
            )
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "",
                )
            ]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("max_output_tokens", events[0].message)

        _run(_t())

    def test_interrupt_session_closes_active_stream(self):
        class ClosableStream:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        async def _t():
            executor = OpenResponsesExecutor(client=FakeClient([]))
            state = executor._get_or_create_session_state("s1")
            state.active_stream = ClosableStream()

            interrupted = await executor.interrupt_session("s1")

            self.assertTrue(interrupted)
            self.assertTrue(state.interrupt_requested)
            self.assertTrue(state.active_stream.closed)

        _run(_t())


class TestOpenAIClientConfig(unittest.TestCase):
    def test_client_uses_openai_env(self):
        from omnigent.inner.open_responses_sdk import _get_openai_client
        from omnigent.spec.types import RetryPolicy

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True),
            patch("openai.OpenAI") as openai_cls,
        ):
            _get_openai_client()
            # Default RetryPolicy injects ``max_retries`` and ``timeout``
            # via its ``openai`` SDK adapter — confirm they are forwarded.
            openai_cls.assert_called_once_with(
                api_key="test-key",
                **RetryPolicy().openai.kwargs(),
            )

    def test_client_uses_openai_base_url_override(self):
        from omnigent.inner.open_responses_sdk import _get_openai_client
        from omnigent.spec.types import RetryPolicy

        with (
            patch.dict(
                "os.environ",
                {
                    "OPENAI_BASE_URL": "https://example.com/serving-endpoints",
                    "OPENAI_API_KEY": "test-key",
                },
                clear=True,
            ),
            patch("openai.OpenAI") as openai_cls,
        ):
            _get_openai_client()
            openai_cls.assert_called_once_with(
                base_url="https://example.com/serving-endpoints",
                api_key="test-key",
                **RetryPolicy().openai.kwargs(),
            )

    def test_client_uses_databricks_config(self):
        from omnigent.inner.databricks_executor import DatabricksCredentials
        from omnigent.inner.open_responses_sdk import _get_openai_client
        from omnigent.spec.types import RetryPolicy

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test",
                ),
            ),
            patch("openai.OpenAI") as openai_cls,
        ):
            _get_openai_client()
            openai_cls.assert_called_once_with(
                base_url="https://example.cloud.databricks.com/ai-gateway/openai/v1",
                api_key="dapi_test",
                **RetryPolicy().openai.kwargs(),
            )


class TestDatabricksBaseUrl(unittest.TestCase):
    def test_databricks_openai_base_url_normalizes_trailing_slash(self):
        self.assertEqual(
            _databricks_openai_base_url("https://example.cloud.databricks.com/"),
            "https://example.cloud.databricks.com/ai-gateway/openai/v1",
        )
