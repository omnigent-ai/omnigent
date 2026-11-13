"""Browser journey: a chat-view turn carrying an image attachment reaches agy.

Run without credentials using the local mock Gemini server::

    OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest \\
        tests/e2e_ui/shells/test_antigravity_attachment_delivery.py -v --ui-skip-build

The executor renders a web attachment as a single ``[Attached: <absolute path>]``
first line. agy word-wraps that line at its own pane width (the runner-owned
tmux pane is narrow until a wide terminal view attaches), and the bridge must
still recognise the pasted draft and submit the turn.
"""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect

from omnigent.harnesses.antigravity_native.bridge import (
    _AGY_IDLE_MARKER,
    _format_pane_debug_tail,
)
from tests.e2e_ui.fork_session.test_fork_preserves_image_attachment import _PNG_B64
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import (  # noqa: F401 (fixtures)
    AntigravitySession,
    _wait_until,
    antigravity_model,
    antigravity_session,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") not in {"1", "mock"},
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (no credentials) or 1 (live model)",
    ),
    pytest.mark.timeout(600),
]

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
# Long and space-free so the materialized '[Attached: <path>]' line exceeds the
# chat view's attached pane width and agy wraps it after the colon.
_SCREENSHOT_NAME = "omnigent-web-composer-image-attachment-screenshot-1280x720-chat-view.png"
# Shortest possible composer row for that attachment; a narrower pane must wrap it.
_WRAP_FORCING_WIDTH = len(f"> [Attached: /attachments/{'0' * 32}/{_SCREENSHOT_NAME}]")


def _pane_text(session: AntigravitySession) -> str:
    captured = session.tmux_command("capture-pane", "-p", "-t", session.pane()["tmux_target"])
    assert captured.returncode == 0, f"tmux capture-pane failed: {captured.stderr.strip()}"
    return captured.stdout


def _pane_size(session: AntigravitySession) -> str:
    size = session.tmux_command(
        "display-message",
        "-p",
        "-t",
        session.pane()["tmux_target"],
        "#{pane_width}x#{pane_height}",
    )
    return size.stdout.strip() if size.returncode == 0 else "unknown"


def test_attachment_turn_from_chat_view_is_delivered(
    request: pytest.FixtureRequest,
    browser: Browser,  # session-scoped: launches before the stack isolates HOME
    antigravity_session: AntigravitySession,  # noqa: F811  (imported fixture)
    antigravity_model: list[str] | None,  # noqa: F811  (imported fixture)
    tmp_path: Path,
) -> None:
    """Attach an image, type a short text and send from the chat view only."""
    session = antigravity_session
    png = tmp_path / _SCREENSHOT_NAME
    png.write_bytes(base64.b64decode(_PNG_B64))
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"
    _wait_until(
        lambda: _AGY_IDLE_MARKER in _pane_text(session),
        "agy did not render its idle input box",
        timeout=120,
    )

    # Open the recorded page only once agy is idle so the clip starts on the journey.
    page = request.getfixturevalue("page")
    try:
        page.goto(f"{session.base_url}/c/{session.session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=60_000)
        page.locator('input[type="file"][accept*="image/"]').set_input_files(str(png))
        expect(page.get_by_role("button", name=f"Remove {png.name}")).to_be_visible(timeout=10_000)
        composer.fill(f"Reply {token}. No tools.")
        pane_size = _pane_size(session)
        width = int(pane_size.split("x")[0]) if "x" in pane_size else 0
        assert 0 < width < _WRAP_FORCING_WIDTH, (
            f"pane {pane_size} must be narrower than {_WRAP_FORCING_WIDTH} columns so agy "
            "wraps the attachment line; otherwise this test passes without exercising the bug"
        )
        page.get_by_role("button", name="Send", exact=True).click()

        error_pill = page.get_by_test_id("error-pill")
        # Match the reply on the token so an in-flight assistant bubble cannot pass
        # for a reply that raced the delivery error.
        reply = page.locator(_ASSISTANT).filter(has_text=token)
        expect(reply.or_(error_pill).first).to_be_visible(timeout=120_000)
        if not error_pill.count():
            expect(reply).to_have_count(1)
            expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)
        # Re-check once the turn settled so a late error pill still gets the diagnostics.
        if error_pill.count():
            error_pill.first.click()
            message = page.get_by_test_id("error-message-content")
            expect(message).to_be_visible(timeout=10_000)
            # Hold the expanded error so a recording of the journey shows it.
            page.wait_for_timeout(1_500)
            page.screenshot(path=str(tmp_path / "delivery-error.png"))
            pytest.fail(
                f"the attachment turn was not delivered to agy (pane {pane_size}):\n"
                f"{message.inner_text()}\n--- agy pane (redacted tail) ---\n"
                f"{_format_pane_debug_tail(_pane_text(session))}"
            )
        # Hold the delivered reply so a recording of the journey shows it.
        page.wait_for_timeout(1_500)
    finally:
        # A dynamically requested page is not closed before fixture teardown.
        page.context.close()
    if antigravity_model is not None:
        assert token in antigravity_model, "agy did not request this reply from the mock model"
