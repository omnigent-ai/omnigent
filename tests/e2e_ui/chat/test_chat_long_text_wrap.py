"""E2E: a long unbroken run of prose or inline code wraps instead of overflowing."""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, ViewportSize, expect

_AGENT_NAME = "hello_world"
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

_TRANSCRIPT_SCROLLER = ".transcript-hide-native-scrollbar"
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

# Low-entropy repeated word (not a secret), long enough to overflow the column unbroken.
_LONG_PLAIN_RUN = "chatColumnOverflow" * 14
_LONG_CODE_TOKEN = "inlineCodeOverflow" * 14

_MESSAGE_TEXT = (
    f"Plain unbroken run: {_LONG_PLAIN_RUN} end.\n\n"
    f"Inline code unbroken run: `{_LONG_CODE_TOKEN}` end."
)


def _no_horizontal_overflow(selector: str) -> str:
    """JS predicate: *selector*'s match has no horizontal overflow (1px tolerance)."""
    return (
        "() => { const el = document.querySelector('"
        + selector
        + "'); return !!el && el.scrollWidth - el.clientWidth <= 1; }"
    )


def test_chat_long_unbroken_text_and_code_wrap_without_overflow(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A long unbroken plain-text run and inline-code token both wrap in place."""
    base_url, session_id = seeded_session
    event_resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": _MESSAGE_TEXT},
        },
        timeout=10.0,
    )
    event_resp.raise_for_status()

    page.set_viewport_size(_MOBILE_VIEWPORT)
    page.goto(f"{base_url}/c/{session_id}")

    bubble = page.locator(_ASSISTANT_BUBBLE).last
    expect(bubble).to_be_visible(timeout=30_000)
    expect(bubble).to_contain_text("chatColumnOverflow")

    # Confirms the token rendered as markdown inline code, not just bubble text.
    code = bubble.locator('code[data-streamdown="inline-code"]')
    expect(code).to_be_visible(timeout=30_000)
    expect(code).to_contain_text("inlineCodeOverflow")

    page.wait_for_function(_no_horizontal_overflow(_TRANSCRIPT_SCROLLER), timeout=30_000)
    page.wait_for_function(_no_horizontal_overflow(_ASSISTANT_BUBBLE), timeout=10_000)
