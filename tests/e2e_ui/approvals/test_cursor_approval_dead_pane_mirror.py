"""E2E (UI): approving a Cursor card after its pane died settles it and shows a notice.

Runs the real ``_run_one_approval`` coroutine against the live spawned server
with a real tmux server behind the pane; that server is killed and its socket
removed while the card is parked, then the user clicks Approve. Needs tmux; no
Cursor login.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.cursor_native.bridge import write_tmux_target
from omnigent.harnesses.cursor_native.permissions import (
    CursorPendingToolCall,
    _prompt_from_pending,
    _run_one_approval,
    cursor_tool_call_elicitation_id,
)

_APPROVAL_CARD = '[data-testid="approval-card"]'

_MOCK_ELICITATION_TIMEOUT_MS = 15_000

# Message prefix of the raw delivery-failure ERROR this test must not observe.
_KEYSTROKE_ERROR_PREFIX = "failed to send cursor keystroke"

# The gated call the mirror detects in cursor's store.db — shaped exactly like
# read_cursor_pending_tool_calls yields it for a gated Shell command.
_PENDING_CALL = CursorPendingToolCall(
    tool_call_id="toolu_dead_pane_approval",
    tool_name="Shell",
    args={"command": "rm -rf build/"},
)

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="cursor-native keystroke delivery requires tmux"
)


class _ErrorRecorder(logging.Handler):
    """Collect ERROR records from the in-process permissions logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _tmux(socket_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``tmux -S <socket> <args…>`` capturing output (no check)."""
    return subprocess.run(
        ["tmux", "-S", str(socket_path), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.timeout(120)
def test_cursor_approval_on_dead_pane_is_not_silently_dropped(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Approving a cursor card after its pane died must not drop the verdict raw.

    Drives: pending cursor approval card in the web UI → the pane's tmux
    server dies (socket removed) while the card is parked → the user clicks
    Approve. Asserts the verdict's keystroke delivery is not swallowed as the
    raw ``failed to send cursor keystroke`` ERROR, and that the user is told
    in the chat that the response could not be delivered.
    """
    base_url, session_id = seeded_session

    # A REAL tmux server backs the cursor pane; the bridge dir advertises it
    # exactly the way the cursor-native launch does (write_tmux_target).
    socket_path = tmp_path / "tmux.sock"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    tmux_session = "cursor-dead-pane"
    proc = _tmux(socket_path, "new-session", "-d", "-s", tmux_session, "sleep 300")
    assert proc.returncode == 0, f"tmux server failed to start: {proc.stderr}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target=tmux_session)

    elicitation_id = cursor_tool_call_elicitation_id(session_id, _PENDING_CALL.tool_call_id)

    recorder = _ErrorRecorder()
    permissions_logger = logging.getLogger("omnigent.harnesses.cursor_native.permissions")
    permissions_logger.addHandler(recorder)

    mirror_result: dict[str, BaseException] = {}

    async def _mirror() -> None:
        # Run the supervisor's real approval coroutine against the live server.
        async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
            await _run_one_approval(
                client,
                session_id=session_id,
                bridge_dir=bridge_dir,
                prompt=_prompt_from_pending(_PENDING_CALL),
                elicitation_id=elicitation_id,
            )

    def _run_mirror() -> None:
        try:
            asyncio.run(_mirror())
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            mirror_result["error"] = exc

    thread = threading.Thread(target=_run_mirror, daemon=True)
    thread.start()
    try:
        page.goto(f"{base_url}/c/{session_id}")

        # The gated Shell call surfaces as a pending approval card.
        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
        expect(card.get_by_text("Cursor wants to run Shell")).to_be_visible()

        # While the card is parked, kill the pane's tmux server and remove its
        # socket (terminal teardown / temp cleanup).
        assert _tmux(socket_path, "kill-server").returncode == 0
        socket_path.unlink(missing_ok=True)
        assert _tmux(socket_path, "has-session", "-t", tmux_session).returncode != 0, (
            "tmux server should be dead before the verdict is delivered"
        )

        # The user approves from the web UI.
        card.get_by_role("button", name="Approve").click()

        # The card settles as answered — from the web UI the approval "worked".
        responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
        expect(responded).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
        expect(responded).to_contain_text("Approved")

        # The verdict reached the mirror and its delivery attempt finished.
        thread.join(timeout=30)
    finally:
        if thread.is_alive():
            # A card assertion failed before a verdict was submitted — resolve
            # the parked elicitation so the mirror exits instead of parking for
            # the hook timeout.
            with contextlib.suppress(httpx.HTTPError):
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/events",
                    json={
                        "type": "approval",
                        "data": {"elicitation_id": elicitation_id, "action": "decline"},
                    },
                    timeout=10.0,
                ).raise_for_status()
            thread.join(timeout=30)
        permissions_logger.removeHandler(recorder)
        _tmux(socket_path, "kill-server")  # no-op when already dead

    assert not thread.is_alive(), "cursor approval mirror never received a web verdict"
    if "error" in mirror_result:
        raise AssertionError(
            f"cursor approval mirror failed: {mirror_result['error']}"
        ) from mirror_result["error"]  # type: ignore[misc]

    # Dead-pane delivery must be attributed, never logged as this raw ERROR.
    dropped = [
        record
        for record in recorder.records
        if record.getMessage().startswith(_KEYSTROKE_ERROR_PREFIX)
    ]
    assert not dropped, (
        "cursor approval verdict was silently dropped on the dead pane: "
        + "; ".join(
            f"{record.levelname} {record.name}: {record.getMessage()}"
            + (f" [exc: {record.exc_info[1]!r}]" if record.exc_info else "")
            for record in dropped
        )
    )

    # And the drop must not be invisible: the user is told in the chat that
    # their response never reached cursor (the card already reads as answered,
    # so feedback is the only signal the approval did not land).
    expect(page.get_by_text("could not be delivered").first).to_be_visible(
        timeout=_MOCK_ELICITATION_TIMEOUT_MS
    )
    expect(page.get_by_text("no longer running").first).to_be_visible(
        timeout=_MOCK_ELICITATION_TIMEOUT_MS
    )
