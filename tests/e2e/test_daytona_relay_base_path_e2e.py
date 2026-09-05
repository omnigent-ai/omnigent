"""Daytona egress relay must keep the base path of a prefixed ``UPSTREAM_URL``.

The Cloudflare Worker in ``deploy/daytona`` reverse-proxies every request a
Daytona sandbox makes (host dial-back HTTP + tunnel upgrades) to the real
Omnigent server. Deployments that mount the server under a URL prefix
(e.g. ``https://example.com/omnigent``) deploy the relay with
``UPSTREAM_URL=https://example.com/omnigent``. A transparent relay must
forward an incoming path *under* that prefix (relay ``/health`` -> upstream
``/omnigent/health``); dropping the prefix 404s every dial-back request, so a
managed host can never come online.

Runs the real Worker module under Node's web-standard runtime — the same
``fetch(request, env)`` entrypoint Cloudflare invokes — in front of a local
upstream that records the paths it receives.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = REPO_ROOT / "deploy" / "daytona" / "src" / "index.js"

BASE_PATH = "/omnigent"

# Minimal Node host for the Worker module: adapts each incoming HTTP request
# to a web-standard Request and serves worker.fetch(request, env) — the same
# invocation Cloudflare's runtime performs. All proxying/URL logic under test
# stays in the product module.
_NODE_HOST = """
import http from "node:http";
import { pathToFileURL } from "node:url";

const [workerPath, portArg, upstreamUrl] = process.argv.slice(2);
const worker = (await import(pathToFileURL(workerPath).href)).default;
const port = Number(portArg);
const env = { UPSTREAM_URL: upstreamUrl };

const server = http.createServer(async (req, res) => {
  try {
    const hasBody = !["GET", "HEAD"].includes(req.method);
    const request = new Request(`http://127.0.0.1:${port}${req.url}`, {
      method: req.method,
      headers: req.headers,
      body: hasBody ? req : undefined,
      duplex: hasBody ? "half" : undefined,
    });
    const response = await worker.fetch(request, env);
    const headers = {};
    for (const [k, v] of response.headers) {
      if (!["content-encoding", "transfer-encoding", "content-length"].includes(k)) {
        headers[k] = v;
      }
    }
    const buf = Buffer.from(await response.arrayBuffer());
    headers["content-length"] = String(buf.length);
    res.writeHead(response.status, headers);
    res.end(buf);
  } catch (err) {
    res.writeHead(502, { "content-type": "text/plain" });
    res.end(`relay host error: ${err}`);
  }
});
server.listen(port, "127.0.0.1", () => console.log("relay listening"));
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port: int, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            if time.monotonic() > deadline:
                raise AssertionError(f"port {port} did not come up within {timeout_s}s") from None
            time.sleep(0.1)


class _PrefixMountedUpstream:
    """Upstream standing in for an Omnigent server mounted under BASE_PATH.

    Answers 200 for any path under the prefix and 404 otherwise, recording
    every request target it receives so the test can see exactly which path
    the relay dialed.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                outer.requests.append(self.path)
                path_only = urlsplit(self.path).path
                ok = path_only == BASE_PATH or path_only.startswith(BASE_PATH + "/")
                body = json.dumps(
                    {"status": "ok" if ok else "not found", "path": self.path}
                ).encode()
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay_with_prefixed_upstream(tmp_path: Path) -> Iterator[tuple[str, _PrefixMountedUpstream]]:
    """The real relay Worker serving locally, UPSTREAM_URL carrying a base path."""
    if shutil.which("node") is None:
        pytest.skip("node is required to run the Daytona relay Worker")
    upstream = _PrefixMountedUpstream()
    relay_port = _free_port()
    host_js = tmp_path / "relay_host.mjs"
    host_js.write_text(_NODE_HOST)
    proc = subprocess.Popen(
        [
            "node",
            str(host_js),
            str(WORKER_PATH),
            str(relay_port),
            f"http://127.0.0.1:{upstream.port}{BASE_PATH}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(relay_port)
        yield f"http://127.0.0.1:{relay_port}", upstream
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.close()


def test_relay_preserves_upstream_base_path(
    relay_with_prefixed_upstream: tuple[str, _PrefixMountedUpstream],
) -> None:
    """Requests through the relay must land under the configured base path.

    With ``UPSTREAM_URL=http://…/omnigent``, the sandbox host's dial-back to
    the relay's ``/health`` (and every tunnel/API path) must reach the
    deployment as ``/omnigent/<path>`` — today the prefix is dropped, the
    upstream 404s every request, and the managed host never comes online.
    """
    relay_url, upstream = relay_with_prefixed_upstream
    with httpx.Client(trust_env=False, timeout=30) as client:
        health = client.get(f"{relay_url}/health")
        tunnel = client.get(f"{relay_url}/v1/hosts/h-123/tunnel", params={"token": "t-1"})

    assert upstream.requests, "the relay never dialed the upstream"
    health_target, tunnel_target = upstream.requests[0], upstream.requests[1]

    assert urlsplit(health_target).path == f"{BASE_PATH}/health", (
        f"relay dropped the UPSTREAM_URL base path: /health was forwarded to "
        f"{health_target!r} instead of {BASE_PATH}/health"
    )
    assert health.status_code == 200, (
        f"GET <relay>/health returned {health.status_code} — the prefix-mounted "
        f"upstream rejected the forwarded path {health_target!r}"
    )

    tunnel_split = urlsplit(tunnel_target)
    assert tunnel_split.path == f"{BASE_PATH}/v1/hosts/h-123/tunnel", (
        f"relay dropped the UPSTREAM_URL base path: the host tunnel dial-back was "
        f"forwarded to {tunnel_target!r} instead of {BASE_PATH}/v1/hosts/h-123/tunnel"
    )
    assert tunnel_split.query == "token=t-1", (
        f"relay must preserve the query string; upstream saw {tunnel_target!r}"
    )
    assert tunnel.status_code == 200, (
        f"GET <relay>/v1/hosts/h-123/tunnel returned {tunnel.status_code} — every "
        f"dial-back 404s, so a managed host can never come online"
    )


class _RecordingUpstream:
    """Upstream that records every request target and answers 200."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                outer.requests.append(self.path)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextlib.contextmanager
def _running_relay(tmp_path: Path, upstream_base: str) -> Iterator[tuple[str, _RecordingUpstream]]:
    """The real Worker serving locally against a recording upstream."""
    if shutil.which("node") is None:
        pytest.skip("node is required to run the Daytona relay Worker")
    upstream = _RecordingUpstream()
    relay_port = _free_port()
    host_js = tmp_path / "relay_host.mjs"
    host_js.write_text(_NODE_HOST)
    proc = subprocess.Popen(
        [
            "node",
            str(host_js),
            str(WORKER_PATH),
            str(relay_port),
            f"http://127.0.0.1:{upstream.port}{upstream_base}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port(relay_port)
        yield f"http://127.0.0.1:{relay_port}", upstream
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.close()


@pytest.mark.parametrize(
    ("upstream_base", "incoming", "expected_target"),
    [
        # No base path: the mapping must stay exactly as before (no double slash).
        ("", "/health", "/health"),
        # Trailing slash on the base path must not produce a double slash.
        ("/omnigent/", "/health", "/omnigent/health"),
        # A root request lands under the prefix, not at the deployment root.
        ("/omnigent", "/", "/omnigent/"),
        # Multi-segment base path, deep path, and query all survive the hop.
        ("/a/b", "/v1/hosts/h-1/tunnel?token=t-1", "/a/b/v1/hosts/h-1/tunnel?token=t-1"),
    ],
)
def test_relay_path_mapping_across_upstream_url_shapes(
    tmp_path: Path, upstream_base: str, incoming: str, expected_target: str
) -> None:
    """Every UPSTREAM_URL shape maps the incoming path under its base path."""
    with _running_relay(tmp_path, upstream_base) as (relay_url, upstream):
        with httpx.Client(trust_env=False, timeout=30) as client:
            response = client.get(f"{relay_url}{incoming}")

    assert response.status_code == 200, f"relay returned {response.status_code} for {incoming!r}"
    assert upstream.requests == [expected_target], (
        f"relay with UPSTREAM_URL base {upstream_base!r} forwarded {incoming!r} to "
        f"{upstream.requests!r} instead of [{expected_target!r}]"
    )
