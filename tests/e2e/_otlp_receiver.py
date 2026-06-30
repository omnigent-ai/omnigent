"""
In-process OTLP HTTP receiver for end-to-end tests of the OTel
observability series (PRs #1050, #1070, #1072, #1083).

Spins up a real HTTP server on a random localhost port that accepts
OTLP/HTTP protobuf POSTs (the same wire format any production OTel
collector receives). Tests configure OTEL_EXPORTER_OTLP_ENDPOINT to
point at the receiver, run the production code path that emits spans
or metrics, then assert on the captured payloads.

This is the canonical real-data verification path for omnigent's OTel
work: real OTel SDK serialization, real OTLP/HTTP protocol, real
protobuf encode, real socket I/O. No mocks past the network boundary.
"""

from __future__ import annotations

import gzip
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
)
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2


class _OTLPHandler(BaseHTTPRequestHandler):
    """Minimal OTLP/HTTP receiver: parses POSTed protobufs into captures."""

    server: OTLPReceiver  # populated by HTTPServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)

        if self.path == "/v1/traces":
            req = trace_service_pb2.ExportTraceServiceRequest()
            req.ParseFromString(body)
            self.server.captured_traces.append(req)
            response = trace_service_pb2.ExportTraceServiceResponse()
        elif self.path == "/v1/metrics":
            req = metrics_service_pb2.ExportMetricsServiceRequest()
            req.ParseFromString(body)
            self.server.captured_metrics.append(req)
            response = metrics_service_pb2.ExportMetricsServiceResponse()
        else:
            self.send_response(404)
            self.end_headers()
            return

        # Respect the OTLP/HTTP spec: 200 with a serialized empty
        # ExportXxxResponse. Returning a zero-length body works with
        # most SDKs but stricter clients hang the connection waiting
        # for content-length bytes.
        body_bytes = response.SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def log_message(self, format: str, *args: Any) -> None:
        return  # Silence default access log to keep test output clean.


class OTLPReceiver(HTTPServer):
    """HTTP server that captures OTLP traces and metrics export requests."""

    captured_traces: list[trace_service_pb2.ExportTraceServiceRequest]
    captured_metrics: list[metrics_service_pb2.ExportMetricsServiceRequest]

    def __init__(self) -> None:
        # Port 0 lets the OS pick a free port. server_address[1] reports it
        # back, avoiding the bind-close-rebind TOCTOU race.
        super().__init__(("127.0.0.1", 0), _OTLPHandler)
        self.captured_traces = []
        self.captured_metrics = []

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def all_spans(self) -> list[Any]:
        """Flatten captured trace requests to a list of span protobufs."""
        out: list[Any] = []
        for req in self.captured_traces:
            for rs in req.resource_spans:
                for ss in rs.scope_spans:
                    out.extend(ss.spans)
        return out

    def all_metric_data_points(self, metric_name: str) -> list[Any]:
        """Flatten captured metric requests to data points for a metric name."""
        out: list[Any] = []
        for req in self.captured_metrics:
            for rm in req.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        if m.name != metric_name:
                            continue
                        which = m.WhichOneof("data")
                        if which == "histogram":
                            out.extend(m.histogram.data_points)
                        elif which == "sum":
                            out.extend(m.sum.data_points)
                        elif which == "gauge":
                            out.extend(m.gauge.data_points)
        return out


def attr_value_to_python(av: Any) -> Any:
    """Convert an OTLP AnyValue protobuf to a Python value."""
    which = av.WhichOneof("value")
    if which == "string_value":
        return av.string_value
    if which == "int_value":
        return av.int_value
    if which == "double_value":
        return av.double_value
    if which == "bool_value":
        return av.bool_value
    return None


def attrs_to_dict(kv_list: Any) -> dict[str, Any]:
    """Convert OTLP KeyValue repeated field to a plain dict."""
    return {kv.key: attr_value_to_python(kv.value) for kv in kv_list}


def start_receiver() -> tuple[OTLPReceiver, threading.Thread]:
    """Spawn the receiver in a background thread. Caller is responsible for shutdown."""
    server = OTLPReceiver()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
