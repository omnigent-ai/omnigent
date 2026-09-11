"""Sending must dismiss native prediction before clearing the composer."""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

_QUEUED_PLACEHOLDER = "Send a follow-up (queued) — Esc to stop"


def _publish_running(base_url: str, session_id: str) -> None:
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "running", "response_id": "resp_native_prediction"},
        },
        timeout=10.0,
    )
    response.raise_for_status()


def test_submit_button_resets_native_text_input_when_send_moves_focus(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clear and end the textarea's native input session after sending."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    _publish_running(base_url, session_id)
    expect(composer).to_have_attribute("placeholder", _QUEUED_PLACEHOLDER, timeout=15_000)

    composer.fill("disabled")
    page.get_by_role("button", name="Send").focus()
    composer.evaluate("textarea => textarea.form?.requestSubmit()")
    expect(composer).to_have_value("")
    expect(composer).not_to_be_focused()
    expect(composer).to_have_attribute("placeholder", _QUEUED_PLACEHOLDER)
