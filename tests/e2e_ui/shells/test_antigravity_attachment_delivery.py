"""An attachment-carrying chat turn must reach the real agy TUI even though agy
word-wraps the long ``[Attached: <path>]`` line at its pane width. Run with
``OMNIGENT_E2E_ANTIGRAVITY=mock`` to use the local mock Gemini backend."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.fork_session.test_fork_preserves_image_attachment import _write_png
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import (  # noqa: F401
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

_COMPOSER_LABEL = "Message the agent"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_AGY_IDLE_FOOTER = "? for shortcuts"


def _pane_text(session: AntigravitySession) -> str:
    return session.tmux_command("capture-pane", "-p", "-t", session.pane()["tmux_target"]).stdout


def _pane_size(session: AntigravitySession) -> str:
    target = session.pane()["tmux_target"]
    return session.tmux_command(
        "display-message", "-p", "-t", target, "#{pane_width}x#{pane_height}"
    ).stdout.strip()


def _send_attachment_turn(page: Page, png: Path, text: str) -> None:
    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(png))
    expect(page.get_by_role("button", name=f"Remove {png.name}")).to_be_visible(timeout=10_000)
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    send = page.get_by_role("button", name="Send", exact=True)
    expect(send).to_be_enabled(timeout=10_000)
    send.click()


def _wait_for_turn_outcome(
    page: Page, session: AntigravitySession, token: str, timeout_s: float
) -> str:
    """Return the delivery-error text, or ``""`` once a reply carrying *token* lands.
    Matching on *token* keeps the optimistic in-flight assistant bubble from being
    mistaken for a reply that raced the error pill."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pill = page.locator(_ERROR_PILL)
        if pill.count() > 0:
            pill.first.click()
            page.wait_for_timeout(500)
            return pill.first.inner_text()
        if page.locator(_ASSISTANT).filter(has_text=token).count() > 0:
            return ""
        page.wait_for_timeout(1_000)
    raise AssertionError(
        f"neither a reply nor a delivery error appeared within {timeout_s}s; agy pane:\n"
        f"{_pane_text(session)}"
    )


def test_attachment_turn_from_chat_view_is_delivered(
    page: Page,
    antigravity_session: AntigravitySession,  # noqa: F811  (imported fixture)
    antigravity_model: list[str] | None,  # noqa: F811  (imported fixture)
    tmp_path: Path,
) -> None:
    session = antigravity_session
    png = _write_png(tmp_path)
    token = f"agy-e2e-{uuid.uuid4().hex[:8]}"

    page.goto(f"{session.base_url}/c/{session.session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=120_000)
    _wait_until(
        lambda: _AGY_IDLE_FOOTER in _pane_text(session), "agy never showed its input box", 120
    )
    pane_size = _pane_size(session)

    _send_attachment_turn(page, png, f"Reply {token}. No tools.")

    error_text = _wait_for_turn_outcome(page, session, token, timeout_s=120)
    assert not error_text, (
        f"attachment turn was not delivered to agy (pane {pane_size}):\n{error_text}\n"
        f"--- agy pane ---\n{_pane_text(session)}"
    )
    expect(page.locator(_ASSISTANT).filter(has_text=token)).to_have_count(1)
    if antigravity_model is not None:
        assert token in antigravity_model, "agy did not request this reply from the mock model"
    expect(page.locator(_ERROR_PILL)).to_have_count(0)
