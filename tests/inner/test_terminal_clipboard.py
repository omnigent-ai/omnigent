"""Native copy helpers must submit text without touching the host clipboard."""

from __future__ import annotations

import contextlib
import gc
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.inner import terminal_clipboard as clipboard


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> Iterator[clipboard.TerminalClipboardBridge]:
    # Keep AF_UNIX paths under macOS's 104-byte limit.
    with tempfile.TemporaryDirectory(prefix="clip-") as directory:
        root = Path(directory)
        monkeypatch.setattr(clipboard.shutil, "which", lambda _name: "/test/tmux")
        instance = clipboard.TerminalClipboardBridge(root, root / "tmux.sock")
        try:
            yield instance
        finally:
            instance.close()


def test_native_helper_submits_exact_text_without_host_clipboard_access(
    bridge: clipboard.TerminalClipboardBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    env = os.environ.copy()
    old_path = env["PATH"]
    bridge.prepare_environment(env)
    assert env["PATH"] == f"{bridge.bin_dir}{os.pathsep}{old_path}"
    assert not (bridge.bin_dir / "pbpaste").exists()
    bridge.start()
    text = "copied λ\nsecond line\n".encode()
    with subprocess.Popen(
        [str(bridge.bin_dir / "pbcopy"), "-pboard", "general", "-Prefer", "txt"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    ) as proc:
        stdout, stderr = proc.communicate(text, timeout=5)
    assert (proc.returncode, stdout, stderr) == (0, b"", b"")
    run.assert_called_once_with(
        [
            "/test/tmux",
            "-S",
            str(bridge.tmux_socket),
            "load-buffer",
            "-w",
            "-b",
            "omnigent-clipboard",
            "-",
        ],
        input=text,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=clipboard._IO_TIMEOUT,
        check=False,
    )


@pytest.mark.parametrize("args", [[], ["-pboard", "find"], ["-Prefer", "rtf"], ["--read"]])
def test_helper_fails_closed_without_a_bridge(
    bridge: clipboard.TerminalClipboardBridge, args: list[str]
) -> None:
    env = os.environ.copy()
    bridge.prepare_environment(env)
    proc = subprocess.run(
        [str(bridge.bin_dir / "pbcopy"), *args],
        input=b"must not copy",
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stdout == b""


@pytest.mark.parametrize(
    "payload",
    [b"", b"\xff", b"X" * (clipboard.MAX_CLIPBOARD_BYTES + 1)],
    ids=["empty", "invalid-utf8", "oversized"],
)
def test_client_rejects_non_text_and_oversize_payloads(
    bridge: clipboard.TerminalClipboardBridge, payload: bytes
) -> None:
    assert not clipboard._copy_to_terminal(str(bridge.socket_path), payload)


def test_native_helper_rejects_empty_copies_explicitly(
    bridge: clipboard.TerminalClipboardBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    env = os.environ.copy()
    bridge.prepare_environment(env)
    bridge.start()
    with subprocess.Popen(
        [str(bridge.bin_dir / "pbcopy")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    ) as proc:
        stdout, stderr = proc.communicate(b"", timeout=5)
    assert proc.returncode == 1
    assert stdout == b""
    assert b"does not support clearing the clipboard" in stderr
    run.assert_not_called()


@pytest.mark.parametrize(
    "wire_request",
    [
        struct.pack("!I", 0),
        struct.pack("!I", clipboard.MAX_CLIPBOARD_BYTES + 1),
        struct.pack("!I", 1) + b"\xff",
        struct.pack("!I", 5) + b"short"[:2],
        b"\x00\x00",
    ],
    ids=["empty", "oversized", "invalid-utf8", "truncated-body", "truncated-header"],
)
def test_listener_rejects_invalid_requests_and_keeps_serving(
    bridge: clipboard.TerminalClipboardBridge,
    monkeypatch: pytest.MonkeyPatch,
    wire_request: bytes,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    bridge.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(bridge.socket_path))
        connection.sendall(wire_request)
        with contextlib.suppress(OSError):
            connection.shutdown(socket.SHUT_WR)
        assert connection.recv(1) == b"\x01"
    run.assert_not_called()
    assert clipboard._copy_to_terminal(str(bridge.socket_path), b"next copy")
    run.assert_called_once()


def test_slow_request_has_a_total_deadline(
    bridge: clipboard.TerminalClipboardBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    monkeypatch.setattr(clipboard, "_IO_TIMEOUT", 0.2)
    bridge.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(bridge.socket_path))
        connection.sendall(struct.pack("!I", 100))
        for _ in range(3):
            time.sleep(0.07)
            try:
                connection.sendall(b"a")
            except BrokenPipeError:
                break
        assert connection.recv(1) == b"\x01"
    run.assert_not_called()
    assert clipboard._copy_to_terminal(str(bridge.socket_path), b"not stalled")


def test_close_stops_worker_and_can_restart(bridge: clipboard.TerminalClipboardBridge) -> None:
    bridge.start()
    worker = bridge._thread
    bridge.start()
    assert bridge._thread is worker
    assert worker is not None and worker.is_alive()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(bridge.socket_path))
        connection.sendall(b"\x00")
        bridge.close()
    assert not worker.is_alive()
    assert not bridge.socket_path.exists()
    assert not clipboard._copy_to_terminal(str(bridge.socket_path), b"closed")
    bridge.start()
    assert bridge.socket_path.exists()


def test_abandoned_bridge_stops_worker(bridge: clipboard.TerminalClipboardBridge) -> None:
    abandoned = clipboard.TerminalClipboardBridge(bridge.bin_dir.parent, bridge.tmux_socket)
    abandoned.start()
    worker = abandoned._thread
    reference = weakref.ref(abandoned)
    del abandoned
    gc.collect()
    assert reference() is None
    assert worker is not None
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not bridge.socket_path.exists()


def test_thread_start_failure_cleans_up_socket(
    bridge: clipboard.TerminalClipboardBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("cannot start worker")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="cannot start worker"):
        bridge.start()
    assert not bridge.socket_path.exists()
    bridge.close()


def test_tmux_failure_is_reported_without_native_fallback(
    bridge: clipboard.TerminalClipboardBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(side_effect=subprocess.TimeoutExpired("tmux", 1))
    monkeypatch.setattr(clipboard.subprocess, "run", run)
    bridge.start()
    assert not clipboard._copy_to_terminal(str(bridge.socket_path), b"failed")
    run.assert_called_once()
