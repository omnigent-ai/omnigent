"""The turn-error normalizer honors a known structured code (no error ``type`` present) so the
UI shows a specific reason, while an unknown/default code still collapses to ``runner_error``."""

from __future__ import annotations

from omnigent.runner.app import _normalize_turn_error


def test_known_structured_code_is_surfaced() -> None:
    result = _normalize_turn_error({"code": "codex_input_too_large", "message": "input too large"})
    assert result == {"code": "codex_input_too_large", "message": "input too large"}


def test_unknown_code_falls_back_to_runner_error() -> None:
    # The default ErrorDetail code is the exception class name; it must not leak
    # through as a surfaced turn code.
    result = _normalize_turn_error({"code": "RuntimeError", "message": "boom"})
    assert result["code"] == "runner_error"
    assert result["message"] == "boom"


def test_explicit_type_takes_precedence_over_code() -> None:
    result = _normalize_turn_error(
        {
            "type": "context_length_exceeded",
            "code": "codex_input_too_large",
            "message": "overflow",
        }
    )
    assert result["code"] == "context_length_exceeded"


def test_missing_type_and_code_reports_runner_error() -> None:
    assert _normalize_turn_error({"message": "generic"})["code"] == "runner_error"
