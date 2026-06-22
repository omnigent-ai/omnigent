"""Tests for llms.types — dataclass construction and edge cases."""

from omnigent.llms.types import (
    NATIVE_TOOL_OUTPUT_TYPES,
    FunctionCallOutput,
    MessageOutput,
    NativeToolOutput,
    NativeToolOutputAddedEvent,
    OutputText,
    Response,
    ResponseCompletedEvent,
    ResponseReasoningStartedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
    Usage,
)

# ── OutputText ──────────────────────────────────────────


def test_output_text_defaults() -> None:
    ot = OutputText(text="Hello")
    assert ot.text == "Hello"
    assert ot.type == "output_text"
    assert ot.annotations is None


def test_output_text_with_annotations() -> None:
    annotations = [{"type": "file_citation", "file_id": "f1"}]
    ot = OutputText(text="Hello", annotations=annotations)
    assert ot.annotations == annotations


# ── MessageOutput ───────────────────────────────────────


def test_message_output_defaults() -> None:
    mo = MessageOutput(content=[OutputText(text="Hi")])
    assert mo.type == "message"
    assert len(mo.content) == 1
    assert mo.content[0].text == "Hi"


# ── FunctionCallOutput ──────────────────────────────────


def test_function_call_output_defaults() -> None:
    fc = FunctionCallOutput(call_id="c1", name="fn", arguments="{}")
    assert fc.type == "function_call"
    assert fc.call_id == "c1"
    assert fc.name == "fn"
    assert fc.arguments == "{}"


# ── NativeToolOutput ────────────────────────────────────


def test_native_tool_output() -> None:
    data = {"type": "web_search_call", "id": "ws_1", "status": "completed"}
    nto = NativeToolOutput(data=data)
    assert nto.data["type"] == "web_search_call"


# ── NATIVE_TOOL_OUTPUT_TYPES ────────────────────────────


def test_native_tool_output_types_is_frozenset() -> None:
    assert isinstance(NATIVE_TOOL_OUTPUT_TYPES, frozenset)
    assert "web_search_call" in NATIVE_TOOL_OUTPUT_TYPES
    assert "file_search_call" in NATIVE_TOOL_OUTPUT_TYPES
    assert "code_interpreter_call" in NATIVE_TOOL_OUTPUT_TYPES
    assert "mcp_call" in NATIVE_TOOL_OUTPUT_TYPES


# ── Usage ───────────────────────────────────────────────


def test_usage_defaults_to_none() -> None:
    u = Usage()
    assert u.input_tokens is None
    assert u.output_tokens is None
    assert u.total_tokens is None


def test_usage_with_values() -> None:
    u = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
    assert u.input_tokens == 10
    assert u.output_tokens == 5
    assert u.total_tokens == 15


def test_usage_equality() -> None:
    a = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
    b = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
    assert a == b


def test_usage_inequality() -> None:
    a = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
    b = Usage(input_tokens=10, output_tokens=6, total_tokens=16)
    assert a != b


# ── Response ────────────────────────────────────────────


def test_response_with_text() -> None:
    resp = Response(
        output=[MessageOutput(content=[OutputText(text="Hello")])],
        model="gpt-5.4",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    )
    assert resp.model == "gpt-5.4"
    assert len(resp.output) == 1
    assert resp.usage is not None
    assert resp.usage.total_tokens == 15


def test_response_no_usage() -> None:
    resp = Response(output=[], model="test-model")
    assert resp.usage is None


def test_response_mixed_output() -> None:
    resp = Response(
        output=[
            MessageOutput(content=[OutputText(text="Hi")]),
            FunctionCallOutput(call_id="c1", name="fn", arguments="{}"),
            NativeToolOutput(data={"type": "web_search_call"}),
        ],
        model="test-model",
    )
    assert len(resp.output) == 3
    assert isinstance(resp.output[0], MessageOutput)
    assert isinstance(resp.output[1], FunctionCallOutput)
    assert isinstance(resp.output[2], NativeToolOutput)


# ── Streaming event types ───────────────────────────────


def test_response_text_delta_event_defaults() -> None:
    e = ResponseTextDeltaEvent(delta="Hello")
    assert e.type == "response.output_text.delta"
    assert e.delta == "Hello"


def test_response_reasoning_text_delta_event_defaults() -> None:
    e = ResponseReasoningTextDeltaEvent(delta="thinking")
    assert e.type == "response.reasoning_text.delta"


def test_response_reasoning_summary_text_delta_event_defaults() -> None:
    e = ResponseReasoningSummaryTextDeltaEvent(delta="summary")
    assert e.type == "response.reasoning_summary_text.delta"


def test_response_reasoning_started_event_defaults() -> None:
    e = ResponseReasoningStartedEvent()
    assert e.type == "response.reasoning.started"


def test_native_tool_output_added_event_defaults() -> None:
    item = {"type": "web_search_call", "id": "ws_1"}
    e = NativeToolOutputAddedEvent(item=item)
    assert e.type == "response.output_item.done"
    assert e.item is item


def test_response_completed_event_defaults() -> None:
    resp = Response(output=[], model="test")
    e = ResponseCompletedEvent(response=resp)
    assert e.type == "response.completed"
    assert e.response is resp
