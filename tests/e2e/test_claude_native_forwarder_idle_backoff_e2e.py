"""End-to-end regression: idle claude-native transcript-forwarder polling.

The claude-native transcript forwarder (``supervise_forwarder`` ->
``forward_claude_transcript_to_session``) polls its bridge directory every
``_DEFAULT_POLL_INTERVAL_S = 0.25`` s per session, forever, regardless of
activity. Each tick re-opens the hook-state JSON, the bridge config, the hooks
log, and the transcript, and runs ~8 forward scans -- so a **fully idle**
session (or one whose harness already emitted a terminal ``Stop`` hook) still
burns a fixed ~28 file-opens/s of pure overhead, scaling linearly with the
number of accumulated sessions (observed 20-65% CPU on end-of-day hosts).

This test drives the REAL production entrypoint -- the same
``supervise_forwarder`` coroutine the runner starts for every claude-native
session, with its default poll interval -- against an on-disk bridge dir and a
live (loopback) Omnigent-shaped HTTP server, exactly as in production. It then
observes the forwarder from the OUTSIDE, counting ``IN_OPEN`` inotify events on
the bridge directory over a fully idle window.

The assertions encode the FIXED contract, so they FAIL while the bug is live:

* an idle session's steady-state file-open rate must fall well below the
  fixed-4 Hz firehose (any of the ticket's fix shapes -- interval backoff,
  a stat-based change-detector gating the scans, or teardown -- lands far
  below the threshold, while today's fixed rate is ~10x above it);
* a session whose harness reported ``Stop`` must quiesce at least as much.

Linux-only (inotify); CI runs on Linux.
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

from omnigent.claude_native_bridge import prepare_bridge_dir, record_hook_event
from omnigent.claude_native_forwarder import supervise_forwarder

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="observes the forwarder's file-open rate via inotify (Linux-only)",
)

# Let the forwarder finish its cold-start work (hook-state ensure, external
# session-id mirror PATCH, initial transcript read) AND settle into its idle
# steady state before measuring. Any idle-throttle shape needs a settle window
# of unchanged inputs before it may engage (the shipped gate uses 8 s), so the
# warm-up must outlast it; measuring earlier captures the cold-start burst and
# not the steady state. An unfixed fixed-rate poller fails identically at any
# warm-up length.
_WARMUP_S = 12.0
# Fully idle observation window. Long enough that a fixed 4 Hz poller produces
# an unambiguous count (~112 opens at the observed ~28 opens/s), short enough
# to keep the test fast.
_IDLE_WINDOW_S = 4.0
# Maximum tolerated file-opens per second on the bridge dir while idle. The
# live bug produces ~28/s (4 polls/s x ~7 opens/tick). Any reasonable fix --
# backing the interval off toward >=1 s when nothing changed, gating the scans
# behind a stat()-based change detector (stat emits no IN_OPEN), or tearing
# down after a terminal Stop -- lands at or near <=3/s. Chosen ~10x below the
# buggy rate so the test is robust to fix shape but fails loudly today.
_MAX_IDLE_OPENS_PER_S = 3.0

_IN_OPEN = 0x00000020


@pytest.fixture(autouse=True)
def _bridge_root_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Relocate the bridge root under this test's temp dir (mirrors the unit suite)."""
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "claude-native")
    # The idle gate must be at its production default here: an ambient
    # kill-switch value on the host would measure the ungated loop instead.
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
    """Create a bridge dir + settled transcript, exactly as hooks leave them.

    :param tmp_path: Per-test temp dir (transcript home, like ~/.claude/projects).
    :param conversation_id: Omnigent conversation id, e.g. ``"conv_idle"``.
    :param last_hook_event: The hook event the harness last reported, e.g.
        ``"SessionStart"`` (idle-but-live) or ``"Stop"`` (harness finished).
    :returns: The bridge directory path.
    """
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

    Watching a directory reports opens of files inside it; watching a file
    reports opens of that file. ``stat()`` calls emit no ``IN_OPEN``, so a
    fixed change-detector probe would not count against the budget.

    :param paths: Directories/files to watch, e.g. the bridge dir + transcript.
    :param duration_s: Observation window in seconds.
    :returns: Total number of open events observed.
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
    """Run the production forwarder on an idle bridge; return opens/second.

    Starts ``supervise_forwarder`` (the exact coroutine the runner launches per
    claude-native session, default poll interval), waits out the cold-start,
    then counts bridge-dir + transcript file opens over a fully idle window.

    :param base_url: The accept-all Omnigent stand-in server URL.
    :param tmp_path: Per-test temp dir.
    :param conversation_id: Omnigent conversation id for this session.
    :param last_hook_event: Hook state to seed, ``"SessionStart"`` or ``"Stop"``.
    :returns: Observed file opens per second during the idle window.
    """
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
    """A fully idle claude-native session must not keep polling at the fixed 4 Hz rate.

    Journey: a user starts a claude-native session, runs a turn, and walks
    away. Nothing on disk changes; no turns run. The forwarder should throttle
    its per-session file polling (backoff, change-detector gating, or
    equivalent) instead of re-opening the bridge state + transcript ~28x/s
    forever -- the fixed-rate firehose that holds 20-65% CPU on idle hosts.
    """
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
    """A session whose harness reported a terminal ``Stop`` must quiesce its forwarder.

    Journey: a user's claude-native harness finishes (Claude Code emits its
    ``Stop`` hook) and the session sits in the sidebar untouched. The hook
    state on disk is ``{"last_hook_event_name": "Stop"}`` -- yet the forwarder
    keeps polling at the full fixed rate forever. Post-fix it must either tear
    down or back off; either way the idle open rate falls under the budget.
    """
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
