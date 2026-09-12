"""E2E repro: a silent HTTPS server is reported as an Omnigent ERROR.

A user runs ``omnigent host --server https://<server>`` against an HTTPS
deployment whose edge accepts every WebSocket tunnel upgrade (a valid
``101``) but whose backend never sends a single frame — the shape a
Databricks Apps ingress produces while the app behind it is hung or
restarting. The host reconnect-loops (each attempt even prints
``✓ Connected`` before the tunnel dies unanswered), and after
``_SILENT_CONNECT_ESCALATE_ATTEMPTS`` consecutive accepted-but-silent
connections it emits, at **ERROR** level::

    The server at https://... accepted N consecutive connections but never
    responded on any of them. Treating the endpoint as unhealthy;
    reconnecting on slow backoff until it responds.

The detection itself is wanted (operator ⚠ notice on stderr, slow-backoff
retry, automatic recovery once the server speaks). The defect this test
pins is the *attribution*: the condition is the upstream endpoint failing
to answer, yet it lands in the omnigent debug log as an unstructured
ERROR-level record from ``omnigent.host.connect`` — the exact record the
error-KPI pipeline counts as an Omnigent defect. The identical message for
an ``http://`` (local) server is already triaged as expected; the
``https://`` variant is the same host-side code path reporting a
server-side outage as its own error.

This test drives the real user journey: it stands up a TLS endpoint that
accepts upgrades and stays silent, points a real ``omnigent host`` process
at it under a non-loopback https hostname (a DNS shim maps the hostname to
loopback, so the reconnect loop classifies the endpoint as a remote deploy
exactly like the field), lets the silent-connect streak cross the
escalation threshold, and then asserts the host survived AND that the
upstream condition was not recorded as an ERROR-level omnigent log record.

Run with::

    .venv/bin/python -m pytest tests/e2e/test_host_silent_https_endpoint.py -v --timeout=240
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import os
import re
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A deliberately non-loopback hostname: the reconnect loop's disconnect
# classification distinguishes loopback from remote (Apps-ingress-fronted)
# servers, and the reported failure is against a remote https deployment.
# A getaddrinfo shim maps it to 127.0.0.1; it never touches real DNS.
_FAKE_HOST = "app-omni-silent-e2e.test"

# Mirrors _SILENT_CONNECT_ESCALATE_ATTEMPTS in omnigent/host/connect.py:
# consecutive accepted-but-silent connections before the host declares the
# endpoint unhealthy. The journey gate below waits for a couple more than
# this so the escalation moment has definitely passed, whatever form the
# build under test reports it in.
_SILENT_CONNECT_ESCALATE_ATTEMPTS = 10
_JOURNEY_ATTEMPTS = _SILENT_CONNECT_ESCALATE_ATTEMPTS + 2

# The stable phrase of the silent-endpoint escalation message.
_ESCALATION_PHRASE = "consecutive connections but never responded"

# Process-log lines start with the level name (see DEFAULT_LOG_PREFIX_FORMAT
# in omnigent/process_logging.py).
_ERROR_RECORD_RE = re.compile(r"^ERROR\b.*" + re.escape(_ESCALATION_PHRASE))

_COLLECT_DEADLINE_S = 150.0

_WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_SITECUSTOMIZE = f'''\
"""Resolve the test's fake remote hostname to loopback (test shim)."""
import socket

_real_getaddrinfo = socket.getaddrinfo


def _loopback_getaddrinfo(host, *args, **kwargs):
    if host == {_FAKE_HOST!r}:
        return _real_getaddrinfo("127.0.0.1", *args, **kwargs)
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _loopback_getaddrinfo
'''


def _write_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Write a self-signed TLS cert/key pair for the fake hostname.

    The host process trusts it via ``SSL_CERT_FILE`` (honored by
    ``omnigent.util.tls.client_ssl_context``), so the ``wss://`` handshake
    verifies exactly as it would against a real CA-signed endpoint.

    :param cert_path: Where to write the PEM certificate.
    :param key_path: Where to write the PEM private key.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _FAKE_HOST)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(_FAKE_HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class _SilentHttpsEndpoint:
    """An HTTPS server that accepts WS upgrades but never responds on them.

    Every WebSocket upgrade request receives a valid ``101 Switching
    Protocols`` (correct ``Sec-WebSocket-Accept``), then the socket is held
    briefly and dropped without one frame — the client sees an accepted
    connection that dies with "no close frame received or sent". Plain HTTP
    requests (the host's best-effort ``GET /v1/me`` probes) get a bare 503.
    """

    def __init__(self, certfile: str, keyfile: str) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        self._ctx = ctx
        self._lock = threading.Lock()
        self._accepted_upgrades = 0
        endpoint = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                endpoint._handle(self.request)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def accepted_upgrades(self) -> int:
        with self._lock:
            return self._accepted_upgrades

    def _handle(self, raw_sock: socket.socket) -> None:
        try:
            raw_sock.settimeout(10.0)
            tls = self._ctx.wrap_socket(raw_sock, server_side=True)
        except (OSError, ssl.SSLError):
            return
        try:
            tls.settimeout(10.0)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = tls.recv(4096)
                if not chunk:
                    return
                head += chunk
            text = head.decode("latin-1", errors="replace")
            key_match = re.search(r"(?im)^sec-websocket-key:\s*(\S+)\s*$", text)
            if key_match and re.search(r"(?im)^upgrade:\s*websocket\s*$", text):
                accept = base64.b64encode(
                    hashlib.sha1((key_match.group(1) + _WS_MAGIC_GUID).encode()).digest()
                ).decode()
                tls.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n"
                        "\r\n"
                    ).encode()
                )
                with self._lock:
                    self._accepted_upgrades += 1
                # Give the client time to finish its handshake and enter its
                # receive loop, then drop without ever sending a frame.
                time.sleep(0.4)
            else:
                tls.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"content-length: 0\r\nconnection: close\r\n\r\n"
                )
        except (OSError, ssl.SSLError):
            # The host tears connections down at its own pace; either peer
            # racing a close here is part of the journey, not a failure.
            pass
        finally:
            with contextlib.suppress(OSError):
                tls.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _read(path: Path) -> str:
    """Return *path*'s text, or empty when it does not exist yet."""
    return path.read_text(errors="replace") if path.exists() else ""


@pytest.mark.timeout(240)
def test_host_silent_https_endpoint_not_logged_as_omnigent_error(tmp_path: Path) -> None:
    """A silent https endpoint must not be recorded as an Omnigent ERROR.

    Drives the reported journey to its failure state — the host has made
    well past the escalation threshold of consecutive accepted-but-silent
    connections against a remote-classified https endpoint — then asserts:

    1. the daemon is still retrying (exiting would strand every runner);
    2. the upstream endpoint's silence is not recorded in the omnigent
       debug log as an unstructured ERROR-level ``never responded`` record
       (the error-KPI pipeline ingests ERROR records as Omnigent defects;
       an upstream/server condition must carry a non-ERROR level or a
       structured upstream attribution instead). The operator-facing ⚠
       stderr notice and the slow-backoff retry cadence are wanted and are
       untouched by this assertion.
    """
    from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    _write_self_signed_cert(cert_path, key_path)

    inject_dir = tmp_path / "inject"
    inject_dir.mkdir()
    (inject_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE)

    endpoint = _SilentHttpsEndpoint(str(cert_path), str(key_path))
    server_url = f"https://{_FAKE_HOST}:{endpoint.port}"

    home = tmp_path / "home"
    config_home = tmp_path / "config"
    data_dir = tmp_path / "data"
    for directory in (home, config_home, data_dir):
        directory.mkdir()
    host_log = tmp_path / "host-daemon.log"
    console_log = tmp_path / "host-console.log"

    # Isolate the child from this process's own omnigent/Databricks context
    # and from proxies (a proxy would terminate the TLS connection itself,
    # bypassing the silent endpoint the journey needs).
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_", "DATABRICKS_"))
        and key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "wss_proxy", "ws_proxy")
    }
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(data_dir),
            PROCESS_LOG_FILE_ENV_VAR: str(host_log),
            "SSL_CERT_FILE": str(cert_path),
            # The shim must load in the child, and the child must import
            # this worktree's omnigent (plus its path-provided sdks).
            "PYTHONPATH": os.pathsep.join(
                [
                    str(inject_dir),
                    str(_REPO_ROOT),
                    str(_REPO_ROOT / "sdks" / "ui"),
                    str(_REPO_ROOT / "sdks" / "python-client"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ).rstrip(os.pathsep),
        }
    )

    with open(console_log, "wb") as console_fh:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "host",
                "--server",
                server_url,
                "--non-interactive",
            ],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=console_fh,
            stderr=subprocess.STDOUT,
        )
    try:
        # Journey gate: the failure state is reached once the endpoint has
        # accepted (and left unanswered) comfortably more consecutive
        # connections than the escalation threshold.
        deadline = time.monotonic() + _COLLECT_DEADLINE_S
        while time.monotonic() < deadline:
            if proc.poll() is not None or endpoint.accepted_upgrades >= _JOURNEY_ATTEMPTS:
                break
            time.sleep(0.5)

        diagnostics = (
            f"accepted upgrades: {endpoint.accepted_upgrades}\n"
            f"--- console ---\n{_read(console_log)[-4000:]}\n"
            f"--- daemon log tail ---\n{_read(host_log)[-4000:]}"
        )
        assert endpoint.accepted_upgrades >= _JOURNEY_ATTEMPTS, (
            f"journey did not reach the reported failure state: the endpoint "
            f"accepted only {endpoint.accepted_upgrades} tunnel connections within "
            f"{_COLLECT_DEADLINE_S:.0f}s (needed {_JOURNEY_ATTEMPTS} consecutive "
            f"accepted-but-silent connections)\n{diagnostics}"
        )

        # The daemon must still be retrying: the endpoint may heal at any
        # moment, and exiting would strand the host's sessions.
        assert proc.poll() is None, (
            f"host daemon exited (code {proc.returncode}) instead of retrying "
            f"on backoff\n{diagnostics}"
        )

        # The bug: the upstream endpoint's silence lands in the omnigent
        # debug log as an unstructured ERROR record from
        # omnigent.host.connect ("... accepted N consecutive connections but
        # never responded on any of them."), which the error KPI attributes
        # to Omnigent. Detection/operator messaging may (and should) remain;
        # this record must not be a bare ERROR.
        error_records = [
            line for line in _read(host_log).splitlines() if _ERROR_RECORD_RE.search(line)
        ]
        assert not error_records, (
            "the silent https endpoint (an upstream/server condition) was "
            "recorded as an ERROR-level omnigent log record:\n  "
            + "\n  ".join(error_records)
            + f"\n{diagnostics}"
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        endpoint.close()
