"""Local OAuth workspace and authenticated edge for the host onboarding journey."""

from __future__ import annotations

import json
import select
import socket
import ssl
import subprocess
import threading
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

USER_EMAIL = "onboarding@example.test"
WORKSPACE_ID = "1234567890123456"
ACCESS_TOKEN = "local-onboarding-access-token"
MOUNT = "/api/2.0/omnigent"


class HostLoginWorkspace(AbstractContextManager):
    """Serve mock OAuth over TLS and forward authenticated traffic to a real server."""

    def __init__(self, root: Path, upstream_url: str) -> None:
        root.mkdir(parents=True)
        self.cert = root / "cert.pem"
        key = root / "key.pem"
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
                str(self.cert),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        self.upstream_url = upstream_url
        self.authorizations = 0
        self.token_exchanges = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _WorkspaceHandler)
        self.server.daemon_threads = True
        self.server.workspace = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server.server_port}"
        self.server_url = self.url + MOUNT
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> HostLoginWorkspace:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _WorkspaceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def workspace(self) -> HostLoginWorkspace:
        return self.server.workspace

    def _json(self, status: int, payload: object, **headers: str) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value.replace("\r", "").replace("\n", ""))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        workspace = self.workspace
        path = urlsplit(self.path).path
        issuer = workspace.url + "/oidc"
        if path == "/.well-known/databricks-config":
            self._json(200, {"workspace_id": WORKSPACE_ID, "oidc_endpoint": issuer})
        elif path == "/oidc/.well-known/oauth-authorization-server":
            self._json(
                200,
                {
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/v1/authorize",
                    "token_endpoint": issuer + "/v1/token",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["all-apis", "offline_access"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        elif path == "/oidc/v1/authorize":
            query = parse_qs(urlsplit(self.path).query)
            workspace.authorizations += 1
            callback = query["redirect_uri"][0]
            params = urlencode({"code": "local-code", "state": query["state"][0], "iss": issuer})
            self._json(302, {}, Location=f"{callback}?{params}")
        elif path.startswith(MOUNT):
            if self.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
                self._json(401, {}, **{"WWW-Authenticate": 'Bearer realm="DatabricksRealm"'})
            else:
                self._forward()
        elif path.startswith("/api/"):
            self._json(200, {"userName": USER_EMAIL, "id": "1"})
        else:
            self._json(404, {})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if urlsplit(self.path).path == "/oidc/v1/token":
            self.workspace.token_exchanges += 1
            self._json(
                200,
                {
                    "access_token": ACCESS_TOKEN,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "local-refresh-token",
                    "scope": "all-apis offline_access",
                },
            )
        else:
            self._json(404, {})

    def _forward(self) -> None:
        relative = self.path.removeprefix(MOUNT)
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in ("host", "content-length")
        }
        headers["X-Forwarded-Email"] = USER_EMAIL
        if self.headers.get("Upgrade", "").lower() == "websocket":
            upstream = urlsplit(self.workspace.upstream_url)
            with socket.create_connection(
                (upstream.hostname, upstream.port), timeout=10
            ) as tunnel:
                request = f"GET {relative} HTTP/1.1\r\nHost: {upstream.netloc}\r\n"
                request += "".join(f"{name}: {value}\r\n" for name, value in headers.items())
                tunnel.sendall((request + "\r\n").encode())
                tunnel.settimeout(None)
                while True:
                    pending = [self.connection] if self.connection.pending() else []
                    readable = pending or select.select([self.connection, tunnel], [], [], 1)[0]
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = tunnel if source is self.connection else self.connection
                        destination.sendall(data)
        else:
            with httpx.Client(trust_env=False) as client:
                response = client.get(self.workspace.upstream_url + relative, headers=headers)
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type", response.headers.get("Content-Type", "application/json")
            )
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    def log_message(self, format: str, *args: object) -> None:
        pass
