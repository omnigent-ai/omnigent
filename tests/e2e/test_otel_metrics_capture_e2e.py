"""
End-to-end regression: exported OTel request metrics must not miss
traffic (the metrics backend undercounts real API calls).

Two loss mechanisms combine so the metrics backend undercounts real
traffic around a routine graceful server shutdown (hosted deployments
recycle app processes regularly, so this is steady-state loss, not an
edge case):

1. The HTTP request counters (``omnigent.server.http.requests.started``
   / ``.completed``) are bridged into OpenTelemetry only by the
   10-second periodic publisher task
   (:func:`omnigent.server.performance_metrics.publish_server_metrics_periodically`);
   lifespan shutdown cancels it without a final publish.
2. No final OTLP metrics flush is delivered on SIGTERM: everything
   recorded since the last exporter tick — including the inline
   per-request ``request.duration`` histogram — never reaches the
   collector (a bare OTel SDK process *does* flush at exit, so this is
   omnigent's shutdown path, not the SDK).

This test drives the real operator journey:

1. Start a REAL ``omnigent server`` subprocess with OTel metrics export
   configured (``OTEL_EXPORTER_OTLP_ENDPOINT`` pointing at an OTLP
   collector — here a local sink standing in for the M3 pipeline).
2. Make real API calls and wait until the exported counters capture
   them (proves the export pipeline is healthy end to end).
3. Make more real API calls, then gracefully stop the server (SIGTERM —
   the routine app-recycle path on hosted deployments).
4. Read the final values the collector received: every API call the
   server handled must appear in the exported request metrics.

Today step 4 fails: the calls from step 3 are missing from the exported
counters AND the duration histogram — observed live, the collector's
final values stayed frozen at the last pre-shutdown export (9 of 19
delivered requests), losing 100% of the tail window.
"""

from __future__ import annotations

import gzip
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from tests._helpers.compat import apply_server_env

_REPO_ROOT = Path(__file__).resolve().parents[2]

_STARTED = "omnigent.server.http.requests.started"
_COMPLETED = "omnigent.server.http.requests.completed"
_DURATION = "omnigent.server.http.request.duration"

# The lifespan publisher bridges counters every 10s; give one tick plus
# exporter latency plenty of headroom before declaring the pipeline dead.
_PIPELINE_WARMUP_TIMEOUT_S = 45.0
_SERVER_START_TIMEOUT_S = 60.0
_SHUTDOWN_TIMEOUT_S = 45.0
# Grace after server exit for any final OTLP flush to land at the sink.
# (A correct shutdown flushes before process exit; this absorbs sink
# thread scheduling so a delivered flush is never missed.)
_FINAL_FLUSH_GRACE_S = 3.0


class _OtlpMetricsSink:
    """Minimal OTLP/HTTP collector standing in for the M3 pipeline.

    Accepts ``POST /v1/metrics`` protobuf payloads and remembers the
    latest cumulative value per interesting metric, exactly like a
    cumulative-temporality metrics backend (M3/Prometheus) would.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, float] = {}
        self._exports = 0

        sink = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # http.server handler API name
                length = int(self.headers.get("content-length", "0") or "0")
                body = self.rfile.read(length)
                if self.headers.get("content-encoding") == "gzip":
                    body = gzip.decompress(body)
                if self.path == "/v1/metrics":
                    try:
                        request = ExportMetricsServiceRequest()
                        request.ParseFromString(body)
                        sink._record(request)
                    except Exception as exc:  # sink must keep serving
                        print(f"otlp sink parse error: {exc}", file=sys.stderr)
                self.send_response(200)
                self.send_header("content-type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass  # keep pytest output readable

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _record(self, request: ExportMetricsServiceRequest) -> None:
        with self._lock:
            self._exports += 1
            for resource_metrics in request.resource_metrics:
                for scope_metrics in resource_metrics.scope_metrics:
                    for metric in scope_metrics.metrics:
                        if metric.name in (_STARTED, _COMPLETED):
                            for point in metric.sum.data_points:
                                self._latest[metric.name] = point.as_int or point.as_double
                        elif metric.name == _DURATION:
                            self._latest[metric.name] = sum(
                                point.count for point in metric.histogram.data_points
                            )

    def latest(self) -> dict[str, float]:
        with self._lock:
            return dict(self._latest)

    def export_count(self) -> int:
        with self._lock:
            return self._exports

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server_with_otlp_export(
    tmp_path: Path, sink_port: int, server_port: int
) -> tuple[subprocess.Popen[bytes], Path]:
    """Spawn a real ``omnigent server`` exporting OTel metrics to the sink."""
    db_path = tmp_path / "ap.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_path / "server.log"

    env = {
        **os.environ,
        # Operator telemetry opt-in + OTLP export target — the same knobs a
        # hosted deployment sets to feed the M3/Grafana pipeline.
        "OMNIGENT_TELEMETRY_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{sink_port}",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_METRICS_EXPORTER": "otlp",
        # Export frequently so the test observes counter state promptly. The
        # lifespan publisher's 10s bridge interval is what's under test and is
        # not configurable.
        "OTEL_METRIC_EXPORT_INTERVAL": "500",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_TRACES_EXPORTER": "none",
        # Keep the FastAPI auto-instrumentation's own HTTP metrics out of the
        # stream: this test is about omnigent's server performance counters.
        "OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION": "false",
        # The exporter must reach the loopback collector directly even when
        # the environment forces an egress HTTP proxy (CI sandboxes do).
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    apply_server_env(env, _REPO_ROOT)
    log_handle = open(log_path, "w")  # noqa: SIM115 — handle lives for Popen lifetime
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(server_port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return proc, log_path


@pytest.mark.timeout(180)
def test_exported_request_counters_capture_all_traffic(tmp_path: Path) -> None:
    """Every API call the server handles must reach the metrics backend.

    Guards against tail-traffic loss: API calls made after the last
    periodic metrics-publisher tick were dropped from the exported
    ``requests.started`` / ``requests.completed`` counters when the
    server shut down, so the metrics backend never saw them.
    """
    sink = _OtlpMetricsSink()
    server_port = _find_free_port()
    proc, log_path = _start_server_with_otlp_export(tmp_path, sink.port, server_port)
    base_url = f"http://127.0.0.1:{server_port}"

    delivered = 0  # API calls that reached the app (an HTTP response came back)
    # trust_env=False: never route loopback API calls through an ambient
    # egress proxy (CI sandboxes set HTTP_PROXY); a proxy-generated 502
    # would otherwise be miscounted as a delivered request.
    client = httpx.Client(trust_env=False)
    try:
        # ── 1. server up ────────────────────────────────────────────────
        deadline = time.monotonic() + _SERVER_START_TIMEOUT_S
        healthy = False
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/health", timeout=2)
                delivered += 1
                if response.status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        if not healthy:
            proc.kill()
            log_tail = log_path.read_text()[-3000:] if log_path.exists() else ""
            pytest.fail(f"server never became healthy; log tail:\n{log_tail}")

        # ── 2. traffic the pipeline provably captures ──────────────────
        # Real API calls the SPA / SDK makes, then wait for the periodic
        # publisher (10s tick) to bridge them into the OTLP counters and
        # for the exporter to ship them to the collector.
        for _ in range(4):
            assert client.get(f"{base_url}/health", timeout=5).status_code == 200
            delivered += 1
            assert client.get(f"{base_url}/v1/sessions", timeout=5).status_code == 200
            delivered += 1
        warmup_total = delivered

        warmup_deadline = time.monotonic() + _PIPELINE_WARMUP_TIMEOUT_S
        while time.monotonic() < warmup_deadline:
            if sink.latest().get(_STARTED, 0) >= warmup_total:
                break
            time.sleep(0.1)
        warmup_exported = sink.latest()
        assert warmup_exported.get(_STARTED, 0) >= warmup_total, (
            "export pipeline never became healthy: after "
            f"{_PIPELINE_WARMUP_TIMEOUT_S}s the collector saw "
            f"{warmup_exported.get(_STARTED, 0)} started requests of "
            f"{warmup_total} delivered ({sink.export_count()} exports received). "
            f"Exported values: {json.dumps(warmup_exported)}"
        )

        # ── 3. the tail: more API calls, then a routine graceful stop ──
        # These land after a publisher tick; the shutdown must not lose
        # them. (Hosted deployments recycle app processes routinely, so
        # "traffic since the last tick" is steady-state real traffic.)
        for _ in range(5):
            assert client.get(f"{base_url}/health", timeout=5).status_code == 200
            delivered += 1
            assert client.get(f"{base_url}/v1/sessions", timeout=5).status_code == 200
            delivered += 1
        total = delivered

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=_SHUTDOWN_TIMEOUT_S)
    finally:
        client.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    # ── 4. what the metrics backend ended up with ──────────────────────
    time.sleep(_FINAL_FLUSH_GRACE_S)
    final = sink.latest()
    sink.close()

    started = final.get(_STARTED, 0)
    completed = final.get(_COMPLETED, 0)
    duration_count = final.get(_DURATION, 0)

    # Every API call the server actually handled must reach the metrics
    # backend. Without a final publish + flush on graceful shutdown, the
    # requests handled in the tail window — after the last publisher /
    # exporter tick and before a routine SIGTERM — are silently dropped.
    assert started >= total, (
        f"exported requests.started counter lost traffic: collector saw "
        f"{started} of {total} API calls actually handled. Requests handled "
        f"after the last periodic publisher tick were never exported "
        f"(no final metrics publish/flush on graceful shutdown). "
        f"Final exported values: {json.dumps(final)}"
    )
    assert completed >= total, (
        f"exported requests.completed counter lost traffic: collector saw "
        f"{completed} of {total} API calls actually handled. "
        f"Final exported values: {json.dumps(final)}"
    )
    assert duration_count >= total, (
        f"exported request.duration histogram lost traffic: collector "
        f"counted {duration_count} of {total} API calls actually handled — "
        f"no final OTLP metrics flush is delivered on graceful shutdown. "
        f"Final exported values: {json.dumps(final)}"
    )
