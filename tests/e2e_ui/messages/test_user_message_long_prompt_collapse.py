"""E2E: long user prompts collapse by default in the chat transcript.

When a user sends a message longer than the threshold the UI
collapses the bubble to a fixed-height preview and shows an expand button.
Clicking the button expands the full text; clicking it again collapses.

Selectors:
  - user bubble:   ``data-testid="message-bubble"`` + ``data-role="user"``
  - expand button: accessible name matching /show full prompt/i
  - collapse button: accessible name matching /collapse prompt/i
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER_PLACEHOLDER = "Send a message…"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_THRESHOLD = 12_000

# Generate a prompt just over the threshold.
_LONG_PROMPT = "word " * (_THRESHOLD // 5 + 10)


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_long_user_prompt_collapses_by_default(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A prompt over THRESHOLD chars starts collapsed with an expand button visible.

    A failure here means the collapse threshold, initial state, or the button
    label regressed.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, _LONG_PROMPT)

    bubble = page.locator(_USER_BUBBLE).last
    expect(bubble).to_be_visible(timeout=15_000)

    # Expand button must be present immediately after send.
    expand_btn = bubble.get_by_role("button", name="Show full prompt", exact=False)
    expect(expand_btn).to_be_visible(timeout=10_000)

    # Collapse button must NOT be visible while still collapsed.
    collapse_btn = bubble.get_by_role("button", name="Collapse prompt", exact=True)
    expect(collapse_btn).not_to_be_visible()


def test_long_user_prompt_expands_on_click(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking the expand button shows the full text and swaps button labels.

    A failure means the click handler or state toggle regressed.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, _LONG_PROMPT)

    bubble = page.locator(_USER_BUBBLE).last
    expect(bubble).to_be_visible(timeout=15_000)

    expand_btn = bubble.get_by_role("button", name="Show full prompt", exact=False)
    expect(expand_btn).to_be_visible(timeout=10_000)
    expand_btn.click()

    # After expanding: collapse button appears, expand button disappears.
    collapse_btn = bubble.get_by_role("button", name="Collapse prompt", exact=True)
    expect(collapse_btn).to_be_visible(timeout=5_000)
    expect(expand_btn).not_to_be_visible()


def test_long_user_prompt_collapses_again_on_second_click(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking collapse restores the collapsed state.

    A failure means the toggle is one-way only.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, _LONG_PROMPT)

    bubble = page.locator(_USER_BUBBLE).last
    expect(bubble).to_be_visible(timeout=15_000)

    # Expand then collapse.
    expand_btn = bubble.get_by_role("button", name="Show full prompt", exact=False)
    expect(expand_btn).to_be_visible(timeout=10_000)
    expand_btn.click()

    collapse_btn = bubble.get_by_role("button", name="Collapse prompt", exact=True)
    expect(collapse_btn).to_be_visible(timeout=5_000)
    collapse_btn.click()

    # Should be back to collapsed: expand button reappears, collapse disappears.
    expect(expand_btn).to_be_visible(timeout=5_000)
    expect(collapse_btn).not_to_be_visible()


def test_short_user_prompt_never_collapses(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A short prompt renders fully with no collapse button present.

    Guards against accidentally collapsing every message.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    short_prompt = "This is a short message."
    _send(page, short_prompt)

    bubble = page.locator(_USER_BUBBLE).last
    expect(bubble).to_be_visible(timeout=15_000)
    expect(bubble).to_contain_text(short_prompt)

    # Neither collapse-related button should be present.
    expect(bubble.get_by_role("button", name="Show full prompt", exact=False)).not_to_be_visible()
    expect(bubble.get_by_role("button", name="Collapse prompt", exact=True)).not_to_be_visible()
