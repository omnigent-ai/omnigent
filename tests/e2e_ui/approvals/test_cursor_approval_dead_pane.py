"""E2E (UI): approving a Cursor permission card after its terminal pane is gone.

The cursor-native runner mirrors a gated Cursor tool call as a web approval card
and delivers the verdict as a ``tmux send-keys`` into the Cursor pane. The card
can stay parked for up to a day, and in that time the pane's tmux socket can
vanish (temp-dir cleanup, sleep, terminal teardown). Clicking Approve then never
reaches Cursor; the user must be told, instead of watching the card settle as
"Approved" while the runner only logs an ERROR traceback.

The pane is the real runner-launched ``cursor-agent`` TUI (no login needed: the
unauthenticated TUI stays alive), so ``tmux send-keys`` runs for real. CI has no
Cursor account, so the gated call is seeded into the cursor chat store the
runner's transcript supervisor tails; detection is the only stand-in.
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
from playwright.sync_api import expect

from omnigent.harnesses.cursor_native.bridge import (
    bridge_dir_for_session_id,
    capture_cursor_pane,
    read_tmux_info,
)
from omnigent.harnesses.cursor_native.forwarder import _cursor_chats_root, _workspace_hash
from omnigent.process_logging import process_log_dir
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APPROVAL_CARD = '[data-testid="approval-card"]'
_GATED_COMMAND = "rm -rf build/"
_PANE_TIMEOUT_S = 90.0
_CARD_TIMEOUT_MS = 60_000
_VERDICT_TIMEOUT_MS = 15_000
# Delivery wording only: a generic "terminal exited" error card is not this notice.
_UNDELIVERED_NOTICE = re.compile(
    r"(could ?n.t|cannot|can.t|unable to|was ?n.t|not)\W+(be\W+)?(deliver|reach)|undeliver",
    re.IGNORECASE,
)
_KEYSTROKE_FAILURE = "failed to send cursor keystroke"


def _cursor_pane_unavailable_reason() -> str | None:
    if shutil.which("cursor-agent") is None:
        return "needs the `cursor-agent` binary on PATH (the pane runs it; no login needed)"
    if shutil.which("tmux") is None:
        return "needs `tmux` on PATH (runner-owned TUI pane)"
    return None


pytestmark = pytest.mark.skipif(
    _cursor_pane_unavailable_reason() is not None,
    reason=_cursor_pane_unavailable_reason() or "",
)


def _wait_for_cursor_pane(session_id: str) -> dict[str, str]:
    """Return the pane's tmux socket/target once the runner advertises a live pane."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    deadline = time.monotonic() + _PANE_TIMEOUT_S
    while time.monotonic() < deadline:
        info = read_tmux_info(bridge_dir)
        if info is not None and capture_cursor_pane(bridge_dir) is not None:
            return info
        time.sleep(0.5)
    raise AssertionError(f"runner never advertised a live cursor pane for session {session_id}")


def _framed(obj: dict[str, object]) -> bytes:
    # Cursor keeps a pending call inside a binary checkpoint frame, not a JSON row.
    prefix = b"\n \x16\xa0\x815\x13b\xc6mt2\x90{ noise \xff\x00"
    suffix = b"*\x8e\x02\x08\xff\x01 trailing \x00\xfe"
    return prefix + json.dumps(obj).encode("utf-8") + suffix


def _seed_pending_shell_gate(workspace: str, command: str) -> Path:
    """Write a cursor chat store for *workspace* holding one gated Shell call."""
    now_ms = int(time.time() * 1000)
    chat_dir = _cursor_chats_root() / _workspace_hash(workspace) / str(uuid.uuid4())
    chat_dir.mkdir(parents=True)
    (chat_dir / "meta.json").write_text(json.dumps({"createdAtMs": now_ms}), encoding="utf-8")
    user_turn = {
        "role": "user",
        "content": [
            {"type": "text", "text": "<user_query>\nDelete the build directory\n</user_query>"}
        ],
    }
    gated_call = {
        "id": "1",
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": f"call_{uuid.uuid4().hex[:12]}\nfc_1",
                "toolName": "Shell",
                "args": {"command": command},
            }
        ],
        "providerOptions": {"cursor": {"pendingToolCallStartedAtMs": now_ms}},
    }
    store = chat_dir / "store.db"
    con = sqlite3.connect(str(store))
    try:
        con.execute("CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB)")
        con.executemany(
            "INSERT INTO blobs(id, data) VALUES(?, ?)",
            [
                (hashlib.sha256(b"user").hexdigest(), json.dumps(user_turn).encode("utf-8")),
                (hashlib.sha256(b"gate").hexdigest(), _framed(gated_call)),
            ],
        )
        con.commit()
    finally:
        con.close()
    return store


def _tmux(info: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", info["socket_path"], *args], capture_output=True, text=True, timeout=10
    )


def _tmux_server_pid(info: dict[str, str]) -> int | None:
    out = _tmux(info, "display-message", "-p", "#{pid}").stdout.strip()
    return int(out) if out.isdigit() else None


def _remove_pane_socket(info: dict[str, str]) -> str:
    """Make the pane socket vanish (as a temp-dir sweep does); return tmux's error."""
    os.remove(info["socket_path"])
    probe = _tmux(info, "has-session", "-t", info["tmux_target"])
    assert probe.returncode != 0, "tmux still reaches the pane after its socket was removed"
    return probe.stderr.strip()


def _runner_log_text(session_id: str) -> str | None:
    """Return the runner process log that handled *session_id*, if one is readable."""
    logs = sorted(
        process_log_dir("runner").glob("*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in logs:
        text = path.read_text(errors="replace")
        if session_id in text:
            return text
    return None


def _keystroke_failure_excerpt(runner_log: str) -> str:
    lines = runner_log.splitlines()
    hits = [i for i, line in enumerate(lines) if _KEYSTROKE_FAILURE in line]
    return "\n".join(lines[hits[0] : hits[0] + 25]) if hits else "<no keystroke failure logged>"


@pytest.mark.timeout(300)
def test_cursor_approval_after_pane_socket_vanishes_notifies_user(
    request: pytest.FixtureRequest,
    native_cursor_approval_session: tuple[str, str],
) -> None:
    """Approving a card whose pane socket is gone must tell the user, not settle silently."""
    base_url, session_id = native_cursor_approval_session
    pane = _wait_for_cursor_pane(session_id)
    tmux_server_pid = _tmux_server_pid(pane)
    store = _seed_pending_shell_gate(os.path.realpath(str(_REPO_ROOT)), _GATED_COMMAND)
    try:
        page = request.getfixturevalue("page")
        page.goto(f"{base_url}/c/{session_id}")
        _ensure_chat_view(page)
        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
        expect(card).to_contain_text(_GATED_COMMAND)

        tmux_error = _remove_pane_socket(pane)
        print(f"tmux after socket removal: {tmux_error}")
        assert "No such file or directory" in tmux_error, tmux_error
        card.get_by_role("button", name="Approve", exact=True).click()

        responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
        expect(responded).to_be_visible(timeout=_VERDICT_TIMEOUT_MS)
        expect(responded).to_contain_text("Approved")
        notice = page.get_by_text(_UNDELIVERED_NOTICE).first
        expect(notice).to_be_visible(timeout=_VERDICT_TIMEOUT_MS)

        runner_log = _runner_log_text(session_id)
        assert runner_log is not None, f"no runner process log mentions session {session_id}"
        errors = [
            line
            for line in runner_log.splitlines()
            if _KEYSTROKE_FAILURE in line and "ERROR" in line
        ]
        assert not errors, f"dead-pane keystroke failure logged as ERROR: {errors[0]}"
    finally:
        runner_log = _runner_log_text(session_id)
        if runner_log is not None:
            print(
                f"runner log around keystroke delivery:\n{_keystroke_failure_excerpt(runner_log)}"
            )
        # The runner can no longer reach the socket-less server; stop it directly.
        if tmux_server_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(tmux_server_pid, signal.SIGTERM)
        shutil.rmtree(store.parent, ignore_errors=True)
