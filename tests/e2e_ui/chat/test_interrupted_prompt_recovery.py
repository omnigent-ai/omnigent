"""Browser e2e: an early interrupt restores the submitted prompt."""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm


def _wait_for_blocked_turn(mock_url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if httpx.get(f"{mock_url}/gate/pending", timeout=5).json()["pending"]:
            return
        time.sleep(0.1)
    raise AssertionError("LLM request did not reach the blocking gate")


def test_escape_during_early_turn_restores_exact_prompt(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Escape restores and focuses the prompt before assistant output starts."""
    base_url, session_id = seeded_session
    prompt = f"adjust this plan\nthen resend it {uuid.uuid4().hex}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "This response should be interrupted.", "block": True}],
        match=prompt,
    )

    try:
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_visible()
        composer.fill(prompt)
        page.get_by_role("button", name="Send", exact=True).click()

        expect(page.get_by_role("button", name="Interrupt", exact=True)).to_be_visible()
        _wait_for_blocked_turn(mock_llm_server_url)
        composer.press("Escape")

        expect(composer).to_have_value(prompt, timeout=30_000)
        expect(composer).to_be_focused()
    finally:
        # A failed assertion must not leave the shared runner blocked.
        httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5)
