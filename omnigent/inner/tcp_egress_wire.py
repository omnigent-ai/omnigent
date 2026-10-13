"""Standalone sandbox-side connector for fixed-destination TCP routes."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading

SOCKET_ENV = "OMNIGENT_TCP_EGRESS_SOCKET"
TOKEN_ENV = "OMNIGENT_TCP_EGRESS_TOKEN"
CONNECT_ENV = "OMNIGENT_TCP_CONNECT"
MAX_FRAME = 65536


def _read_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise OSError("TCP egress broker disconnected")
        data.extend(chunk)
    return bytes(data)


def _read_frame(sock: socket.socket) -> tuple[bytes, bytes]:
    header = _read_exact(sock, 5)
    length = struct.unpack("!I", header[1:])[0]
    if length > MAX_FRAME:
        raise OSError("TCP egress frame exceeds size limit")
    return header[:1], _read_exact(sock, length)


def _write_frame(sock: socket.socket, kind: bytes, data: bytes) -> None:
    if len(data) > MAX_FRAME:
        raise OSError("TCP egress frame exceeds size limit")
    sock.sendall(kind + struct.pack("!I", len(data)) + data)


def main(argv: list[str] | None = None) -> int:
    """Expose a byte stream on stdin/stdout for a single named route."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0] or len(args[0]) > 64:
        print("Usage: omnigent-egress-connect <route-name>", file=sys.stderr)
        return 2
    path = os.environ.get(SOCKET_ENV)
    token = os.environ.get(TOKEN_ENV)
    if not path or not token:
        print("TCP egress broker is unavailable", file=sys.stderr)
        return 1
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(path)
            request = json.dumps({"token": token, "route": args[0]}).encode()
            if len(request) > 2048:
                raise OSError("TCP egress request is too large")
            sock.sendall(struct.pack("!I", len(request)) + request)
            if _read_exact(sock, 1) != b"\x00":
                print(sock.recv(1024).decode(errors="replace"), file=sys.stderr)
                return 1
            sock.settimeout(None)

            def send_input() -> None:
                try:
                    while chunk := os.read(sys.stdin.fileno(), MAX_FRAME):
                        _write_frame(sock, b"I", chunk)
                    _write_frame(sock, b"Z", b"")
                except OSError:
                    pass

            threading.Thread(target=send_input, daemon=True).start()
            while True:
                kind, body = _read_frame(sock)
                if kind == b"O":
                    remaining = memoryview(body)
                    while remaining:
                        remaining = remaining[os.write(sys.stdout.fileno(), remaining) :]
                elif kind == b"D":
                    print(body.decode(errors="replace"), file=sys.stderr)
                    return 1
                elif kind == b"Z" and not body:
                    sys.stdout.close()
                elif kind == b"X" and len(body) == 4:
                    return struct.unpack("!i", body)[0]
                else:
                    raise OSError("TCP egress broker sent an invalid frame")
    except OSError as exc:
        print(f"TCP egress connection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
