"""The executor translates an oversized-input rejection (JSON-RPC -32602 / input_too_large)
into a plain-English message the user can act on, never leaking the raw error dict."""

from __future__ import annotations

from omnigent.inner.codex_executor import _input_too_large_error


def test_reports_actual_and_limit_char_counts() -> None:
    message = _input_too_large_error(
        {
            "code": -32602,
            "data": {
                "input_error_code": "input_too_large",
                "max_chars": 1_048_576,
                "actual_chars": 1_450_257,
            },
            "message": "Input exceeds the maximum length of 1048576 characters.",
        }
    )
    assert message is not None
    assert "1450257" in message
    assert "1048576" in message
    # The raw JSON-RPC fragments must never reach the user.
    assert "-32602" not in message
    assert "input_error_code" not in message


def test_generic_message_when_counts_absent() -> None:
    message = _input_too_large_error({"data": {"input_error_code": "input_too_large"}})
    assert message is not None
    assert "maximum length" in message.lower()
    assert "-32602" not in message


def test_ignores_non_oversized_rejections() -> None:
    assert _input_too_large_error({"data": {"input_error_code": "other"}}) is None
    assert _input_too_large_error({"code": -32000, "message": "boom"}) is None
    assert _input_too_large_error("not a dict") is None
    assert _input_too_large_error({"data": "not a dict"}) is None
