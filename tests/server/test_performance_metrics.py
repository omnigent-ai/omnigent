"""Tests for server performance metrics tracking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import httpx
import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request
from opentelemetry.util.types import Attributes
from starlette.types import Scope

from omnigent.server.performance_metrics import (
    RequestDurationAccessFormatter,
    ServerMetricsOtelPublisher,
    ServerMetricsSnapshot,
    ServerPerformanceMetrics,
    SystemLoadAverage,
    publish_server_metrics_periodically,
    set_request_duration_for_access_log,
    set_request_id_for_access_log,
    set_request_session_id_for_access_log,
    set_request_user_agent_for_access_log,
)


def _default_load_average() -> SystemLoadAverage:
    """
    Return the default fake load average used by metrics tests.

    :returns: One-, five-, and fifteen-minute load averages.
    """
    return SystemLoadAverage(
        one_minute=1.0,
        five_minutes=2.0,
        fifteen_minutes=3.0,
    )


@dataclass
class _FakeMetricInputs:
    """
    Deterministic metric input source for unit tests.

    :param wall: Monotonic wall time in seconds.
    :param cpu: Process CPU time in seconds.
    :param rss: Resident memory in bytes.
    :param load: One-, five-, and fifteen-minute load average values.
    """

    wall: float = 0.0
    cpu: float = 0.0
    rss: int = 128 * 1024 * 1024
    load: SystemLoadAverage = field(default_factory=_default_load_average)

    def clock(self) -> float:
        """
        Return the current fake wall time.

        :returns: Monotonic wall time in seconds.
        """
        return self.wall

    def process_time(self) -> float:
        """
        Return the current fake process CPU time.

        :returns: Process CPU time in seconds.
        """
        return self.cpu

    def rss_bytes(self) -> int:
        """
        Return fake resident memory usage.

        :returns: Resident memory in bytes.
        """
        return self.rss

    def load_average(self) -> SystemLoadAverage:
        """
        Return fake system load averages.

        :returns: One-, five-, and fifteen-minute load averages.
        """
        return self.load


@dataclass(frozen=True)
class _MetricRecord:
    """
    Recorded metric value from a fake OpenTelemetry instrument.

    :param amount: Numeric metric value.
    :param attributes: Metric attributes recorded with the value.
    """

    amount: int | float
    attributes: Attributes


@dataclass
class _FakeCounter:
    """
    Fake OpenTelemetry counter that records ``add`` calls.

    :param name: Instrument name, e.g.
        ``"omnigent.server.http.requests.started"``.
    :param records: Values added to the counter.
    """

    name: str
    records: list[_MetricRecord] = field(default_factory=list)

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        """
        Record a counter delta.

        :param amount: Counter delta.
        :param attributes: Optional metric attributes.
        """
        self.records.append(_MetricRecord(amount=amount, attributes=attributes))


@dataclass
class _FakeGauge:
    """
    Fake OpenTelemetry gauge that records ``set`` calls.

    :param name: Instrument name, e.g.
        ``"omnigent.server.http.requests.in_flight"``.
    :param records: Values set on the gauge.
    """

    name: str
    records: list[_MetricRecord] = field(default_factory=list)

    def set(self, amount: int | float, attributes: Attributes = None) -> None:
        """
        Record a gauge value.

        :param amount: Gauge value.
        :param attributes: Optional metric attributes.
        """
        self.records.append(_MetricRecord(amount=amount, attributes=attributes))


@dataclass
class _FakeHistogram:
    """
    Fake OpenTelemetry histogram that records ``record`` calls.

    :param name: Instrument name, e.g.
        ``"omnigent.server.http.request.duration"``.
    :param records: Values recorded in the histogram.
    """

    name: str
    records: list[_MetricRecord] = field(default_factory=list)

    def record(self, amount: int | float, attributes: Attributes = None) -> None:
        """
        Record a histogram sample.

        :param amount: Histogram sample value.
        :param attributes: Optional metric attributes.
        """
        self.records.append(_MetricRecord(amount=amount, attributes=attributes))


@dataclass
class _FakeMeter:
    """
    Fake OpenTelemetry meter that creates recording instruments.

    :param counters: Counters created by metric name.
    :param gauges: Gauges created by metric name.
    :param histograms: Histograms created by metric name.
    """

    counters: dict[str, _FakeCounter] = field(default_factory=dict)
    gauges: dict[str, _FakeGauge] = field(default_factory=dict)
    histograms: dict[str, _FakeHistogram] = field(default_factory=dict)

    def create_counter(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _FakeCounter:
        """
        Create and record a fake counter.

        :param name: Metric name.
        :param unit: Metric unit.
        :param description: Metric description.
        :returns: Fake counter instrument.
        """
        counter = _FakeCounter(name)
        self.counters[name] = counter
        return counter

    def create_gauge(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _FakeGauge:
        """
        Create and record a fake gauge.

        :param name: Metric name.
        :param unit: Metric unit.
        :param description: Metric description.
        :returns: Fake gauge instrument.
        """
        gauge = _FakeGauge(name)
        self.gauges[name] = gauge
        return gauge

    def create_histogram(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _FakeHistogram:
        """
        Create and record a fake histogram.

        :param name: Metric name.
        :param unit: Metric unit.
        :param description: Metric description.
        :returns: Fake histogram instrument.
        """
        histogram = _FakeHistogram(name)
        self.histograms[name] = histogram
        return histogram


def _fake_metrics(inputs: _FakeMetricInputs) -> ServerPerformanceMetrics:
    """
    Build a metrics tracker wired to fake inputs.

    :param inputs: Fake input source for clocks and process metrics.
    :returns: Metrics tracker using deterministic input functions.
    """
    return ServerPerformanceMetrics(
        clock=inputs.clock,
        process_time_fn=inputs.process_time,
        rss_bytes_fn=inputs.rss_bytes,
        load_avg_fn=inputs.load_average,
    )


def _websocket_scope(path: str) -> Scope:
    """
    Build a minimal ASGI WebSocket scope for metrics tests.

    :param path: WebSocket path, e.g.
        ``"/v1/runners/metrics-runner/tunnel"``.
    :returns: ASGI WebSocket scope accepted by FastAPI.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def test_snapshot_reports_rolling_request_windows_and_process_metrics() -> None:
    """
    Rolling counters include request starts and processing time.

    A request started at t=0 falls out of the 30s window at t=31,
    while requests at t=5 and t=25 remain. Completed request timings
    are averaged by completion time, so only the t=25 completion is in
    the 10s processing window at t=31.
    """
    inputs = _FakeMetricInputs()
    metrics = _fake_metrics(inputs)

    started_at = metrics.request_started()
    inputs.wall = 0.1
    metrics.request_finished(started_at=started_at)
    inputs.wall = 5.0
    started_at = metrics.request_started()
    inputs.wall = 5.4
    metrics.request_finished(started_at=started_at, failed=True)
    inputs.wall = 25.0
    started_at = metrics.request_started()
    inputs.wall = 25.5
    metrics.request_finished(started_at=started_at)

    inputs.wall = 31.0
    inputs.cpu = 2.5
    snapshot = metrics.snapshot()

    assert snapshot.in_flight == 0
    assert snapshot.total_started == 3
    assert snapshot.total_completed == 3
    assert snapshot.total_failed == 1
    assert snapshot.requests_last_1s == 0
    assert snapshot.requests_last_10s == 1
    assert snapshot.requests_last_30s == 2
    assert snapshot.active_websockets == 0
    assert snapshot.request_processing_avg_ms == pytest.approx((0.1 + 0.4 + 0.5) / 3 * 1000.0)
    assert snapshot.request_processing_max_ms == pytest.approx(500.0)
    assert snapshot.request_processing_avg_1s_ms == 0.0
    assert snapshot.request_processing_avg_10s_ms == pytest.approx(500.0)
    assert snapshot.request_processing_avg_30s_ms == pytest.approx((0.4 + 0.5) / 2 * 1000.0)
    assert snapshot.process_cpu_percent == pytest.approx(2.5 / 31.0 * 100.0)
    assert snapshot.load_average_1m == 1.0
    assert snapshot.load_average_5m == 2.0
    assert snapshot.load_average_15m == 3.0
    assert snapshot.rss_bytes == 128 * 1024 * 1024
    assert snapshot.rss_mib == 128.0


def test_snapshot_tracks_active_websocket_connections() -> None:
    """
    Active WebSocket counters increment, decrement, and never underflow.

    If accepted WebSocket lifecycle tracking regresses, the exact
    snapshot assertions below catch both missing increments and
    double-decrement underflow.
    """
    metrics = _fake_metrics(_FakeMetricInputs())

    metrics.websocket_connected()
    metrics.websocket_connected()
    assert metrics.snapshot().active_websockets == 2

    metrics.websocket_disconnected()
    assert metrics.snapshot().active_websockets == 1

    metrics.websocket_disconnected()
    metrics.websocket_disconnected()
    assert metrics.snapshot().active_websockets == 0


def test_request_duration_access_formatter_appends_individual_duration() -> None:
    """
    Uvicorn access logs include individual request processing duration.

    A regression that drops the millisecond suffix from the Uvicorn access
    formatter would recreate the operator problem where access logs
    show that a request happened but not how long it spent in the
    server.
    """
    formatter = RequestDurationAccessFormatter(
        fmt='%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        use_colors=False,
    )
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "10.125.55.247:0",
            "GET",
            "/health?session_ids=conv_one%2Cconv_two",
            "1.1",
            200,
        ),
        exc_info=None,
    )

    set_request_duration_for_access_log(0.12345)
    try:
        assert formatter.format(record) == (
            'INFO:     10.125.55.247:0 - "GET '
            '/health?session_ids=conv_one%2Cconv_two HTTP/1.1" '
            "200 OK 123.5ms"
        )
        assert formatter.format(record) == (
            'INFO:     10.125.55.247:0 - "GET '
            '/health?session_ids=conv_one%2Cconv_two HTTP/1.1" '
            "200 OK"
        )
    finally:
        set_request_duration_for_access_log(None)


def _make_access_record() -> logging.LogRecord:
    """
    Build a minimal Uvicorn-style access log record.

    :returns: A ``LogRecord`` matching the shape Uvicorn's access
        logger emits.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "10.0.0.1:0",
            "GET",
            "/v1/sessions/conv_abc/events",
            "1.1",
            200,
        ),
        exc_info=None,
    )


def _make_formatter() -> RequestDurationAccessFormatter:
    """
    Create a formatter with colours disabled for deterministic assertions.

    :returns: Configured ``RequestDurationAccessFormatter``.
    """
    return RequestDurationAccessFormatter(
        fmt='%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        use_colors=False,
    )


def test_access_formatter_includes_request_id() -> None:
    """Request ID appears in the access log when set."""
    formatter = _make_formatter()
    record = _make_access_record()

    set_request_id_for_access_log("aabbccdd11223344")
    try:
        output = formatter.format(record)
        assert "rid=aabbccdd11223344" in output
    finally:
        set_request_id_for_access_log(None)


def test_access_formatter_includes_user_agent() -> None:
    """User-Agent header appears quoted in the access log when set."""
    formatter = _make_formatter()
    record = _make_access_record()

    set_request_user_agent_for_access_log("python-httpx/0.27")
    try:
        output = formatter.format(record)
        assert 'ua="python-httpx/0.27"' in output
    finally:
        set_request_user_agent_for_access_log(None)


def test_access_formatter_truncates_long_user_agent() -> None:
    """User-Agent values longer than 80 characters are truncated."""
    formatter = _make_formatter()
    record = _make_access_record()

    long_ua = "A" * 120
    set_request_user_agent_for_access_log(long_ua)
    try:
        output = formatter.format(record)
        assert f'ua="{"A" * 80}"' in output
        assert "A" * 81 not in output
    finally:
        set_request_user_agent_for_access_log(None)


def test_access_formatter_sanitizes_user_agent_control_chars() -> None:
    """A crafted User-Agent cannot inject newlines or terminal escapes."""
    formatter = _make_formatter()
    record = _make_access_record()

    # CRLF log-forging attempt plus an ANSI escape sequence.
    set_request_user_agent_for_access_log(
        'evil\r\n10.0.0.1 - "GET /admin" 200\x1b[2J',
    )
    try:
        output = formatter.format(record)
        # The whole access line stays on one physical line.
        assert "\n" not in output
        assert "\r" not in output
        assert "\x1b" not in output
        # Sanitized field is present with control chars replaced by '?'.
        assert "ua=" in output
    finally:
        set_request_user_agent_for_access_log(None)


def test_access_formatter_sanitizes_quote_in_user_agent() -> None:
    """An embedded double quote cannot break out of the quoted ua field."""
    formatter = _make_formatter()
    record = _make_access_record()

    set_request_user_agent_for_access_log('foo" bar')
    try:
        output = formatter.format(record)
        # Exactly one opening and one closing quote around the field.
        assert 'ua="foo? bar"' in output
    finally:
        set_request_user_agent_for_access_log(None)


def test_access_formatter_sanitizes_session_id_control_chars() -> None:
    """Control chars surviving URL parsing are stripped from the sid field."""
    formatter = _make_formatter()
    record = _make_access_record()

    # \x1b (ANSI ESC) survives Starlette's URL path parsing; ensure it
    # cannot reach the log line unescaped.
    set_request_session_id_for_access_log("conv_abc\x1b[31m")
    try:
        output = formatter.format(record)
        assert "\x1b" not in output
        assert "sid=conv_abc?[31m" in output
    finally:
        set_request_session_id_for_access_log(None)


def test_access_formatter_includes_session_id() -> None:
    """Session ID appears in the access log when set."""
    formatter = _make_formatter()
    record = _make_access_record()

    set_request_session_id_for_access_log("conv_abc")
    try:
        output = formatter.format(record)
        assert "sid=conv_abc" in output
    finally:
        set_request_session_id_for_access_log(None)


def test_access_formatter_includes_all_fields() -> None:
    """All enrichment fields appear together in the expected order."""
    formatter = _make_formatter()
    record = _make_access_record()

    set_request_duration_for_access_log(0.005)
    set_request_id_for_access_log("deadbeef")
    set_request_user_agent_for_access_log("curl/8.0")
    set_request_session_id_for_access_log("conv_xyz")
    try:
        output = formatter.format(record)
        assert "5.0ms" in output
        assert "rid=deadbeef" in output
        assert 'ua="curl/8.0"' in output
        assert "sid=conv_xyz" in output
        # Verify ordering: duration before rid before ua before sid.
        dur_pos = output.index("5.0ms")
        rid_pos = output.index("rid=")
        ua_pos = output.index("ua=")
        sid_pos = output.index("sid=")
        assert dur_pos < rid_pos < ua_pos < sid_pos
    finally:
        set_request_duration_for_access_log(None)
        set_request_id_for_access_log(None)
        set_request_user_agent_for_access_log(None)
        set_request_session_id_for_access_log(None)


def test_access_formatter_clears_context_after_format() -> None:
    """Context variables are cleared after formatting so they don't leak."""
    formatter = _make_formatter()
    record = _make_access_record()

    set_request_id_for_access_log("first_req")
    set_request_user_agent_for_access_log("test-agent/1.0")
    set_request_session_id_for_access_log("conv_123")

    first = formatter.format(record)
    assert "rid=first_req" in first
    assert 'ua="test-agent/1.0"' in first
    assert "sid=conv_123" in first

    second = formatter.format(record)
    assert "rid=" not in second
    assert "ua=" not in second
    assert "sid=" not in second


def test_access_formatter_omits_absent_fields() -> None:
    """Fields not set via context variables are omitted, not blank."""
    formatter = _make_formatter()
    record = _make_access_record()

    set_request_id_for_access_log("only_rid")
    try:
        output = formatter.format(record)
        assert "rid=only_rid" in output
        assert "ua=" not in output
        assert "sid=" not in output
    finally:
        set_request_id_for_access_log(None)


@pytest.mark.asyncio
async def test_middleware_sets_request_id_header_and_access_log_context(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The metrics middleware sets X-Request-Id on responses and populates
    access log context variables for request ID, User-Agent, and
    session ID.
    """
    from omnigent.server import app as server_app

    captured_ids: list[str | None] = []
    captured_uas: list[str | None] = []
    captured_sids: list[str | None] = []

    _original_set_rid = set_request_id_for_access_log
    _original_set_ua = set_request_user_agent_for_access_log
    _original_set_sid = set_request_session_id_for_access_log

    def spy_rid(v: str | None) -> None:
        captured_ids.append(v)
        _original_set_rid(v)

    def spy_ua(v: str | None) -> None:
        captured_uas.append(v)
        _original_set_ua(v)

    def spy_sid(v: str | None) -> None:
        captured_sids.append(v)
        _original_set_sid(v)

    monkeypatch.setattr(server_app, "set_request_id_for_access_log", spy_rid)
    monkeypatch.setattr(server_app, "set_request_user_agent_for_access_log", spy_ua)
    monkeypatch.setattr(server_app, "set_request_session_id_for_access_log", spy_sid)

    resp = await client.get("/health")

    assert resp.status_code == 200
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) == 32  # uuid4 hex

    assert len(captured_ids) == 1
    assert captured_ids[0] == resp.headers["x-request-id"]

    assert len(captured_uas) == 1
    assert captured_uas[0] is not None  # httpx sends a User-Agent

    assert len(captured_sids) == 1
    assert captured_sids[0] is None  # /health has no session ID


@pytest.mark.asyncio
async def test_middleware_extracts_session_id_from_path(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The middleware extracts the session ID from session-scoped URL paths.
    """
    from omnigent.server import app as server_app

    captured_sids: list[str | None] = []
    _original = set_request_session_id_for_access_log

    def spy_sid(v: str | None) -> None:
        captured_sids.append(v)
        _original(v)

    monkeypatch.setattr(server_app, "set_request_session_id_for_access_log", spy_sid)

    # This will 404 but the middleware still runs and extracts the ID.
    await client.get("/v1/sessions/conv_test123/events")

    assert len(captured_sids) == 1
    assert captured_sids[0] == "conv_test123"


def test_otel_publisher_emits_snapshot_values_and_counter_deltas() -> None:
    """
    The OTEL publisher exports every snapshot metric value.

    Counter assertions prove cumulative snapshot totals are converted
    to deltas; gauge assertions prove the same operational fields as
    the periodic log line are emitted.
    """
    meter = _FakeMeter()
    publisher = ServerMetricsOtelPublisher(meter=meter)
    first = ServerMetricsSnapshot(
        in_flight=2,
        total_started=9,
        total_completed=7,
        total_failed=1,
        requests_last_1s=3,
        requests_last_10s=5,
        requests_last_30s=8,
        active_websockets=4,
        request_processing_avg_ms=123.45,
        request_processing_max_ms=987.65,
        request_processing_avg_1s_ms=111.11,
        request_processing_avg_10s_ms=222.22,
        request_processing_avg_30s_ms=333.33,
        process_cpu_percent=12.34,
        load_average_1m=1.25,
        load_average_5m=2.5,
        load_average_15m=3.75,
        rss_bytes=64 * 1024 * 1024,
    )
    second = ServerMetricsSnapshot(
        in_flight=1,
        total_started=11,
        total_completed=10,
        total_failed=1,
        requests_last_1s=2,
        requests_last_10s=4,
        requests_last_30s=6,
        active_websockets=3,
        request_processing_avg_ms=222.0,
        request_processing_max_ms=999.0,
        request_processing_avg_1s_ms=100.0,
        request_processing_avg_10s_ms=200.0,
        request_processing_avg_30s_ms=300.0,
        process_cpu_percent=50.0,
        load_average_1m=None,
        load_average_5m=None,
        load_average_15m=None,
        rss_bytes=32 * 1024 * 1024,
    )

    publisher.publish(first)
    publisher.publish(second)

    assert [
        record.amount for record in meter.counters["omnigent.server.http.requests.started"].records
    ] == [9, 2]
    assert [
        record.amount
        for record in meter.counters["omnigent.server.http.requests.completed"].records
    ] == [7, 3]
    assert [
        record.amount for record in meter.counters["omnigent.server.http.requests.failed"].records
    ] == [1]
    assert meter.gauges["omnigent.server.http.requests.in_flight"].records[-1].amount == 1
    assert meter.gauges["omnigent.server.http.requests.last_1s"].records[-1].amount == 2
    assert meter.gauges["omnigent.server.http.requests.last_10s"].records[-1].amount == 4
    assert meter.gauges["omnigent.server.http.requests.last_30s"].records[-1].amount == 6
    assert meter.gauges["omnigent.server.websocket.connections.active"].records[-1].amount == 3
    assert meter.gauges["omnigent.server.http.request.processing.avg"].records[-1].amount == 222.0
    assert meter.gauges["omnigent.server.process.cpu.percent"].records[-1].amount == 50.0
    assert meter.gauges["omnigent.server.system.load_average.1m"].records[-1].amount == 1.25
    assert meter.gauges["omnigent.server.process.memory.rss"].records[-1].amount == (
        32 * 1024 * 1024
    )


def test_otel_publisher_records_request_duration_histogram() -> None:
    """
    Request durations are exported as individual OTEL histogram samples.
    """
    meter = _FakeMeter()
    publisher = ServerMetricsOtelPublisher(meter=meter)

    publisher.record_request_duration(
        duration_seconds=0.125,
        failed=True,
        method="POST",
        route="/v1/sessions/{session_id}",
        status_code=500,
    )

    records = meter.histograms["omnigent.server.http.request.duration"].records
    assert records == [
        _MetricRecord(
            amount=0.125,
            attributes={
                "failed": True,
                "http.request.method": "POST",
                "http.route": "/v1/sessions/{session_id}",
                "http.response.status_code": 500,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_publish_server_metrics_periodically_exports_until_cancelled() -> None:
    """
    The periodic publisher emits metric snapshots through OTEL.

    The test waits for concrete fake OTEL records, so it fails if the
    publisher stops snapshotting or forgets to publish counter/gauge
    values. Cancelling the task then verifies shutdown does not hang.
    """
    inputs = _FakeMetricInputs()
    metrics = _fake_metrics(inputs)
    metrics.request_started()
    meter = _FakeMeter()
    publisher = ServerMetricsOtelPublisher(meter=meter)
    task = asyncio.create_task(
        publish_server_metrics_periodically(
            metrics,
            otel_publisher=publisher,
            interval_seconds=0.01,
        )
    )

    async def wait_for_started_counter() -> None:
        """
        Wait until the fake OTEL started counter receives a record.
        """
        while not meter.counters["omnigent.server.http.requests.started"].records:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_started_counter(), timeout=1.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert [
        record.amount for record in meter.counters["omnigent.server.http.requests.started"].records
    ] == [1]
    assert meter.gauges["omnigent.server.http.requests.in_flight"].records[-1].amount == 1


@pytest.mark.asyncio
async def test_create_app_metrics_middleware_counts_http_requests(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``create_app`` wires metrics middleware into real HTTP handling.

    Driving ``GET /health`` through the test ASGI client exercises the
    same middleware path production requests use, then checks the
    app-owned tracker rather than a test-local stand-in.
    """
    from omnigent.server import app as server_app

    before = app.state.server_metrics.snapshot()

    access_log_durations: list[float | None] = []

    def spy_set_request_duration_for_access_log(
        duration_seconds: float | None,
    ) -> None:
        """
        Capture the duration handed from middleware to access logging.

        :param duration_seconds: Request processing duration in
            seconds, or ``None`` when cleared.
        """
        access_log_durations.append(duration_seconds)

    monkeypatch.setattr(
        server_app,
        "set_request_duration_for_access_log",
        spy_set_request_duration_for_access_log,
    )

    resp = await client.get(
        "/health",
        params={"session_ids": "conv_one,conv_two"},
    )

    after = app.state.server_metrics.snapshot()
    assert resp.status_code == 200
    assert after.total_started == before.total_started + 1
    assert after.total_completed == before.total_completed + 1
    assert after.total_failed == before.total_failed
    assert after.in_flight == 0
    assert after.active_websockets == before.active_websockets
    assert after.requests_last_1s >= 1
    assert after.requests_last_10s >= 1
    assert after.requests_last_30s >= 1
    assert after.request_processing_avg_ms >= 0.0
    assert after.request_processing_max_ms >= 0.0
    assert after.request_processing_avg_1s_ms >= 0.0
    assert after.request_processing_avg_10s_ms >= 0.0
    assert after.request_processing_avg_30s_ms >= 0.0
    assert len(access_log_durations) == 1
    assert access_log_durations[0] is not None
    assert access_log_durations[0] >= 0.0


def test_request_route_template_for_metrics_uses_sentinel_for_unmatched_routes() -> None:
    """
    Unmatched routes do not use the raw URL path as a metric label.

    A request scope with no matched Starlette route catches the
    cardinality regression where scanner paths or probe IDs leak into the
    ``http.route`` OTEL attribute.
    """
    from omnigent.server.app import request_route_template_for_metrics

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/scanner/probe/conv_1234567890abcdef",
        "headers": [],
        "query_string": b"",
    }

    assert request_route_template_for_metrics(Request(scope)) == "<unmatched>"


@pytest.mark.asyncio
async def test_create_app_websocket_metrics_count_accepted_connections(
    app: FastAPI,
) -> None:
    """
    ``create_app`` wires metrics middleware into real WebSocket handling.

    The runner tunnel route accepts before waiting for its hello frame,
    which lets the test observe the active counter while the accepted
    WebSocket remains open.
    """
    before = app.state.server_metrics.snapshot()
    communicator = ApplicationCommunicator(
        app,
        _websocket_scope("/v1/runners/metrics-runner/tunnel"),
    )

    try:
        await communicator.send_input({"type": "websocket.connect"})
        accepted = await communicator.receive_output(timeout=1.0)
        assert accepted["type"] == "websocket.accept"

        during = app.state.server_metrics.snapshot()
        assert during.active_websockets == before.active_websockets + 1
    finally:
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait(timeout=1.0)

    after = app.state.server_metrics.snapshot()
    assert after.active_websockets == before.active_websockets
