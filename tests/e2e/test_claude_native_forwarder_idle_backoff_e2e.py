"""Measure idle forwarder file opens through the production supervisor.

Linux inotify observes an on-disk bridge while ``supervise_forwarder`` runs
against a loopback HTTP server. Idle and stopped sessions must both quiesce.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir, record_hook_event
from omnigent.harnesses.claude_native.forwarder import supervise_forwarder

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="observes the forwarder's file-open rate via inotify (Linux-only)",
)

# Outlast the 8 s idle gate and cold-start work before measuring.
_WARMUP_S = 12.0
_IDLE_WINDOW_S = 4.0
# A 4 Hz ungated loop opens ~28 files/s; periodic resync still needs headroom.
_MAX_IDLE_OPENS_PER_S = 3.0

_IN_OPEN = 0x00000020


@pytest.fixture(autouse=True)
def _bridge_root_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Relocate the bridge root under this test's temp dir (mirrors the unit suite)."""
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "claude-native"
    )
    # An ambient kill switch would measure the ungated loop.
    monkeypatch.delenv("OMNIGENT_CLAUDE_FORWARDER_IDLE_GATE", raising=False)


class _AcceptAllHandler(BaseHTTPRequestHandler):
    """Accept every Omnigent-shaped request so the forwarder never errors/retries."""

    def log_message(self, format: str, *args: object) -> None:  # shadows builtin per stdlib API
        del format, args

    def _ok(self) -> None:
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._ok()

    def do_PATCH(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._ok()

    def do_GET(self) -> None:
        self._ok()


@pytest.fixture
def accept_all_server() -> Any:
    """A live loopback HTTP server standing in for the Omnigent server."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AcceptAllHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _make_idle_bridge(tmp_path: Path, conversation_id: str, last_hook_event: str) -> Path:
    """Create a settled transcript and its bridge hook state."""
    bridge_dir = prepare_bridge_dir(conversation_id, workspace=tmp_path)
    transcript = tmp_path / f"{conversation_id}-transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": f"user-{conversation_id}",
                "message": {"role": "user", "content": "hello"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": f"assistant-{conversation_id}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": last_hook_event,
            "session_id": f"claude-{conversation_id}",
            "transcript_path": str(transcript),
        },
    )
    return bridge_dir


def _count_opens(paths: list[Path], duration_s: float) -> int:
    """Count inotify ``IN_OPEN`` events across *paths* over *duration_s* seconds.

    Directory watches include contained files; ``stat()`` emits no ``IN_OPEN``.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK)
    if fd < 0:
        pytest.skip("inotify unavailable in this environment")
    total = 0
    try:
        for path in paths:
            if libc.inotify_add_watch(fd, str(path).encode(), _IN_OPEN) < 0:
                pytest.skip(f"inotify watch failed for {path}")
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            offset = 0
            while offset < len(data):
                _wd, _mask, _cookie, name_len = struct.unpack_from("iIII", data, offset)
                offset += 16 + name_len
                total += 1
    finally:
        os.close(fd)
    return total


async def _measure_idle_open_rate(
    base_url: str, tmp_path: Path, conversation_id: str, last_hook_event: str
) -> float:
    """Run ``supervise_forwarder`` and measure settled file opens per second."""
    bridge_dir = _make_idle_bridge(tmp_path, conversation_id, last_hook_event)
    transcript = tmp_path / f"{conversation_id}-transcript.jsonl"
    task = asyncio.create_task(
        supervise_forwarder(
            base_url=base_url,
            headers={},
            session_id=conversation_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=True,
        )
    )
    try:
        await asyncio.sleep(_WARMUP_S)
        loop = asyncio.get_running_loop()
        opens = await loop.run_in_executor(
            None, _count_opens, [bridge_dir, transcript], _IDLE_WINDOW_S
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return opens / _IDLE_WINDOW_S


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_idle_claude_native_session_forwarder_backs_off(
    accept_all_server: str, tmp_path: Path
) -> None:
    """An idle session must not keep polling its bridge at the full rate."""
    opens_per_s = await _measure_idle_open_rate(
        accept_all_server, tmp_path, "conv_forwarder_idle", "SessionStart"
    )
    assert opens_per_s <= _MAX_IDLE_OPENS_PER_S, (
        f"idle claude-native forwarder opened bridge/transcript files "
        f"{opens_per_s:.1f}x/s over a fully idle {_IDLE_WINDOW_S:.0f}s window "
        f"(expected <= {_MAX_IDLE_OPENS_PER_S}/s once idle polling is throttled). "
        f"This is the fixed-4Hz idle polling bug: _DEFAULT_POLL_INTERVAL_S "
        f"drives every session at full rate with no idle backoff, burning "
        f"CPU proportional to session count."
    )


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_stopped_claude_native_session_forwarder_quiesces(
    accept_all_server: str, tmp_path: Path
) -> None:
    """A terminal ``Stop`` hook must quiesce the session's forwarder."""
    opens_per_s = await _measure_idle_open_rate(
        accept_all_server, tmp_path, "conv_forwarder_stop", "Stop"
    )
    assert opens_per_s <= _MAX_IDLE_OPENS_PER_S, (
        f"claude-native forwarder for a STOPPED harness (last_hook_event_name="
        f"'Stop') still opened bridge/transcript files {opens_per_s:.1f}x/s "
        f"over a fully idle {_IDLE_WINDOW_S:.0f}s window "
        f"(expected <= {_MAX_IDLE_OPENS_PER_S}/s -- a stopped harness should "
        f"quiesce or tear down its forwarder)."
    )
