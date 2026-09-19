"""E2E: a cursor-native web approval on a dead pane must not vanish as a raw ERROR.

The runner-side cursor-native mirror parks a gated tool call on the
``cursor-permission-request`` hook and, on the web verdict, delivers a ``y``
keystroke into the cursor TUI's tmux pane
(``omnigent.harnesses.cursor_native.permissions._send_cursor_keys`` →
``bridge.send_cursor_pane_keys`` → ``tmux -S <socket> send-keys``). The parked
hook waits on the human for up to a day, and nothing re-checks the pane in the
meantime — so when the tmux server backing the pane dies while the card is
parked (terminal teardown, temp-dir cleanup, machine sleep), the Approve click
drives ``send-keys`` at a stale ``tmux.json`` advert and fails with
``RuntimeError: tmux command failed (rc=1): error connecting to …/tmux.sock
(No such file or directory)``. On the buggy build ``_send_cursor_keys``
swallows that into a raw ``logger.exception("failed to send cursor keystroke
…")`` ERROR — the fleet-scraped delivery-failure signature — and returns
``False``, which ``_run_one_approval`` ignores entirely.

The user-observable failure: the card flips to its "Approved" responded state
as if the verdict landed, but cursor never received the keystroke — the
approval is silently dropped, with no user-facing feedback and no structured
error reason, only the unhandled-looking ERROR + traceback in the runner log.

This test drives the REAL production path end-to-end minus the Cursor TUI
itself (CI has no Cursor login, so a live ``cursor-agent`` turn cannot run —
the same gap that makes ``test_cursor_native_approval.py`` skip, and the same
stand-in lane as ``test_cursor_multiselect_question.py``): a background thread
runs the actual runner mirror coroutine ``_run_one_approval`` with a
transcript-shaped pending Shell call, pointed at the live spawned server; a
REAL tmux server backs the pane and is advertised via the real
``write_tmux_target``. The server parks the elicitation, the SPA renders the
card in a real browser, the tmux server is killed (and its socket removed —
the reported ENOENT state) while the card is parked, and the user clicks
Approve.

The regression contract asserted: delivering a web approval verdict to a pane
whose tmux server is gone must be *handled* — a pane-liveness check, a
structured/attributed reason, and user-facing feedback in the chat — not
swallowed as the raw ``failed to send cursor keystroke`` ERROR with the
tmux-connect traceback. On the buggy build exactly that record is emitted when
Approve is clicked (and no feedback is shown), and the assertions fail —
exactly this bug.
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

# The exact message prefix of the raw delivery-failure ERROR record
# (omnigent.harnesses.cursor_native.permissions._send_cursor_keys) that the
# buggy build emits when the verdict keystroke hits a dead tmux socket.
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
    """Collect ERROR+ records emitted by the cursor-native permissions logger.

    The mirror coroutine runs in a background thread of THIS process (the same
    lane as ``test_cursor_multiselect_question.py``), so its log records are
    observable here — the process-level analog of the runner log the fleet KPI
    scrapes.
    """

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
        # The exact coroutine the cursor-native supervisor runs for a detected
        # gated tool call: POST the cursor-permission-request hook → park for
        # the web verdict → deliver the y/Escape keystroke into the pane.
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

        # THE TRIGGER: while the card is still parked, the tmux server backing
        # the cursor pane dies and its socket disappears (terminal teardown /
        # temp cleanup) — the exact ENOENT state in the reported traceback.
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

    # THE BUG: the web verdict's keystroke hit the dead socket and was swallowed
    # as a raw `failed to send cursor keystroke` ERROR — the approval silently
    # went nowhere while the card claims "Approved". A fixed build handles
    # dead-pane delivery (liveness check, structured/attributed reason) instead
    # of emitting this exact unhandled-exception signature.
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
