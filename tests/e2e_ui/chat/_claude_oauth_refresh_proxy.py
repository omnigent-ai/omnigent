"""Loopback HTTPS proxy that rejects Claude Code's OAuth token refresh.

Claude Code honors ``HTTPS_PROXY`` and ``NODE_EXTRA_CA_CERTS``, so a throwaway
local CA can terminate TLS and answer the refresh with ``invalid_grant``.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import socket
import ssl
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_SERVED_HOSTS = (
    "platform.claude.com",
    "console.anthropic.com",
    "api.anthropic.com",
    "claude.ai",
    "downloads.claude.ai",
    "statsig.anthropic.com",
)
REFRESH_PATH = "/v1/oauth/token"


def _write_cert_material(directory: Path) -> tuple[Path, Path, Path]:
    now = _dt.datetime.now(_dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "omnigent e2e refresh-proxy CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _SERVED_HOSTS[0])]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(h) for h in _SERVED_HOSTS]), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    ca_pem = directory / "ca.pem"
    leaf_pem = directory / "leaf.pem"
    key_pem = directory / "leaf-key.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    leaf_pem.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_pem.touch(mode=0o600)
    key_pem.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_pem.chmod(0o600)
    return ca_pem, leaf_pem, key_pem


def _read_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def _response(status: str, body: Mapping[str, object]) -> bytes:
    payload = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + payload


@dataclass
class ClaudeOAuthRefreshRejectingProxy:
    """Serve ``HTTPS_PROXY`` for Claude Code; log every tunneled request."""

    directory: Path
    requests: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.ca_pem, leaf_pem, key_pem = _write_cert_material(self.directory / "certs")
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(str(leaf_pem), str(key_pem))
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = self.directory / "proxy-requests.jsonl"
        self._lock = threading.Lock()
        self._connections: list[threading.Thread] = []
        self._live: set[socket.socket] = set()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def env(self) -> dict[str, str]:
        """Environment that routes Claude Code through this proxy and trusts its CA."""
        return {
            "HTTPS_PROXY": self.url,
            "NO_PROXY": "127.0.0.1,localhost",
            "NODE_EXTRA_CA_CERTS": str(self.ca_pem),
        }

    def close(self) -> None:
        self._listener.close()
        with self._lock:
            live, workers = list(self._live), list(self._connections)
        for conn in live:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
        for worker in workers:
            worker.join(timeout=5)

    def snapshot(self) -> list[dict[str, str]]:
        """Copy of the recorded requests, safe to read while connections are still served."""
        with self._lock:
            return list(self.requests)

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            with self._lock:
                self._connections.append(worker)
                self._live.add(conn)
            worker.start()

    def _record(self, entry: dict[str, str]) -> None:
        with self._lock:
            self.requests.append(entry)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry) + "\n")

    def _serve(self, conn: socket.socket) -> None:
        host = ""
        recorded = False
        try:
            conn.settimeout(10)
            head, _, pipelined = _read_head(conn).partition(b"\r\n\r\n")
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, target, *_ = [*line.split(" "), "", ""]
            if method != "CONNECT":
                self._record({"host": "", "method": method, "path": target, "status": "403"})
                conn.sendall(_response("403 Forbidden", {"error": "connect only"}))
                return
            host = target.rsplit(":", 1)[0]
            if pipelined:
                # Bytes sent before our 200 are lost to the TLS handshake below.
                self._record(
                    {"host": host, "method": method, "path": target, "status": "error:pipelined"}
                )
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            with self._tls.wrap_socket(conn, server_side=True) as tls:
                req = _read_head(tls)
                first = req.split(b"\r\n", 1)[0].decode("latin-1")
                req_method, path, *_ = [*first.split(" "), "", ""]
                if path.startswith(REFRESH_PATH):
                    status, body = (
                        "400 Bad Request",
                        {
                            "error": "invalid_grant",
                            "error_description": "The refresh token is invalid or revoked.",
                        },
                    )
                elif host == "api.anthropic.com":
                    status, body = (
                        "401 Unauthorized",
                        {
                            "type": "error",
                            "error": {"type": "authentication_error", "message": "invalid token"},
                        },
                    )
                else:
                    status, body = "404 Not Found", {}
                self._record(
                    {"host": host, "method": req_method, "path": path, "status": status[:3]}
                )
                recorded = True
                tls.sendall(_response(status, body))
        except (OSError, ssl.SSLError, ValueError) as exc:
            if not recorded:
                self._record(
                    {
                        "host": host,
                        "method": "",
                        "path": "",
                        "status": f"error:{type(exc).__name__}",
                    }
                )
            return
        finally:
            with self._lock:
                self._live.discard(conn)
            with contextlib.suppress(OSError):
                conn.close()
