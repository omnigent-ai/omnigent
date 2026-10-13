"""Serve authenticated Git smart HTTP through a loopback ``github.com`` TLS proxy.

Set ``https_proxy`` to ``FakeGitHub.proxy_url`` and ``GIT_SSL_NO_VERIFY=1``
for the self-signed certificate. Keeping the clone URL on ``github.com``
exercises Git's actual hostname-scoped credential-helper chain.
"""

from __future__ import annotations

import base64
import contextlib
import os
import socketserver
import ssl
import subprocess
import tempfile
import threading
from http.client import HTTPMessage, parse_headers
from pathlib import Path

_GIT_TIMEOUT_S = 30.0

# Env that isolates seeding-time git calls from ambient user/system config.
_SEED_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def _run_git(*args: str) -> None:
    subprocess.run(
        ["git", *args],
        check=True,
        env=_SEED_GIT_ENV,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
    )


def make_bare_repo(root: Path, org_repo: str) -> Path:
    """Seed ``<root>/<org>/<repo>.git`` with one commit; return the repo path."""
    seed = root / "_seed" / org_repo.replace("/", "__")
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "README.md").write_text(f"{org_repo}: a private repository\n")
    _run_git("init", "-q", "-b", "main", str(seed))
    _run_git("-C", str(seed), "add", "README.md")
    _run_git(
        "-C",
        str(seed),
        "-c",
        "user.email=e2e@example.invalid",
        "-c",
        "user.name=e2e",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    bare = root / f"{org_repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _run_git("clone", "-q", "--bare", str(seed), str(bare))
    return bare


def _make_github_cert(directory: Path) -> tuple[Path, Path]:
    """Generate a throwaway self-signed cert for ``github.com``."""
    key = directory / "key.pem"
    cert = directory / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "2",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=github.com",
            "-addext",
            "subjectAltName=DNS:github.com",
        ],
        check=True,
        capture_output=True,
        timeout=60.0,
    )
    return cert, key


def _read_body(rfile, headers: HTTPMessage) -> bytes:
    """Read a request body (Content-Length or chunked)."""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks = []
        while True:
            size_line = rfile.readline(65536).strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                rfile.readline(65536)  # trailing CRLF
                return b"".join(chunks)
            chunks.append(rfile.read(size))
            rfile.readline(65536)  # chunk-terminating CRLF
    length = int(headers.get("content-length", "0") or "0")
    return rfile.read(length)


class _ProxyHandler(socketserver.BaseRequestHandler):
    """One CONNECT tunnel: TLS-terminate github.com, then serve smart HTTP."""

    def handle(self) -> None:
        server: FakeGitHub = self.server.fake_github  # type: ignore[attr-defined]
        rfile = self.request.makefile("rb")
        request_line = rfile.readline(65536).decode("latin-1").strip()
        parse_headers(rfile)
        parts = request_line.split()
        if len(parts) != 3 or parts[0] != "CONNECT" or parts[1] != "github.com:443":
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            tls = server.ssl_context.wrap_socket(self.request, server_side=True)
        except ssl.SSLError:
            return
        try:
            self._serve_http(tls, server)
        finally:
            with contextlib.suppress(OSError):
                tls.close()

    def _serve_http(self, tls: ssl.SSLSocket, server: FakeGitHub) -> None:
        rfile = tls.makefile("rb")
        request_line = rfile.readline(65536).decode("latin-1").strip()
        if not request_line:
            return
        method, target, _ = request_line.split()
        headers = parse_headers(rfile)
        if headers.get("expect", "").lower() == "100-continue":
            tls.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        body = _read_body(rfile, headers)

        auth = headers.get("authorization", "")
        if auth != f"Basic {server.expected_basic}":
            server.saw_unauthenticated_request = True
            tls.sendall(
                b"HTTP/1.1 401 Unauthorized\r\n"
                b'WWW-Authenticate: Basic realm="GitHub"\r\n'
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return
        server.saw_authenticated_request = True

        path, _, query = target.partition("?")
        cgi_env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_PROJECT_ROOT": str(server.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": method,
            "REMOTE_USER": server.username,
            "REMOTE_ADDR": "127.0.0.1",
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "GATEWAY_INTERFACE": "CGI/1.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
        cgi = subprocess.run(
            ["git", "http-backend"],
            input=body,
            env=cgi_env,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
        raw_headers, _, payload = cgi.stdout.partition(b"\r\n\r\n")
        status = "200 OK"
        forwarded: list[str] = []
        for line in raw_headers.decode("latin-1").splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "status":
                status = value.strip()
            elif name.strip():
                forwarded.append(f"{name.strip()}: {value.strip()}")
        forwarded.append(f"Content-Length: {len(payload)}")
        forwarded.append("Connection: close")
        head = f"HTTP/1.1 {status}\r\n" + "\r\n".join(forwarded) + "\r\n\r\n"
        tls.sendall(head.encode("latin-1") + payload)


class _ThreadingProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeGitHub:
    """Context-managed proxy serving *project_root* with the supplied Git credentials."""

    def __init__(self, project_root: Path, username: str, token: str) -> None:
        self.project_root = project_root
        self.username = username
        self.expected_basic = base64.b64encode(f"{username}:{token}".encode()).decode()
        self.saw_unauthenticated_request = False
        self.saw_authenticated_request = False
        self._certdir = tempfile.TemporaryDirectory(prefix="fake-github-")
        cert, key = _make_github_cert(Path(self._certdir.name))
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Loopback stand-in still refuses the TLS versions real github.com refuses.
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ssl_context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        self.ssl_context.set_alpn_protocols(["http/1.1"])
        self._server = _ThreadingProxy(("127.0.0.1", 0), _ProxyHandler)
        self._server.fake_github = self  # type: ignore[attr-defined]
        self.proxy_port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def __enter__(self) -> FakeGitHub:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._certdir.cleanup()
