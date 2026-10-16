"""Tests for the slog-keyed JSON log formatter."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from omnigent.debug_logging import (
    set_current_request_id,
    set_current_session_id,
    set_current_user_id,
)
from omnigent.log_format import MAX_ATTR_BYTES, JsonFormatter, json_logging_requested


def _record(msg: str = "hello %s", *args: object, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="omnigent.server.app",
        level=level,
        pathname="/build/omnigent/server/app.py",
        lineno=42,
        msg=msg,
        args=args or ("world",),
        exc_info=None,
    )


def _format(record: logging.LogRecord) -> dict[str, object]:
    line = JsonFormatter().format(record)
    assert "\n" not in line
    return json.loads(
        line, parse_constant=lambda token: pytest.fail(f"Nonstandard JSON constant: {token}")
    )


@pytest.fixture(autouse=True)
def _clear_request_context() -> None:
    set_current_request_id(None)
    set_current_session_id(None)
    set_current_user_id(None)


def test_emits_slog_keys() -> None:
    record = _record()
    entry = _format(record)

    assert entry["msg"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["source"] == "server/app.py:42"
    assert entry["logger"] == "omnigent.server.app"
    stamp = str(entry["time"])
    assert stamp.endswith("Z")
    assert datetime.fromisoformat(stamp).timestamp() == pytest.approx(record.created, abs=1e-6)


@pytest.mark.parametrize(
    ("level", "expected"),
    [(logging.WARNING, "WARN"), (logging.CRITICAL, "ERROR"), (logging.DEBUG, "DEBUG")],
)
def test_maps_level_names_to_slog(level: int, expected: str) -> None:
    assert _format(_record(level=level))["level"] == expected


def test_extra_fields_become_top_level_keys() -> None:
    record = _record()
    record.event_name = "session_creation_started"
    record.attributes = {"operation": "create"}

    entry = _format(record)

    assert entry["event_name"] == "session_creation_started"
    assert entry["attributes"] == {"operation": "create"}


def test_ambient_request_context_is_included() -> None:
    set_current_request_id("rid123")
    set_current_session_id("conv_abc")
    set_current_user_id("alice@example.com")

    entry = _format(_record())

    assert entry["request_id"] == "rid123"
    assert entry["session_id"] == "conv_abc"
    assert entry["user_id"] == "alice@example.com"


def test_trace_id_from_active_span_context() -> None:
    span_context = SpanContext(
        trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
        span_id=0x00F067AA0BA902B7,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    token = otel_context.attach(trace.set_span_in_context(NonRecordingSpan(span_context)))
    try:
        entry = _format(_record())
    finally:
        otel_context.detach(token)

    assert entry["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.parametrize(
    ("value", "truncated", "expected"),
    [
        pytest.param("x" * (MAX_ATTR_BYTES * 2), True, ..., id="ascii"),
        pytest.param("😀" * MAX_ATTR_BYTES, True, ..., id="unicode"),
        pytest.param("\x00" * MAX_ATTR_BYTES, True, ..., id="control-characters"),
        pytest.param('"\\' * MAX_ATTR_BYTES, True, ..., id="escaped-characters"),
        pytest.param({"text": "😀" * MAX_ATTR_BYTES}, True, ..., id="nested-unicode"),
        pytest.param({"text": "\x00" * MAX_ATTR_BYTES}, True, ..., id="nested-controls"),
        pytest.param(["x" * 100] * 200, True, ..., id="nested-many-values"),
        pytest.param("x" * (MAX_ATTR_BYTES - 2), False, ..., id="ascii-at-limit"),
        pytest.param("😀" * ((MAX_ATTR_BYTES - 2) // 4), False, ..., id="unicode-below-limit"),
        pytest.param("\x00" * ((MAX_ATTR_BYTES - 2) // 6), False, ..., id="controls-below-limit"),
        pytest.param({"text": "😀" * 100}, False, ..., id="small-nested-unicode"),
        pytest.param(float("nan"), False, None, id="nan"),
        pytest.param(float("inf"), False, None, id="positive-infinity"),
        pytest.param(float("-inf"), False, None, id="negative-infinity"),
        pytest.param(
            {"numbers": [float("nan"), float("inf"), {"value": float("-inf")}, 1.5]},
            False,
            {"numbers": [None, None, {"value": None}, 1.5]},
            id="nested-non-finite",
        ),
        pytest.param((float("nan"), float("inf")), False, [None, None], id="tuple-non-finite"),
        pytest.param(1.5, False, ..., id="finite-number"),
    ],
)
def test_values_are_bounded(value: object, truncated: bool, expected: object) -> None:
    record = _record("%s", value)
    record.payload = value
    entry = _format(record)

    for key in ("msg", "payload"):
        size = len(json.dumps(entry[key], ensure_ascii=False).encode("utf-8"))
        assert size <= MAX_ATTR_BYTES
        if truncated:
            assert str(entry[key]).endswith("...[truncated]")
            assert size >= MAX_ATTR_BYTES - 6
        else:
            payload = value if expected is ... else expected
            assert entry[key] == (str(value) if key == "msg" else payload)
    if truncated:
        # Independent values may together exceed the per-value budget.
        assert len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) > MAX_ATTR_BYTES


def test_secrets_are_redacted() -> None:
    entry = _format(_record("calling with Authorization: Bearer %s", "sk-secret-value-123456"))

    assert "sk-secret-value-123456" not in str(entry["msg"])


def test_exception_renders_error_and_stack() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record()
        record.exc_info = sys.exc_info()

    entry = _format(record)

    assert entry["error"] == "ValueError: boom"
    assert "Traceback" in str(entry["stack"])


def test_json_logging_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_LOG_FORMAT", " JSON ")
    assert json_logging_requested() is True
    monkeypatch.setenv("OMNIGENT_LOG_FORMAT", "text")
    assert json_logging_requested() is False
