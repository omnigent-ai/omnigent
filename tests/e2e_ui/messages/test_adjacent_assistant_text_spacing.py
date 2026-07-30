"""E2E: adjacent assistant text messages remain visually separated.

Native forwarders can persist multiple assistant message items under the same
``response_id``. The transcript model should keep those items in one assistant
bubble, but each logical text item needs a small visual pause so separate
received messages do not read as one undifferentiated block.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TEXT_SECTION = '[data-testid="assistant-text-section"]'
_RESPONSE_ID = "resp_adjacent_assistant_text_spacing"
_FIRST_TEXT = "First assistant section for spacing."
_SECOND_TEXT = "Second **markdown** section for spacing."


def _seed_assistant_text(base_url: str, session_id: str, text: str) -> None:
    """Append one deterministic assistant message to ``session_id``."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": _AGENT_NAME,
                "response_id": _RESPONSE_ID,
                "text": text,
            },
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def test_adjacent_assistant_text_items_have_subtle_spacing(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Two text items in one assistant bubble render as separated sections."""
    base_url, session_id = seeded_session
    _seed_assistant_text(base_url, session_id, _FIRST_TEXT)
    _seed_assistant_text(base_url, session_id, _SECOND_TEXT)

    page.goto(f"{base_url}/c/{session_id}")

    bubble = page.locator(_ASSISTANT).filter(has_text=_FIRST_TEXT)
    expect(bubble).to_have_count(1, timeout=30_000)
    expect(bubble).to_contain_text("Second markdown section for spacing.")

    sections = bubble.locator(_TEXT_SECTION)
    expect(sections).to_have_count(2)
    expect(sections.nth(0)).not_to_have_class(re.compile(r"(^| )mt-2( |$)"))
    expect(sections.nth(1)).to_have_class(re.compile(r"(^| )mt-2( |$)"))

    margin_top = sections.nth(1).evaluate("el => getComputedStyle(el).marginTop")
    assert margin_top == "8px"

    visible_gap_px = sections.nth(1).evaluate(
        """el => {
            const previous = el.previousElementSibling;
            const previousBox = previous.getBoundingClientRect();
            const currentBox = el.getBoundingClientRect();
            return Math.round(currentBox.top - previousBox.bottom);
        }"""
    )
    assert visible_gap_px >= 12

    expect(sections.nth(1).locator('[data-streamdown="strong"]')).to_have_text("markdown")
