"""E2E (UI): approving a Cursor card whose terminal is gone must not be dropped silently.

A cursor-native session mirrors cursor-agent's in-terminal tool gate as a web
``ApprovalCard``. The card can sit pending for a long time, and by the time the
user clicks **Approve** the tmux socket behind the Cursor pane may no longer
exist (the terminal died, or the temp dir holding the socket was swept). The
runner then fails to deliver the ``y`` keystroke, logs
``failed to send cursor keystroke 'y' ...`` with a tmux-connect traceback, and
the user sees only "Approved" — cursor never receives the verdict and nothing
in the chat says so.

``cursor-agent`` has no mock backend and is not logged in on CI, so the gated
tool call is seeded into the cursor chat store the real runner-side supervisor
tails (``~/.cursor/chats/<md5(workspace)>/<chat>/store.db``); everything from
detection onward — the runner supervisor, the server hook, the SPA card, the
Approve click and the keystroke delivery — runs for real against the real
``cursor-agent`` pane the runner launched.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.cursor_native.bridge import bridge_dir_for_session_id, read_tmux_info
from omnigent.process_logging import process_log_dir
from tests.e2e_ui.conftest import _REPO_ROOT
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view

_APPROVAL_CARD = '[data-testid="approval-card"]'
_GATED_COMMAND = "rm -rf build/"
_KEYSTROKE_ERROR_RE = re.compile(r"ERROR.*failed to send cursor keystroke")
_DELIVERY_FAILURE_NOTICE_RE = re.compile(
    r"could not be delivered|no longer running|not running|terminal (has )?(exited|closed|gone)",
    re.IGNORECASE,
)

_TERMINAL_READY_TIMEOUT_S = 120.0
_CARD_TIMEOUT_MS = 60_000
_NOTICE_TIMEOUT_S = 20.0


def _cursor_unavailable_reason() -> str | None:
    if shutil.which("cursor-agent") is None:
        return "needs the `cursor-agent` binary on PATH (the runner launches it in the pane)."
    if shutil.which("tmux") is None:
        return "needs `tmux` on PATH (runner-owned TUI pane)."
    return None


pytestmark = pytest.mark.skipif(
    _cursor_unavailable_reason() is not None,
    reason=_cursor_unavailable_reason() or "",
)


def _wait_for_tmux_info(bridge_dir: Path, timeout_s: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        time.sleep(0.5)
    raise AssertionError(
        f"the runner never advertised the cursor pane's tmux target in {bridge_dir} "
        f"within {timeout_s:.0f}s"
    )


def _tmux_server_pid(socket_path: Path) -> int | None:
    """PID of the tmux server behind ``socket_path``, so cleanup can target exactly it."""
    proc = subprocess.run(
        ["tmux", "-S", str(socket_path), "display-message", "-p", "#{pid}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    pid = proc.stdout.strip()
    return int(pid) if proc.returncode == 0 and pid.isdigit() else None


def _seed_pending_shell_call(workspace: str, command: str) -> Path:
    """Write a cursor chat store holding one gated ``Shell`` call awaiting a human."""
    now_ms = int(time.time() * 1000)
    chat_dir = (
        Path.home()
        / ".cursor"
        / "chats"
        / hashlib.md5(workspace.encode("utf-8")).hexdigest()
        / str(uuid.uuid4())
    )
    chat_dir.mkdir(parents=True)
    (chat_dir / "meta.json").write_text(json.dumps({"createdAtMs": now_ms}), encoding="utf-8")
    message = {
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": f"call_{uuid.uuid4().hex}",
                "toolName": "Shell",
                "args": {"command": command},
            }
        ],
        "providerOptions": {"cursor": {"pendingToolCallStartedAtMs": now_ms}},
    }
    # cursor keeps a blocked call inside a binary checkpoint frame, not a plain-JSON row.
    data = b"\x0a\xff" + json.dumps(message).encode("utf-8") + b"\xff"
    con = sqlite3.connect(str(chat_dir / "store.db"))
    try:
        con.execute("CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB)")
        con.execute(
            "INSERT INTO blobs(id, data) VALUES(?, ?)",
            (hashlib.sha256(data).hexdigest(), data),
        )
        con.commit()
    finally:
        con.close()
    return chat_dir / "store.db"


def _runner_log_text() -> str:
    """Concatenate the spawned runner's process logs (it shares this process's data dir)."""
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(process_log_dir("runner").glob("runner-*.log"))
    )


def _keystroke_error_excerpt(log_text: str) -> str:
    lines = log_text.splitlines()
    for index, line in enumerate(lines):
        if _KEYSTROKE_ERROR_RE.search(line):
            return "\n".join(lines[index : index + 25])
    return ""


@pytest.mark.timeout(600)
def test_cursor_approval_after_pane_socket_vanishes_is_not_dropped_silently(
    page: Page,
    native_cursor_approval_session: tuple[str, str],
) -> None:
    base_url, session_id = native_cursor_approval_session
    bridge_dir = bridge_dir_for_session_id(session_id)
    tmux = _wait_for_tmux_info(bridge_dir, _TERMINAL_READY_TIMEOUT_S)
    socket_path = Path(tmux["socket_path"])
    assert socket_path.exists(), f"cursor pane socket {socket_path} missing right after launch"
    server_pid = _tmux_server_pid(socket_path)

    # The runner realpath-normalises the session workspace before hashing it.
    workspace = os.path.realpath(str(_REPO_ROOT))
    store_path = _seed_pending_shell_call(workspace, _GATED_COMMAND)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        _ensure_chat_view(page)

        card = page.locator(
            f'{_APPROVAL_CARD}[data-state="pending"]', has_text="Cursor wants to run Shell"
        ).first
        expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
        expect(card).to_contain_text(_GATED_COMMAND)

        # While the card is parked, the pane's tmux socket goes away (a swept temp
        # dir or a dead terminal); the runner is not told and keeps the approval parked.
        socket_path.unlink()

        card.get_by_role("button", name="Approve").click()
        responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
        expect(responded).to_be_visible(timeout=30_000)
        expect(responded.get_by_text("Approved", exact=False).first).to_be_visible()

        notice = page.get_by_text(_DELIVERY_FAILURE_NOTICE_RE).first
        deadline = time.monotonic() + _NOTICE_TIMEOUT_S
        while time.monotonic() < deadline:
            if notice.count() > 0 and notice.is_visible():
                break
            if _KEYSTROKE_ERROR_RE.search(_runner_log_text()):
                # The runner already gave up on the keystroke; allow the UI a moment to react.
                page.wait_for_timeout(5_000)
                break
            time.sleep(0.5)

        excerpt = _keystroke_error_excerpt(_runner_log_text())
        assert notice.count() > 0 and notice.is_visible(), (
            "Approve on a Cursor card whose terminal is gone shows only 'Approved': no "
            "message tells the user the approval could not be delivered to Cursor.\n"
            f"Runner log:\n{excerpt or '(no keystroke error logged)'}"
        )
        assert not excerpt, (
            "the runner recorded the undelivered approval as an ERROR traceback instead of "
            f"handling the missing Cursor pane:\n{excerpt}"
        )
    finally:
        shutil.rmtree(store_path.parent, ignore_errors=True)
        # The tmux server (and cursor-agent inside it) outlives its unlinked socket.
        if server_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(server_pid, signal.SIGTERM)
