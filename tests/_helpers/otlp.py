"""In-test OTLP/HTTP trace sink for scenarios that read a real process's spans."""

from __future__ import annotations

import gzip
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

import httpx

from tests._helpers.live_server import find_free_port
from tests._helpers.polling import poll_until


class OtlpTraceCollector:
    """
    Record the spans a real OTLP/HTTP exporter posts to :attr:`endpoint`.

    Point ``OTEL_EXPORTER_OTLP_ENDPOINT`` of the process under test at
    :attr:`endpoint` with ``OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf``. Each
    recorded span is a dict with ``service``, ``name``, ``trace_id`` (hex) and
    ``attributes`` (span attribute values as strings).
    """

    def __init__(self) -> None:
        self._spans: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._port = find_free_port()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # http.server API name
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                if self.path == "/v1/traces":
                    collector._record(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        """Base URL for ``OTEL_EXPORTER_OTLP_ENDPOINT``, e.g. ``"http://127.0.0.1:4318"``."""
        return f"http://127.0.0.1:{self._port}"

    def exporter_env(self) -> dict[str, str]:
        """Environment for an Omnigent process that exports its traces here, per request."""
        return {
            "OMNIGENT_TELEMETRY_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.endpoint,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOGS_EXPORTER": "none",
            # Export each request's spans promptly instead of on the default 5s timer.
            "OTEL_BSP_SCHEDULE_DELAY": "200",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

    def traced_request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        route_span: str,
        **request_options: Any,
    ) -> tuple[httpx.Response, list[dict[str, Any]]]:
        """
        Send one request in a fresh trace; return it and that trace's spans.

        The server's route span ends after the work it waited on, so once it
        is exported the request's other spans are too.

        :param client: Client for the process exporting to this collector.
        :param method: HTTP method, e.g. ``"GET"``.
        :param url: Request URL, e.g. ``"/v1/sessions/abc"``.
        :param route_span: The route span's name, e.g.
            ``"GET /v1/sessions/{session_id}"``.
        :param request_options: Passed to :meth:`httpx.Client.request`.
        """
        trace_id = secrets.token_hex(16)
        headers = {
            **request_options.pop("headers", {}),
            "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01",
        }
        response = client.request(method, url, headers=headers, **request_options)
        poll_until(
            lambda: any(span["name"] == route_span for span in self.snapshot(trace_id)),
            timeout_s=30,
            interval_s=0.2,
        )
        return response, self.snapshot(trace_id)

    def _record(self, body: bytes) -> None:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        rows: list[dict[str, Any]] = []
        for resource_spans in request.resource_spans:
            service = next(
                (
                    attr.value.string_value
                    for attr in resource_spans.resource.attributes
                    if attr.key == "service.name"
                ),
                "",
            )
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    attributes = {}
                    for attr in span.attributes:
                        kind = attr.value.WhichOneof("value")
                        attributes[attr.key] = str(getattr(attr.value, kind)) if kind else ""
                    rows.append(
                        {
                            "service": service,
                            "name": span.name,
                            "trace_id": span.trace_id.hex(),
                            "attributes": attributes,
                        }
                    )
        with self._lock:
            self._spans.extend(rows)

    def snapshot(self, trace_id: str | None = None) -> list[dict[str, Any]]:
        """
        Return the spans recorded so far.

        :param trace_id: Only spans of this trace (hex), e.g. the id a caller
            sent in its ``traceparent`` header. ``None`` returns every span.
        """
        with self._lock:
            return [span for span in self._spans if trace_id in (None, span["trace_id"])]

    def close(self) -> None:
        """Stop the sink. Spans recorded so far stay readable."""
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> OtlpTraceCollector:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
