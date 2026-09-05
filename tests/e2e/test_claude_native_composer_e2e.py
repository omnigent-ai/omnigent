"""Opt-in live Claude/tmux checks for labelled headers and multiline replacement.

Run with OMNIGENT_E2E_CLAUDE_NATIVE=1 and an authenticated Claude CLI.
The private terminal uses safe mode with tools disabled; each case asks
the real model to echo a unique marker and verifies the recorded user turn.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.claude_native_bridge import (
    _claude_prompt_rendered,
    bridge_dir_for_bridge_id,
    inject_user_message,
    write_tmux_target,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1"
    or shutil.which("claude") is None
    or shutil.which("tmux") is None,
    reason="requires tmux, authenticated Claude, and OMNIGENT_E2E_CLAUDE_NATIVE=1",
)


def _tmux(socket: Path, *arguments: str) -> str:
    """Run a command against only this test's private tmux server."""
    return subprocess.run(
        ["tmux", "-S", str(socket), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


@pytest.fixture
def live_composer(tmp_path: Path) -> Iterator[tuple[Path, Path, str]]:
    """Start an isolated, named Claude session and always reap its terminal."""
    session_id = str(uuid.uuid4())
    socket = tmp_path / "tmux.sock"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir = bridge_dir_for_bridge_id(f"composer_e2e_{session_id}")
    try:
        _tmux(
            socket,
            "new-session",
            "-d",
            "-s",
            "composer",
            "-x",
            "120",
            "-y",
            "35",
            "-c",
            str(workspace),
            "claude",
            "--safe-mode",
            "--strict-mcp-config",
            "--no-chrome",
            "--tools",
            "",
            "--name",
            "composer-e2e",
            "--session-id",
            session_id,
        )
        write_tmux_target(bridge_dir, socket_path=socket, tmux_target="composer")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            pane = _tmux(socket, "capture-pane", "-p", "-t", "composer")
            (tmp_path / "startup-pane.txt").write_text(pane)
            if _claude_prompt_rendered(pane):
                assert " composer-e2e ─" in pane
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"Claude never rendered its labelled composer; see {tmp_path}")
        yield bridge_dir, socket, session_id
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=10)
        shutil.rmtree(bridge_dir, ignore_errors=True)


def _recorded_messages(session_id: str, role: str) -> list[str]:
    """Read only this test session's native transcript, tolerating a partial write."""
    paths = list((Path.home() / ".claude/projects").glob(f"*/{session_id}.jsonl"))
    if not paths:
        return []
    assert len(paths) == 1
    messages = []
    for line in paths[0].read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != role:
            continue
        content = record.get("message", {}).get("content", "")
        if isinstance(content, str):
            messages.append(content)
        elif isinstance(content, list):
            messages.append(
                "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
            )
    return messages


@pytest.mark.parametrize(
    ("diagram", "cursor_at_end"),
    [(None, True), ("│   │", True), ("│   │", False), ("─── note ─", True), ("─── note ─", False)],
    ids=["empty-labelled", "tree-end", "tree-middle", "divider-end", "divider-middle"],
)
def test_live_claude_composer_replacement(
    live_composer: tuple[Path, Path, str],
    tmp_path: Path,
    diagram: str | None,
    cursor_at_end: bool,
) -> None:
    """The real CLI receives exactly the replacement and the real model answers."""
    bridge_dir, socket, session_id = live_composer
    if diagram is not None:
        for index, line in enumerate(["explain this diagram", diagram, "end"]):
            if index:
                _tmux(socket, "send-keys", "-t", "composer", "M-Enter")
            _tmux(socket, "send-keys", "-t", "composer", "-l", line)
        if not cursor_at_end:
            _tmux(socket, "send-keys", "-t", "composer", "Up", "Left")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pane = _tmux(socket, "capture-pane", "-p", "-t", "composer")
            if diagram in pane and "  end" in pane:
                break
            time.sleep(0.1)
        else:
            pytest.fail("The real multiline draft never rendered")
        (tmp_path / "draft-pane.txt").write_text(pane)
        assert _claude_prompt_rendered(pane)

    marker = f"COMPOSER_E2E_{uuid.uuid4().hex[:12].upper()}"
    message = f"Reply with exactly this text and nothing else: {marker}"
    try:
        inject_user_message(bridge_dir, content=message, timeout_s=15)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            replies = _recorded_messages(session_id, "assistant")
            if any(reply.strip() == marker for reply in replies):
                users = _recorded_messages(session_id, "user")
                assert [text for text in users if marker in text] == [message]
                return
            time.sleep(0.5)
        pytest.fail(f"No real model reply for {marker}")
    finally:
        (tmp_path / "final-pane.txt").write_text(
            _tmux(socket, "capture-pane", "-p", "-t", "composer")
        )
