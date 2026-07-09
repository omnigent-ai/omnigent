"""
Unit tests for the OTel log bridge wired up in
``omnigent.runtime.telemetry``.

Exercises ``_init_otel_logs`` and verifies that log records emitted
inside an active span carry the span's trace_id and span_id once the
bridge is installed.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace import TracerProvider

from omnigent.runtime import telemetry

_BRIDGE_NAME = "omnigent-otel-log-bridge"


def _remove_bridge_handlers() -> None:
    """
    Strip any leftover OTel log bridge handlers from the root logger.

    The root logger is process-global. Without an explicit cleanup
    step, handlers attached by one test leak into the next. Each
    handler's provider is shut down so its background batch flush
    thread stops before pytest exits.

    :returns: ``None``.
    """
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if handler.get_name() != _BRIDGE_NAME:
            continue
        provider = getattr(handler, "_logger_provider", None)
        root_logger.removeHandler(handler)
        if provider is not None and hasattr(provider, "shutdown"):
            with contextlib.suppress(Exception):
                provider.shutdown()


@pytest.fixture(autouse=True)
def _opt_in_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Telemetry is opt-in (``OMNIGENT_TELEMETRY_ENABLED``, off by default);
    these log-bridge tests opt in for every test, and reset global tracing
    state afterward so init()/enable_tracing() can't leak into other suites.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    try:
        yield
    finally:
        from omnigent.inner.tracing import disable_tracing

        disable_tracing()
        telemetry._initialized = False


@pytest.fixture
def reset_log_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Reset the telemetry log bridge state between tests.

    Removes any handler the test installed on the root logger and
    clears the module-level ``_logs_initialized`` guard so the next
    test starts fresh.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(telemetry, "_logs_initialized", False)
    _remove_bridge_handlers()
    yield
    _remove_bridge_handlers()
    monkeypatch.setattr(telemetry, "_logs_initialized", False)


# ── _logs_exporter_name ─────────────────────────────────


def test_logs_exporter_name_otlp_from_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When only ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, the helper
    returns ``"otlp"`` so logs ride the same OTLP path as traces.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    assert telemetry._logs_exporter_name() == "otlp"


def test_logs_exporter_name_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no endpoint and no explicit exporter, logs default off.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry._logs_exporter_name() == "none"


def test_logs_exporter_name_explicit_none_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Operators can pin ``OTEL_LOGS_EXPORTER=none`` even when an OTLP
    endpoint is configured for traces.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    assert telemetry._logs_exporter_name() == "none"


# ── _init_otel_logs ─────────────────────────────────────


def test_init_otel_logs_attaches_handler_with_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    reset_log_state: None,
) -> None:
    """
    With an OTLP endpoint set, ``_init_otel_logs`` installs a
    ``LoggingHandler`` on the root logger so logs flow into the OTel
    bridge.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param reset_log_state: Bridge state reset fixture.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)

    telemetry._init_otel_logs()

    root_logger = logging.getLogger()
    bridge_handlers = [
        handler for handler in root_logger.handlers if handler.get_name() == _BRIDGE_NAME
    ]
    assert len(bridge_handlers) == 1, (
        f"expected exactly one OTel log bridge handler, got {len(bridge_handlers)}"
    )
    assert isinstance(bridge_handlers[0], LoggingHandler)


def test_init_otel_logs_noop_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    reset_log_state: None,
) -> None:
    """
    With no endpoint and no explicit exporter, no handler is
    attached. Operators who never opt in pay no overhead.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param reset_log_state: Bridge state reset fixture.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)

    telemetry._init_otel_logs()

    root_logger = logging.getLogger()
    bridge_handlers = [
        handler for handler in root_logger.handlers if handler.get_name() == _BRIDGE_NAME
    ]
    assert bridge_handlers == [], (
        "expected no OTel log bridge handler when no endpoint is configured"
    )


def test_init_otel_logs_idempotent_via_init(
    monkeypatch: pytest.MonkeyPatch,
    reset_log_state: None,
) -> None:
    """
    Calling :func:`telemetry.init` twice does not stack a second
    OTel log bridge handler on the root logger.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param reset_log_state: Bridge state reset fixture.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)

    telemetry.init()
    # Reset the one-shot guard so init() runs again and we can
    # verify the bridge does not double-attach.
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)
    telemetry.init()

    root_logger = logging.getLogger()
    bridge_handlers = [
        handler for handler in root_logger.handlers if handler.get_name() == _BRIDGE_NAME
    ]
    assert len(bridge_handlers) == 1, (
        f"expected exactly one OTel log bridge handler after two init() "
        f"calls, got {len(bridge_handlers)}"
    )


# ── trace_id / span_id propagation ──────────────────────


def test_log_emitted_in_span_carries_trace_and_span_ids(
    monkeypatch: pytest.MonkeyPatch,
    reset_log_state: None,
) -> None:
    """
    A log record emitted inside an active span carries the span's
    trace_id and span_id on the exported ``LogRecord``.

    Uses an ``InMemoryLogRecordExporter`` wired through a fresh
    ``LoggerProvider`` so the test never touches the network and
    can assert directly on emitted records.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param reset_log_state: Bridge state reset fixture.
    """
    # Fresh OTel tracer provider so we control the trace context.
    tracer_provider = TracerProvider()
    otel_trace._TRACER_PROVIDER = tracer_provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]

    log_exporter = InMemoryLogRecordExporter()
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    set_logger_provider(log_provider)

    handler = LoggingHandler(logger_provider=log_provider, level=logging.DEBUG)
    handler.set_name(_BRIDGE_NAME)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    previous_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)

    try:
        tracer = otel_trace.get_tracer("tests.runtime.telemetry_logs")
        with tracer.start_as_current_span("test-span") as span:
            expected_trace_id = span.get_span_context().trace_id
            expected_span_id = span.get_span_context().span_id
            logging.getLogger("omnigent.test").info("hello from inside the span")
    finally:
        root_logger.setLevel(previous_level)

    records = log_exporter.get_finished_logs()
    matched = [
        record for record in records if record.log_record.body == "hello from inside the span"
    ]
    assert len(matched) == 1, (
        f"expected one matching log record, got {len(matched)} (total records: {len(records)})"
    )
    log_record = matched[0].log_record
    assert log_record.trace_id == expected_trace_id, (
        f"log trace_id {log_record.trace_id:032x} does not match "
        f"span trace_id {expected_trace_id:032x}"
    )
    assert log_record.span_id == expected_span_id, (
        f"log span_id {log_record.span_id:016x} does not match "
        f"span span_id {expected_span_id:016x}"
    )
