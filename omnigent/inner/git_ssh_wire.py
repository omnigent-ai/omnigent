"""Standalone Git transport helper and its framed Unix-socket protocol."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading

SOCKET_ENV = "OMNIGENT_GIT_SSH_SOCKET"
TOKEN_ENV = "OMNIGENT_GIT_SSH_TOKEN"
MAX_FRAME = 65536


class GitSshDenied(Exception):
    """The requested Git operation is outside this broker's grant."""


def read_exact(sock: socket.socket, size: int) -> bytes | None:
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            if not result:
                return None
            raise ConnectionError("Git SSH connection ended inside a frame")
        result.extend(chunk)
    return bytes(result)


def read_frame(sock: socket.socket) -> tuple[bytes, bytes] | None:
    header = read_exact(sock, 5)
    if header is None:
        return None
    length = struct.unpack("!I", header[1:])[0]
    if length > MAX_FRAME:
        raise GitSshDenied("Git SSH frame exceeds size limit")
    body = read_exact(sock, length)
    if body is None:
        raise ConnectionError("Git SSH frame body is missing")
    return header[:1], body


def write_frame(sock: socket.socket, kind: bytes, body: bytes, lock: threading.Lock) -> None:
    if len(body) > MAX_FRAME:
        raise ValueError("Git SSH frame exceeds size limit")
    with lock:
        sock.sendall(kind + struct.pack("!I", len(body)) + body)


def main(argv: list[str] | None = None) -> int:
    """Act as Git's SSH command without holding a key or network route."""
    path = os.environ.get(SOCKET_ENV)
    token = os.environ.get(TOKEN_ENV)
    if not path or not token:
        print("Git SSH broker is unavailable", file=sys.stderr)
        return 128
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lock = threading.Lock()
    try:
        sock.connect(path)
        request: dict[str, object] = {
            "token": token,
            "argv": sys.argv[1:] if argv is None else argv,
        }
        if os.environ.get("GIT_PROTOCOL") == "version=2":
            request["git_protocol"] = "version=2"
        write_frame(sock, b"R", json.dumps(request).encode(), lock)
        response = read_frame(sock)
        if response is None or response[0] != b"A":
            detail = response[1].decode(errors="replace") if response else "broker disconnected"
            print(detail, file=sys.stderr)
            return 128

        def send_stdin() -> None:
            try:
                while chunk := os.read(sys.stdin.fileno(), MAX_FRAME):
                    write_frame(sock, b"I", chunk, lock)
                write_frame(sock, b"Z", b"", lock)
            except OSError:
                pass

        threading.Thread(target=send_stdin, daemon=True).start()
        while frame := read_frame(sock):
            kind, body = frame
            if kind == b"O":
                sys.stdout.buffer.write(body)
                sys.stdout.buffer.flush()
            elif kind == b"E":
                sys.stderr.buffer.write(body)
                sys.stderr.buffer.flush()
            elif kind == b"X":
                return struct.unpack("!i", body)[0]
            elif kind == b"D":
                print(body.decode(errors="replace"), file=sys.stderr)
                return 128
        return 128
    except (OSError, ConnectionError) as exc:
        print(f"Git SSH broker connection failed: {exc}", file=sys.stderr)
        return 128
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
