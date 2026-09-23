"""Unit tests for native-terminal-start error classification.

The runner builds every native-terminal-start failure payload through
``_native_terminal_start_error_payload``. A deleted/rebound session agent is a
session-lifecycle condition, not a terminal-startup defect: it must surface a
distinct ``session_agent_missing`` code with a client-safe remedy message,
while any other cause keeps the generic ``native_terminal_start_failed``
startup-defect code. These are fast, layer-local guards for that split.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
import pytest

from omnigent.debug_logging import record_to_row
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.native import orchestration
from omnigent.runner.native.orchestration import (
    _NATIVE_TERMINAL_START_FAILED_CODE,
    _native_terminal_start_error_payload,
    _native_terminal_start_error_response,
    _publish_native_terminal_start_error,
)

_ERROR_ID_RE = re.compile(r" Error ID: (err_[0-9a-f]{32})\.$")


def test_missing_session_agent_classified_as_lifecycle_condition() -> None:
    """A ``SESSION_AGENT_MISSING`` cause yields the distinct lifecycle code.

    The payload must carry ``session_agent_missing`` (not the generic
    startup-defect code) and a client-safe message that names the remedy,
    does not relabel the condition as a terminal-startup failure, and never
    leaks the internal spec-resolver text.
    """
    exc = OmnigentError(
        "session spec resolver: agent 'abc123' for session 'conv_1' was not found",
        code=ErrorCode.SESSION_AGENT_MISSING,
    )

    payload = _native_terminal_start_error_payload(exc, "Claude", session_id="conv_1")

    assert payload["code"] == ErrorCode.SESSION_AGENT_MISSING
    message = payload["message"]
    # Actionable, client-safe wording about the lifecycle condition.
    assert "agent is no longer available" in message
    # Must NOT relabel the lifecycle event as a generic startup defect.
    assert "Native Claude terminal failed to start" not in message
    # Must NOT leak the internal resolver detail or the raw agent id.
    assert "session spec resolver" not in message
    assert "abc123" not in message
    # Correlation id preserved so operators can cross-reference the log.
    match = _ERROR_ID_RE.search(message)
    assert match is not None, message
    assert payload["error_id"] == match.group(1)


def test_other_causes_keep_generic_startup_failure_code() -> None:
    """A non-lifecycle cause keeps the generic startup-defect code.

    The reclassification is scoped to the missing-agent lifecycle condition;
    an unrelated startup exception must still be attributed as a
    ``native_terminal_start_failed`` terminal-startup defect.
    """
    payload = _native_terminal_start_error_payload(
        RuntimeError("tmux server exited before the pane was ready"),
        "Claude",
        session_id="conv_1",
    )

    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert payload["code"] == "native_terminal_start_failed"
    assert "agent is no longer available" not in payload["message"]


def test_unrelated_omnigent_error_is_not_treated_as_missing_agent() -> None:
    """An ``OmnigentError`` with a different code is not reclassified.

    Only the ``SESSION_AGENT_MISSING`` code selects the lifecycle branch; an
    ``OmnigentError`` carrying an unrelated code must fall through to the
    generic startup-defect classification.
    """
    exc = OmnigentError("something else broke", code=ErrorCode.INTERNAL_ERROR)

    payload = _native_terminal_start_error_payload(exc, "Claude", session_id="conv_1")

    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "agent is no longer available" not in payload["message"]


@pytest.mark.parametrize("missing_agent", [False, True])
def test_startup_failure_diagnostics_belong_to_failing_child(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing_agent: bool,
) -> None:
    """A shared runner's parent must not receive a child's startup failure evidence."""
    monkeypatch.setattr(orchestration, "runner_primary_session_id", lambda: "parent-session")
    private_detail = "private launch configuration"
    if missing_agent:
        exc = OmnigentError(private_detail, code=ErrorCode.SESSION_AGENT_MISSING)
    else:
        exc = RuntimeError(private_detail)
        exc.__cause__ = httpx.ReadTimeout("private upstream URL")

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        payload = _publish_native_terminal_start_error(
            lambda _session_id, _event: None, "child-session", "Codex", exc
        )

    record = next(r for r in caplog.records if payload["error_id"] in r.getMessage())
    row = record_to_row(record, source="runner")
    assert row["session_id"] == "child-session"
    assert row["event_name"] == "native_terminal_start_failed"
    attributes = row["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["error_id"] == payload["error_id"]
    assert attributes["code"] == payload["code"]
    assert attributes["runtime"] == "Codex"
    assert attributes["exception_type"] == type(exc).__name__
    assert private_detail not in str(attributes)
    assert private_detail not in payload["message"]
    if missing_agent:
        assert row["stack_trace"] is None
    else:
        assert attributes["exception_cause_type"] == "ReadTimeout"
        assert "ReadTimeout" in str(row["stack_trace"])


def test_ensure_response_and_diagnostic_share_error_and_session_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The HTTP error's opaque ID locates its cause on the requested session."""
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        response = _native_terminal_start_error_response(
            OSError("private launch path"), "Codex", session_id="ensured-session"
        )

    payload = json.loads(response.body)["error"]
    record = next(r for r in caplog.records if payload["error_id"] in r.getMessage())
    row = record_to_row(record, source="runner")
    assert response.status_code == 500
    assert row["session_id"] == "ensured-session"
    attributes = row["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["error_id"] == payload["error_id"]
    assert attributes["exception_type"] == "OSError"
    assert "private launch path" not in str(attributes)
    assert "private launch path" not in payload["message"]
