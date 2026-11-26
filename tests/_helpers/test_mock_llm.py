"""Unit coverage for captured mock-LLM request inspection."""

from __future__ import annotations

import pytest

from tests._helpers.mock_llm import captured_tool_result, tool_result_text


def _messages_result(content: object, call_id: str = "call-1", is_error: object = None) -> dict:
    """One captured Messages request whose user turn answers *call_id*."""
    block: dict = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"messages": [{"role": "user", "content": [block]}]}


def _responses_result(output: object, call_id: str = "call-1") -> dict:
    """One captured Responses request answering *call_id*."""
    return {"input": [{"type": "function_call_output", "call_id": call_id, "output": output}]}


@pytest.mark.parametrize(
    ("captured_request", "expected"),
    [
        pytest.param(
            _messages_result(
                [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
                is_error=True,
            ),
            "is_error=True line one\nline two",
            id="messages-error-text-blocks",
        ),
        pytest.param(
            _messages_result('{"conversation_id": "conv_1"}'),
            '{"conversation_id": "conv_1"}',
            id="messages-string-content",
        ),
        pytest.param(
            _responses_result('{"conversation_id": "conv_2"}'),
            '{"conversation_id": "conv_2"}',
            id="responses-function-output",
        ),
    ],
)
def test_captured_tool_result_returns_the_payload_text(
    captured_request: dict, expected: str
) -> None:
    assert captured_tool_result([captured_request], "call-1") == expected


def test_captured_tool_result_newest_match_wins() -> None:
    requests = [
        _messages_result("first attempt"),
        _responses_result("second attempt"),
        _messages_result('{"conversation_id": "newest"}', is_error=False),
    ]
    assert captured_tool_result(requests, "call-1") == '{"conversation_id": "newest"}'


def test_captured_tool_result_reports_a_missing_call_id() -> None:
    assert (
        captured_tool_result([_messages_result("other", call_id="call-2")], "call-1")
        == "no captured tool result for 'call-1'"
    )


def test_captured_tool_result_keeps_a_long_payload_whole() -> None:
    payload = '{"detail": "' + "x" * 2000 + '"}'
    result = captured_tool_result([_responses_result(payload)], "call-1")
    assert result == payload
    assert len(result) > 2000


@pytest.mark.parametrize(
    ("payload", "error", "expected"),
    [
        pytest.param("text", None, "text", id="string"),
        pytest.param("text", True, "is_error=True text", id="string-error"),
        pytest.param(
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            False,
            "a\nb",
            id="blocks-no-error",
        ),
        pytest.param(None, None, "", id="empty"),
    ],
)
def test_tool_result_text_formats_payload_and_error_flag(
    payload: object, error: object, expected: str
) -> None:
    assert tool_result_text(payload, error=error) == expected
