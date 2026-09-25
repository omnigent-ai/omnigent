"""Claude web steering sends the supported send-now chord through a real PTY.

A fake Claude TUI accepts an idle prompt, then queues the next prompt until
it receives Ctrl-X followed by Ctrl-S. The composer clears before the
send-now hint appears, matching asynchronous prompt screening. The real
executor and bridge must promote the queued message only on a supported
Claude version, without interrupting idle input or submitting twice.

Requires only tmux; no Claude installation, server, or model credentials::

    uv run --no-sync python -m pytest tests/e2e/test_claude_native_send_now_steer_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict, cast

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import TurnComplete

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH"),
    pytest.mark.timeout(30),
]

_INITIAL_MESSAGE = "keep working until I steer you"
_STEER_MESSAGE = "prioritize the regression test now"


class _TuiState(TypedDict):
    submitted: list[str]
    delivered: list[str]
    queued: list[str]
    shortcut_bytes: list[int]
    enter_count: int


_FAKE_CLAUDE_TUI = r"""
import json
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

state_path = Path(sys.argv[1])
show_placeholder = sys.argv[2] == "placeholder"
show_misleading_footer = sys.argv[2] == "misleading-footer"
hook_state_path = Path(sys.argv[3])
state = {
    "submitted": [], "delivered": [], "queued": [], "shortcut_bytes": [], "enter_count": 0,
}
draft = bytearray()
escape = bytearray()
pasting = False
busy = False
chord_started = False
hint_visible = False
hint_ready_at = None
rule = "─" * 70


def render():
    pending = state_path.with_suffix(".tmp")
    pending.write_text(json.dumps(state), encoding="utf-8")
    pending.replace(state_path)
    lines = ["accepted: " + message for message in state["delivered"]]
    if state["queued"]:
        lines.extend("❯ " + message for message in state["queued"])
        if hint_visible:
            lines.append("  ctrl+x ctrl+s to send now")
    if busy:
        lines.extend(["", "· Thinking… (25s · still thinking with max effort)", ""])
    text = draft.decode("utf-8", errors="replace").replace("\r", " ")
    if not text and state["queued"] and show_placeholder:
        text = "Press up to edit queued messages"
    lines.extend([rule, "❯ " + text, rule])
    if state["queued"] and show_misleading_footer:
        lines.append("  Background task ❯ " + state["queued"][-1])
    sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines) + "\r\n")
    sys.stdout.flush()


fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
sys.stdout.write("\x1b[?2004h")
render()
try:
    while True:
        timeout = None if hint_ready_at is None else max(0, hint_ready_at - time.monotonic())
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            hint_visible = True
            hint_ready_at = None
            render()
            continue
        data = os.read(fd, 4096)
        if not data:
            break
        for byte in data:
            if escape:
                escape.append(byte)
                if byte == ord("~"):
                    pasting = escape == b"\x1b[200~"
                    escape.clear()
                continue
            if byte == 27:
                escape.append(byte)
                continue
            if pasting:
                draft.append(byte)
                continue
            if byte in (24, 19):
                state["shortcut_bytes"].append(byte)
                if byte == 19 and chord_started and hint_visible:
                    state["delivered"].extend(state["queued"])
                    state["queued"].clear()
                    hint_visible = False
                    hint_ready_at = None
                chord_started = byte == 24
            elif byte == 11:
                draft.clear()
            elif byte == 13:
                state["enter_count"] += 1
                if draft:
                    message = draft.decode("utf-8").strip("\r\n")
                    state["submitted"].append(message)
                    state["queued" if busy else "delivered"].append(message)
                    if not busy:
                        hook_state_path.write_text(
                            json.dumps({"last_hook_event_name": "UserPromptSubmit"}),
                            encoding="utf-8",
                        )
                    if busy:
                        # Prompt screening finishes after Enter has cleared the composer.
                        hint_visible = False
                        hint_ready_at = time.monotonic() + 0.4
                    busy = True
                    draft.clear()
            elif byte >= 32:
                draft.append(byte)
        render()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
"""


@pytest.fixture
def claude_steering_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    composer: str,
) -> Iterator[tuple[Path, Path]]:
    """Advertise a private tmux pane running the queue-aware fake Claude TUI."""
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "claude-native")
    bridge_dir = tmp_path / "claude-native" / "session"
    state_path = tmp_path / "tui-state.json"
    tui_path = tmp_path / "fake_claude_tui.py"
    tui_path.write_text(_FAKE_CLAUDE_TUI, encoding="utf-8")

    # macOS's default temp path can exceed the Unix socket path length limit.
    with tempfile.TemporaryDirectory(prefix="cc-steer-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "tmux.sock"
        subprocess.run(
            [
                "tmux",
                "-S",
                str(socket_path),
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "claude",
                "-x",
                "100",
                "-y",
                "24",
                sys.executable,
                str(tui_path),
                str(state_path),
                composer,
                str(bridge_dir / "state.json"),
            ],
            check=True,
            timeout=10,
        )
        try:
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="claude")
            (bridge_dir / "state.json").write_text(
                json.dumps({"last_hook_event_name": "SessionStart"}), encoding="utf-8"
            )
            deadline = time.monotonic() + 5
            pane = ""
            while time.monotonic() < deadline:
                pane = bridge._capture_pane(str(socket_path), "claude")
                if bridge._claude_prompt_rendered(pane):
                    break
                time.sleep(0.05)
            assert bridge._claude_prompt_rendered(pane), pane
            yield bridge_dir, state_path
        finally:
            subprocess.run(
                ["tmux", "-S", str(socket_path), "kill-server"],
                check=False,
                capture_output=True,
                timeout=10,
            )


def _read_state(path: Path) -> _TuiState:
    return cast(_TuiState, json.loads(path.read_text(encoding="utf-8")))


async def _submit_message(executor: ClaudeNativeExecutor, text: str, entrypoint: str) -> None:
    if entrypoint == "enqueue":
        assert await executor.enqueue_session_message("session", text)
    else:
        events = [
            event
            async for event in executor.run_turn(
                messages=[{"role": "user", "content": text}],
                tools=[],
                system_prompt="",
            )
        ]
        assert events == [TurnComplete(response=None)]


async def _start_busy_executor(bridge_dir: Path, state_path: Path) -> ClaudeNativeExecutor:
    executor = ClaudeNativeExecutor(bridge_dir=bridge_dir)
    await _submit_message(executor, _INITIAL_MESSAGE, "run_turn")
    assert _read_state(state_path) == {
        "submitted": [_INITIAL_MESSAGE],
        "delivered": [_INITIAL_MESSAGE],
        "queued": [],
        "shortcut_bytes": [],
        "enter_count": 1,
    }
    return executor


async def _wait_for_state(state_path: Path, expected: _TuiState) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if _read_state(state_path) == expected:
            return
        await asyncio.sleep(0.02)
    assert _read_state(state_path) == expected


@pytest.mark.parametrize("composer", ["empty", "placeholder"])
@pytest.mark.parametrize("entrypoint", ["run_turn", "enqueue"])
@pytest.mark.parametrize(
    "version", ["2.1.275", "2.1.274", None], ids=["supported", "old", "unknown"]
)
async def test_claude_send_now_promotes_only_supported_queued_messages(
    claude_steering_pane: tuple[Path, Path],
    entrypoint: str,
    version: str | None,
) -> None:
    """Both delivery paths steer supported queues without interrupting idle input."""
    bridge_dir, state_path = claude_steering_pane
    # The same queued UI on every version tests the version gate independently.
    if version is not None:
        (bridge_dir / "context_raw.json").write_text(
            json.dumps({"version": version}), encoding="utf-8"
        )
    executor = await _start_busy_executor(bridge_dir, state_path)
    await _submit_message(executor, _STEER_MESSAGE, entrypoint)

    supported = version == "2.1.275"
    expected_state: _TuiState = {
        "submitted": [_INITIAL_MESSAGE, _STEER_MESSAGE],
        "delivered": [_INITIAL_MESSAGE, _STEER_MESSAGE] if supported else [_INITIAL_MESSAGE],
        "queued": [] if supported else [_STEER_MESSAGE],
        "shortcut_bytes": [24, 19] if supported else [],
        "enter_count": 2,
    }
    await _wait_for_state(state_path, expected_state)


@pytest.mark.parametrize("composer", ["misleading-footer"])
@pytest.mark.parametrize("entrypoint", ["run_turn", "enqueue"])
async def test_claude_send_now_ignores_matching_prompt_text_in_a_footer(
    claude_steering_pane: tuple[Path, Path], entrypoint: str
) -> None:
    """A queued prompt echoed below the free composer must not cause Enter retries."""
    bridge_dir, state_path = claude_steering_pane
    (bridge_dir / "context_raw.json").write_text(
        json.dumps({"version": "2.1.282"}), encoding="utf-8"
    )
    executor = await _start_busy_executor(bridge_dir, state_path)

    await _submit_message(executor, _STEER_MESSAGE, entrypoint)

    await _wait_for_state(
        state_path,
        {
            "submitted": [_INITIAL_MESSAGE, _STEER_MESSAGE],
            "delivered": [_INITIAL_MESSAGE, _STEER_MESSAGE],
            "queued": [],
            "shortcut_bytes": [24, 19],
            "enter_count": 2,
        },
    )
