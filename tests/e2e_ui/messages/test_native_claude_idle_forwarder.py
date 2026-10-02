"""An idle claude-native session must not keep the runner's transcript forwarder busy.

Once a turn ends, the forwarder that mirrors Claude Code's transcript into the
session must stop re-reading the bridge state and transcript on every 0.25 s
tick: a session nobody is using should cost the runner (almost) nothing, so a
host that accumulates idle sessions does not burn CPU in proportion to how many
exist.

The journey runs the real Claude Code CLI against the mock model: send one
message from the web composer, wait for the reply and for the session to report
idle, then leave it untouched. Over an idle window the test counts with inotify
how often the session's bridge files and Claude transcript are opened, and
samples the runner process tree's CPU time.

Run::

    pytest tests/e2e_ui/messages/test_native_claude_idle_forwarder.py \\
        --ui-skip-build -v
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import os
import select
import shutil
import struct
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import psutil
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(shutil.which("claude") is None, reason="requires the claude CLI")

_IDLE_TIMEOUT_S = 60.0
# Long enough for a change-gated forwarder to settle after the turn's last write.
_SETTLE_S = 10.0
_WINDOW_S = 10.0
# A 0.25 s tick opens several bridge files (~28 opens/s when unthrottled); an idle
# forwarder needs at most an occasional resync.
_MAX_IDLE_OPENS_PER_S = 3.0

_IN_OPEN = 0x00000020
_INOTIFY_EVENT = struct.Struct("iIII")


class _OpenCounter:
    """Count ``IN_OPEN`` events for the entries directly inside the watched directories."""

    def __init__(self, directories: list[Path]) -> None:
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self._fd = self._libc.inotify_init1(os.O_NONBLOCK)
        if self._fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._dirs: dict[int, Path] = {}
        for directory in directories:
            wd = self._libc.inotify_add_watch(self._fd, os.fsencode(directory), _IN_OPEN)
            if wd < 0:
                raise OSError(ctypes.get_errno(), f"inotify_add_watch failed for {directory}")
            self._dirs[wd] = directory

    def close(self) -> None:
        os.close(self._fd)

    def count(self, duration_s: float) -> Counter[str]:
        counts: Counter[str] = Counter()
        deadline = time.monotonic() + duration_s
        while (remaining := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                continue
            buf = os.read(self._fd, 1 << 16)
            offset = 0
            while offset < len(buf):
                wd, mask, _cookie, name_len = _INOTIFY_EVENT.unpack_from(buf, offset)
                start = offset + _INOTIFY_EVENT.size
                name = buf[start : start + name_len].rstrip(b"\0").decode(errors="replace")
                offset = start + name_len
                if mask & _IN_OPEN:
                    counts[str(self._dirs[wd] / name) if name else f"{self._dirs[wd]}/"] += 1
        return counts


def _session(base_url: str, session_id: str) -> dict[str, object]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _wait_idle(base_url: str, session_id: str) -> dict[str, object]:
    deadline = time.monotonic() + _IDLE_TIMEOUT_S
    while True:
        session = _session(base_url, session_id)
        if session.get("status") == "idle":
            return session
        assert time.monotonic() < deadline, (
            f"session {session_id} did not report idle after its turn: "
            f"status={session.get('status')!r}"
        )
        time.sleep(0.5)


def _bridge_dir(session: dict[str, object], session_id: str) -> Path:
    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )

    labels = session.get("labels") or {}
    bridge_id = labels.get(BRIDGE_ID_LABEL_KEY) if isinstance(labels, dict) else None
    return bridge_dir_for_bridge_id(str(bridge_id or session_id))


def _watched_directories(bridge_dir: Path) -> list[Path]:
    from omnigent.harnesses.claude_native.bridge import read_transcript_path

    transcript = read_transcript_path(bridge_dir)
    assert transcript is not None, (
        f"Claude's hooks never reported a transcript path in {bridge_dir}"
    )
    candidates = [
        bridge_dir,
        *sorted(p for p in bridge_dir.iterdir() if p.is_dir()),
        transcript.parent,
        transcript.with_suffix(""),
        transcript.with_suffix("") / "subagents",
    ]
    directories: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir() and candidate not in directories:
            directories.append(candidate)
    return directories


def _last_hook_event(bridge_dir: Path) -> object:
    try:
        state = json.loads((bridge_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state.get("last_hook_event_name") if isinstance(state, dict) else None


def _cpu_seconds(processes: list[psutil.Process]) -> dict[int, tuple[str, float]]:
    samples: dict[int, tuple[str, float]] = {}
    for proc in processes:
        try:
            times = proc.cpu_times()
            label = " ".join(proc.cmdline()[:4])[:90] or proc.name()
        except psutil.Error:
            continue
        samples[proc.pid] = (label, times.user + times.system)
    return samples


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_idle_native_claude_session_stops_polling(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """After one exchange, an untouched claude-native session goes quiet on disk."""
    base_url, session_id = native_claude_mock_session
    runner = psutil.Process(int(_server_state["runner_pid"]))

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    nonce = uuid.uuid4().hex[:8]
    token = f"ast-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)
    _send(page, _turn_prompt(1, f"usr-{nonce}", token))
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    session = _wait_idle(base_url, session_id)

    bridge_dir = _bridge_dir(session, session_id)
    directories = _watched_directories(bridge_dir)
    time.sleep(_SETTLE_S)

    counter = _OpenCounter(directories)
    try:
        tree = [runner, *runner.children(recursive=True)]
        cpu_before = _cpu_seconds(tree)
        opens = counter.count(_WINDOW_S)
        cpu_after = _cpu_seconds(tree)
    finally:
        counter.close()

    total_opens = sum(opens.values())
    opens_per_s = total_opens / _WINDOW_S
    cpu_lines = []
    for pid, (label, after) in cpu_after.items():
        before = cpu_before.get(pid, (label, after))[1]
        cpu_lines.append(f"  {100.0 * (after - before) / _WINDOW_S:5.1f}%  pid {pid}  {label}")
    final_status = _session(base_url, session_id).get("status")
    report = (
        f"idle claude-native session {session_id} (status={final_status!r}, "
        f"last hook event={_last_hook_event(bridge_dir)!r}): {total_opens} file opens in "
        f"{_WINDOW_S:.0f}s = {opens_per_s:.2f}/s (budget {_MAX_IDLE_OPENS_PER_S:.1f}/s)\n"
        "opens by path:\n"
        + "\n".join(f"  {count:5d}  {path}" for path, count in opens.most_common())
        + "\nrunner process tree CPU over the window:\n"
        + "\n".join(cpu_lines)
    )
    _log.info(report)
    assert opens_per_s <= _MAX_IDLE_OPENS_PER_S, report
