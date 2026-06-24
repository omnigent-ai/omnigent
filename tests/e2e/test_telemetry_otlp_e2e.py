"""
End-to-end OTLP-over-HTTP test for omnigent telemetry.

Proves the BYO-OTel-backend contract: omnigent emits OTLP/protobuf to
whatever ``OTEL_EXPORTER_OTLP_ENDPOINT`` is pointed at, and the spans
that come out the other side carry the names, attributes (including
the GenAI semconv attrs added in PR #1050), and parent-child
relationships the agent runtime promises.

A stub OTLP/HTTP receiver runs in-process on an ephemeral port. No
external collector binary or container is needed, so the test is
runnable in CI without infrastructure. The protocol round-trip
(HTTP POST + protobuf-encoded ExportTraceServiceRequest) is real.
The span content is synthetic but representative of the
agent / llm / tool pattern the production code emits.

The test is opted out of the default ``pytest`` run two ways:

1. ``tests/e2e`` is in the ``addopts = "--ignore=tests/e2e ..."`` list
   in ``pyproject.toml``, so a bare ``pytest`` invocation never
   collects it.
2. The test carries ``@pytest.mark.e2e`` so explicit selection via
   ``pytest tests/e2e -m e2e --override-ini="addopts="`` is supported.

Run it locally with::

    python -m pytest tests/e2e/test_telemetry_otlp_e2e.py \\
        -xvs -m e2e --override-ini="addopts="
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, Span
from opentelemetry.sdk.trace import TracerProvider

from omnigent.inner.tracing import TracingContext
from omnigent.runtime import telemetry

_RESP_HEX = "abcdef0123456789abcdef0123456789"
_RESP_ID = f"resp_{_RESP_HEX}"


class _OTLPReceiver:
    """
    In-process OTLP/HTTP receiver that captures POSTed protobuf bodies.

    Listens on an ephemeral port, accepts ``POST /v1/traces`` with
    ``Content-Type: application/x-protobuf``, decodes the body as an
    ``ExportTraceServiceRequest`` and appends every ``ResourceSpans``
    entry to ``self.resource_spans``. Other paths return 404 so the
    test fails loud if the exporter targets the wrong route.
    """

    def __init__(self) -> None:
        self.requests: list[ExportTraceServiceRequest] = []
        self.resource_spans: list[ResourceSpans] = []
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/v1/traces":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                req = ExportTraceServiceRequest()
                try:
                    req.ParseFromString(body)
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return
                with receiver._lock:
                    receiver.requests.append(req)
                    receiver.resource_spans.extend(req.resource_spans)
                # An empty body is a valid success response per the
                # OTLP/HTTP spec.
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                # Silence the default stderr access log so test output
                # stays readable.
                return

        return _Handler

    def start(self) -> None:
        # Let the OS pick a free port.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self._server = HTTPServer(("127.0.0.1", self.port), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/traces"

    @property
    def all_spans(self) -> list[Span]:
        spans: list[Span] = []
        with self._lock:
            for rs in self.resource_spans:
                for ss in rs.scope_spans:
                    spans.extend(ss.spans)
        return spans


def _reset_mlflow_trace_state() -> None:
    """Wipe MLflow's in-memory trace manager between tests."""
    from mlflow.tracing.trace_manager import InMemoryTraceManager

    inst = getattr(InMemoryTraceManager, "_instance", None)
    if inst is not None:
        inst._traces.clear()  # type: ignore[attr-defined]
        inst._otel_id_to_mlflow_trace_id.clear()  # type: ignore[attr-defined]


@pytest.fixture
def otlp_receiver(monkeypatch: pytest.MonkeyPatch) -> Iterator[_OTLPReceiver]:
    """
    Start a localhost OTLP/HTTP receiver and wire telemetry to it.

    Installs a fresh ``TracerProvider`` (each test gets a clean span
    stream), points the OTLP HTTP exporter at the local receiver,
    calls ``telemetry.init()``, and yields the receiver. On teardown
    the provider is force-flushed, the receiver thread is joined, and
    the module-level init guards are reset so the next test starts
    clean.
    """
    receiver = _OTLPReceiver()
    receiver.start()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", receiver.endpoint)
    # Belt-and-suspenders: explicitly point the traces sub-endpoint too,
    # since some exporter versions read the per-signal env var first.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", receiver.endpoint)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("MLFLOW_USE_DEFAULT_TRACER_PROVIDER", "false")
    monkeypatch.delenv("MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT", raising=False)
    monkeypatch.setenv("MLFLOW_ENABLE_OTLP_EXPORTER", "true")

    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)

    _reset_mlflow_trace_state()

    provider = TracerProvider()
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]

    # Force MLflow to re-register its span processors on the new
    # provider. Without resetting the once-flag, mlflow.tracing.enable()
    # is a no-op and the OTLP processor never lands on our provider.
    from mlflow.tracing.provider import provider as mlflow_provider_wrapper

    mlflow_provider_wrapper._global_provider_init_once._done = False  # type: ignore[attr-defined]

    telemetry.init()

    try:
        yield receiver
    finally:
        # Drain in-flight batches before tearing down the receiver so
        # spans don't get dropped on the floor.
        with contextlib.suppress(Exception):
            provider.force_flush(timeout_millis=5000)
        with contextlib.suppress(Exception):
            provider.shutdown()
        receiver.stop()
        telemetry._initialized = False  # type: ignore[attr-defined]
        telemetry._metrics_initialized = False  # type: ignore[attr-defined]


def _attr_value_as_python(value: Any) -> Any:
    """
    Convert an OTLP ``AnyValue`` protobuf to a plain Python value.

    The OTLP wire format wraps every attribute in an ``AnyValue``
    oneof. Tests assert on logical values, so we project back to
    str / int / float / bool / list / dict via the populated oneof
    field.
    """
    which = value.WhichOneof("value")
    if which is None:
        return None
    raw = getattr(value, which)
    if which == "array_value":
        return [_attr_value_as_python(v) for v in raw.values]
    if which == "kvlist_value":
        return {kv.key: _attr_value_as_python(kv.value) for kv in raw.values}
    if which == "bytes_value":
        return bytes(raw)
    return raw


def _span_attrs(span: Span) -> dict[str, Any]:
    """
    Materialize an OTLP span's attributes as a Python dict.

    MLflow JSON-encodes string attribute values before OTLP export
    (so dicts and lists round-trip through OTel's string-only store),
    so each string attribute is also passed through ``json.loads``
    on a best-effort basis.
    """
    out: dict[str, Any] = {}
    for kv in span.attributes:
        v = _attr_value_as_python(kv.value)
        if isinstance(v, str):
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                v = json.loads(v)
        out[kv.key] = v
    return out


@pytest.mark.e2e
def test_otlp_round_trip_emits_expected_spans(otlp_receiver: _OTLPReceiver) -> None:
    """
    Drive the production span pattern through the real OTLP HTTP path.

    The synthetic agent -> llm -> tool sequence mirrors what a turn
    emits in production. We verify the receiver got the spans, the
    expected names and ``gen_ai.*`` attributes are present on the
    OTLP payload, parent-child relationships survive the round trip,
    and the trace id derived from the response id is what shows up
    on the wire (the BYO-backend lookup contract).
    """
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        ctx = TracingContext()
        agent = ctx.start_agent_span(
            agent_name="debby",
            user_message="hello",
            model="anthropic/claude-sonnet-4-5",
        )
        llm = ctx.start_llm_span(
            messages=[{"role": "user", "content": "hello"}],
            model="openai/gpt-5",
        )
        ctx.end_llm_span(llm, response_text="hi back")
        tool = ctx.start_tool_span(tool_name="calculator", tool_args={"x": 1})
        ctx.end_tool_span(tool, result={"answer": 1}, parent_span=agent)
        ctx.end_agent_span(agent, response="done")

    # Pull the OTel TracerProvider the fixture installed and drain
    # pending batches so the receiver has every span before we assert.
    provider = otel_trace.get_tracer_provider()
    assert provider.force_flush(timeout_millis=5000), (
        "force_flush returned False; OTLP exporter did not finish draining within 5s"
    )

    assert len(otlp_receiver.requests) >= 1, (
        "stub OTLP receiver got zero POSTs; either MLflow's OTLP "
        "exporter was not wired to our endpoint or no spans were "
        "emitted at all"
    )

    spans = otlp_receiver.all_spans
    names = [s.name for s in spans]
    assert "agent:debby" in names, f"missing agent span; got names={names!r}"
    assert "llm_call" in names, f"missing llm span; got names={names!r}"
    assert "tool:calculator" in names, f"missing tool span; got names={names!r}"

    by_name = {s.name: s for s in spans}

    # The trace id derived from the response id hex must surface on
    # every span on the wire; this is what operators paste into a
    # trace backend's lookup UI.
    expected_trace_id = bytes.fromhex(_RESP_HEX)
    for span in spans:
        assert span.trace_id == expected_trace_id, (
            f"span {span.name!r} has trace_id={span.trace_id.hex()!r}, "
            f"expected {_RESP_HEX!r} derived from response id"
        )

    # GenAI semconv attributes (added in PR #1050) survive OTLP encode.
    agent_attrs = _span_attrs(by_name["agent:debby"])
    assert agent_attrs.get("gen_ai.operation.name") == "invoke_agent"
    assert agent_attrs.get("gen_ai.agent.name") == "debby"
    assert agent_attrs.get("gen_ai.provider.name") == "anthropic"
    assert agent_attrs.get("gen_ai.request.model") == "claude-sonnet-4-5"
    # Original omnigent attributes still present so MLflow's UI is
    # unaffected.
    assert agent_attrs.get("agent.name") == "debby"

    llm_attrs = _span_attrs(by_name["llm_call"])
    assert llm_attrs.get("gen_ai.operation.name") == "chat"
    assert llm_attrs.get("gen_ai.provider.name") == "openai"
    assert llm_attrs.get("gen_ai.request.model") == "gpt-5"

    tool_attrs = _span_attrs(by_name["tool:calculator"])
    assert tool_attrs.get("gen_ai.operation.name") == "execute_tool"
    assert tool_attrs.get("tool.name") == "calculator"

    # Parent-child structure preserved over the wire: llm and tool
    # spans both list the agent span's span_id as their parent.
    agent_span_id = by_name["agent:debby"].span_id
    assert agent_span_id, "agent span has empty span_id on the wire"
    assert by_name["llm_call"].parent_span_id == agent_span_id, (
        "llm_call should be parented to agent:debby; "
        f"got parent={by_name['llm_call'].parent_span_id.hex()!r}, "
        f"agent span_id={agent_span_id.hex()!r}"
    )
    assert by_name["tool:calculator"].parent_span_id == agent_span_id, (
        "tool:calculator should be parented to agent:debby; "
        f"got parent={by_name['tool:calculator'].parent_span_id.hex()!r}, "
        f"agent span_id={agent_span_id.hex()!r}"
    )


@pytest.mark.e2e
def test_otlp_receiver_rejects_unknown_path(otlp_receiver: _OTLPReceiver) -> None:
    """
    Sanity check the stub receiver.

    If a future MLflow / OTel SDK upgrade changes the default OTLP
    sub-path off ``/v1/traces``, the main test would still pass with
    zero spans recorded if the receiver silently accepted any path.
    This test pokes the wrong path directly to confirm the receiver
    rejects it, so the path assertion in the main test is meaningful.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{otlp_receiver.port}/wrong-path",
        data=b"\x00",
        headers={"Content-Type": "application/x-protobuf"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=2.0)
    assert excinfo.value.code == 404
