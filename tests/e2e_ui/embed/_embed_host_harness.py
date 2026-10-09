"""Shared helpers: an embed-host origin proxying two real Omnigent Servers.

The embedded web UI is mounted by a host that owns transport and auth: its
``fetcher`` maps ``/v1/...`` onto the host's API surface, often proxying several
Servers behind one origin (see ``web/src/lib/host.ts``). One loopback origin here
serves the built host page (``web/dist-e2e-embed-host``) with an SPA fallback and
streams ``/omnigent/<key>/<path>`` to real Omnigent servers. Identity is header
auth: the host page asserts ``X-Forwarded-Email`` per Server, so the servers run
multi-user with a per-server admin roster.
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
)

# Hop-by-hop headers (RFC 9110 §7.6.1) are never forwarded in either direction.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM with a short grace period, escalating to SIGKILL."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@contextlib.contextmanager
def spawn_header_auth_server(
    mock_llm_server_url: str,
    server_tmp: Path,
    *,
    name: str,
    admins: list[str],
) -> Iterator[str]:
    """Spawn one multi-user header-auth Omnigent server (no runner); yield its base URL.

    :param mock_llm_server_url: Session-scoped mock LLM base (no real creds).
    :param server_tmp: Per-fixture temp dir; one nested dir per *name*.
    :param name: Short label (``"a"`` / ``"b"``) for the nested tmp dir.
    :param admins: Identities for ``OMNIGENT_ADMIN_LIST_PATH`` (``is_admin`` on
        ``/v1/me``). May be empty.
    :yields: The server's loopback base URL (``http://127.0.0.1:<port>``).
    """
    tmp = server_tmp / f"server_{name}"
    tmp.mkdir(parents=True, exist_ok=True)
    port = _find_free_port()
    log_path = tmp / "server.log"
    db_path = tmp / "test.db"
    artifact_dir = tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)
    admins_path = tmp / "admins"
    admins_path.write_text("".join(f"{email}\n" for email in admins))

    base_url = f"http://127.0.0.1:{port}"
    pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
    server_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        # Multi-user: clear the single-user marker the suite sets, so the
        # X-Forwarded-Email header is the request identity.
        "OMNIGENT_LOCAL_SINGLE_USER": "",
        "OMNIGENT_ADMIN_LIST_PATH": str(admins_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
    }

    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen; closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
            "--agent",
            str(agent_yaml_path),
        ],
        env=server_env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    ready = True
                    break
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        if not ready:
            log_handle.flush()
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"embed-host server {name!r} not healthy within "
                f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
                f"{log_text[-3000:]}"
            )
        yield base_url
    finally:
        _terminate(proc)
        log_handle.close()


class _EmbedHostRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Static host page + streaming reverse proxy for the embed's backends.

    ``/omnigent/<key>/<path>`` forwards to ``upstreams[<key>]``; other paths are
    served from the harness dir, extensionless GETs falling back to the host page.
    """

    # Close-delimited responses keep the streaming copy loop trivial.
    protocol_version = "HTTP/1.0"
    upstreams: dict[str, str] = {}
    host_page: str = "/index.html"

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging."""

    def _upstream_for(self, path: str) -> tuple[str, str] | None:
        for key, base in self.upstreams.items():
            prefix = f"/omnigent/{key}"
            if path == prefix or path.startswith(f"{prefix}/"):
                return base, path[len(prefix) :] or "/"
        return None

    def _serve(self) -> None:
        target = self._upstream_for(self.path)
        if target is not None:
            self._proxy(*target)
        elif self.command in {"GET", "HEAD"}:
            last_segment = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            if "." not in last_segment:
                self.path = self.host_page
            if self.command == "GET":
                super().do_GET()
            else:
                super().do_HEAD()
        else:
            self.send_error(405)

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_PUT(self) -> None:
        self._serve()

    def do_PATCH(self) -> None:
        self._serve()

    def do_DELETE(self) -> None:
        self._serve()

    def _proxy(self, base_url: str, upstream_path: str) -> None:
        """Forward this request to *base_url* and stream the response back."""
        parsed = urlsplit(base_url)
        assert parsed.hostname is not None and parsed.port is not None
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        # Drop Host (the upstream's own) and Accept-Encoding (identity bodies
        # keep the copy loop transparent).
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() not in {"host", "accept-encoding"}
        }
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=120)
        try:
            try:
                conn.request(self.command, upstream_path, body=body, headers=headers)
                resp = conn.getresponse()
            except OSError as exc:
                self.send_error(502, f"upstream unreachable: {exc}")
                return
            try:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() in _HOP_BY_HOP:
                        continue
                    self.send_header(k, v)
                self.end_headers()
                # read1 returns as soon as bytes arrive, so SSE is forwarded
                # event by event.
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Browser closed the stream (navigation / context close).
                pass
        finally:
            conn.close()


@contextlib.contextmanager
def embed_host_proxy(dist_dir: Path, host_page: str, upstreams: dict[str, str]) -> Iterator[str]:
    """Serve the built host page + per-Server proxy; yield the origin URL.

    :param dist_dir: The built harness (``web/dist-e2e-embed-host``).
    :param host_page: Host HTML path inside *dist_dir*, served for every route.
    :param upstreams: Server key → base URL, reachable at ``/omnigent/<key>/<path>``.
    :yields: ``http://127.0.0.1:<port>`` serving the host page.
    """
    handler_cls = type(
        "BoundEmbedHostRequestHandler",
        (_EmbedHostRequestHandler,),
        {"upstreams": dict(upstreams), "host_page": host_page},
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(handler_cls, directory=str(dist_dir)),
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
