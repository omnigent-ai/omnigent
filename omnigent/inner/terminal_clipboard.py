"""Route managed terminals' native text copies through clipboard consent.

The private ``pbcopy`` command talks to a write-only, size-limited socket.
Only its owner can access tmux's control socket; pane code receives no new
tmux authority. Browser attachments retain their existing consent/input gates,
and native terminal attachments receive tmux's normal clipboard notification.

This is clipboard integration, not an OS sandbox: it does not prevent an
unsandboxed program from deliberately using an absolute native clipboard API.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

MAX_CLIPBOARD_BYTES = 1024 * 1024
_IO_TIMEOUT = 1.0
_BUFFER_NAME = "omnigent-clipboard"


def _receive_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    """Read one bounded protocol field, rejecting truncated requests."""
    chunks = bytearray()
    while len(chunks) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Clipboard request timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ValueError("Truncated clipboard request")
        chunks.extend(chunk)
    return bytes(chunks)


def _serve(
    listener: socket.socket,
    stopped: threading.Event,
    tmux: str,
    tmux_socket: str,
) -> None:
    """Accept text only; never execute a command supplied by the pane."""
    while not stopped.is_set():
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        with connection:
            try:
                deadline = time.monotonic() + _IO_TIMEOUT
                (size,) = struct.unpack("!I", _receive_exact(connection, 4, deadline))
                if size > MAX_CLIPBOARD_BYTES:
                    raise ValueError("Clipboard request is too large")
                payload = _receive_exact(connection, size, deadline)
                payload.decode("utf-8")
                if stopped.is_set():
                    return
                result = subprocess.run(
                    [tmux, "-S", tmux_socket, "load-buffer", "-w", "-b", _BUFFER_NAME, "-"],
                    input=payload,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_IO_TIMEOUT,
                    check=False,
                )
                connection.settimeout(0.2)
                connection.sendall(b"\x00" if result.returncode == 0 else b"\x01")
            except (OSError, ValueError, subprocess.SubprocessError):
                with contextlib.suppress(OSError):
                    connection.settimeout(0.2)
                    connection.sendall(b"\x01")


def _stop(listener: socket.socket, stopped: threading.Event, path: Path) -> None:
    """Release the private listener when its terminal owner disappears."""
    stopped.set()
    listener.close()
    with contextlib.suppress(OSError):
        path.unlink()


class TerminalClipboardBridge:
    """Own the native clipboard helper and its restricted tmux bridge."""

    def __init__(self, private_dir: Path, tmux_socket: Path) -> None:
        self.bin_dir = private_dir / "clipboard-bin"
        self.socket_path = private_dir / "clip.sock"
        self.tmux_socket = tmux_socket
        self._cleanup: weakref.finalize | None = None
        self._thread: threading.Thread | None = None

    def prepare_environment(self, env: dict[str, str]) -> None:
        """Prepend a terminal-private copy helper without changing host settings."""
        self.bin_dir.mkdir(mode=0o700, exist_ok=True)
        command = shlex.join(
            [sys.executable, str(Path(__file__).resolve()), str(self.socket_path)]
        )
        helper = self.bin_dir / "pbcopy"
        helper.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
        helper.chmod(0o700)
        env["PATH"] = os.pathsep.join([str(self.bin_dir), env.get("PATH", os.defpath)])

    def start(self) -> None:
        """Start one bounded listener before the pane can invoke its helper."""
        if self._cleanup is not None and self._cleanup.alive:
            return
        tmux = shutil.which("tmux")
        if tmux is None:
            raise RuntimeError("tmux is required for terminal clipboard routing")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            listener.bind(str(self.socket_path))
            bound = True
            self.socket_path.chmod(0o600)
            listener.listen(4)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            if bound:
                with contextlib.suppress(OSError):
                    self.socket_path.unlink()
            raise
        stopped = threading.Event()
        self._cleanup = weakref.finalize(self, _stop, listener, stopped, self.socket_path)
        self._thread = threading.Thread(
            target=_serve,
            args=(listener, stopped, tmux, str(self.tmux_socket)),
            name="terminal-clipboard",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            self._cleanup()
            self._thread = None
            raise

    def close(self) -> None:
        """Stop serving copies and join the bounded worker before cleanup."""
        if self._cleanup is not None:
            self._cleanup()
        if self._thread is not None:
            self._thread.join(timeout=2 * _IO_TIMEOUT + 0.5)
            self._thread = None


def _copy_to_terminal(socket_path: str, payload: bytes) -> bool:
    """Submit bounded UTF-8 text, never falling back to the host clipboard."""
    if len(payload) > MAX_CLIPBOARD_BYTES:
        return False
    try:
        payload.decode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2 * _IO_TIMEOUT)
            connection.connect(socket_path)
            connection.sendall(struct.pack("!I", len(payload)) + payload)
            return connection.recv(1) == b"\x00"
    except (OSError, ValueError):
        return False


def _main() -> int:
    """Implement the text/general-pasteboard subset used by terminal programs."""
    if len(sys.argv) < 2:
        return 2
    args = sys.argv[2:]
    while args:
        option = args.pop(0)
        if option == "-pboard" and args and args.pop(0) == "general":
            continue
        if option == "-Prefer" and args and args.pop(0) == "txt":
            continue
        print(
            "Omnigent terminal copying supports plain text on the general clipboard.",
            file=sys.stderr,
        )
        return 2
    payload = sys.stdin.buffer.read(MAX_CLIPBOARD_BYTES + 1)
    if _copy_to_terminal(sys.argv[1], payload):
        return 0
    print("Could not send text to the terminal clipboard bridge.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
