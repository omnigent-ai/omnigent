"""Tests for ``_normalize_turn_error`` — the ``failed`` status error shape.

Every turn failure the UI renders is normalized here into the
``{code, message}`` pair the wire schema requires. That ``code`` is what
lands in the durable ``last_task_error`` the web rebuilds a failure card
from on reconnect, and the card's headline is looked up by code, so a call
site's explicit code has to survive: publishing the raised exception's
class name instead degraded the card to a bare "Something went wrong".
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import _normalize_turn_error


def test_explicit_code_wins_over_exception_type() -> None:
    """A call site's own code is published, not the exception class."""
    normalized = _normalize_turn_error(
        {
            "code": "connection_error",
            "message": "Harness stream connection error.",
            "type": "ReadError",
        }
    )
    assert normalized == {
        "code": "connection_error",
        "message": "Harness stream connection error.",
    }


def test_context_overflow_keeps_its_code() -> None:
    """The overflow failure keeps a describable code, not ``_ContextWindowOverflow``."""
    normalized = _normalize_turn_error(
        {
            "code": "context_length_exceeded",
            "message": "Context window exceeded: 1 tokens > 0 max",
            "type": "_ContextWindowOverflow",
        }
    )
    assert normalized["code"] == "context_length_exceeded"


def test_exception_type_is_used_when_no_code_is_given() -> None:
    """Call sites that only carry ``type`` are unchanged."""
    normalized = _normalize_turn_error({"message": "boom", "type": "RuntimeError"})
    assert normalized == {"code": "RuntimeError", "message": "boom"}


@pytest.mark.parametrize("error", [{}, {"code": ""}, {"code": None}, {"type": ""}])
def test_missing_or_blank_code_falls_back(error: dict[str, object]) -> None:
    """A blank or absent code still yields the generic runner code."""
    assert _normalize_turn_error(error)["code"] == "runner_error"


def test_status_only_error_keeps_its_generated_message() -> None:
    """The ``{"status": ...}`` shape is unaffected by the code preference."""
    assert _normalize_turn_error({"status": 502}) == {
        "code": "runner_error",
        "message": "turn failed (status 502)",
    }
