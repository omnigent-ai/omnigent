"""E2E: inline code keeps punctuation runs visually separated."""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER = "Message the agent"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_COMMAND = "abc--def abc...def"


def test_inline_code_adds_tracking_for_punctuation_runs(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Inline code retains spacing around adjacent hyphens and periods."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label(_COMPOSER)
    expect(composer).to_be_enabled(timeout=30_000)
    composer.fill(f"Run `{_COMMAND}`")
    page.get_by_role("button", name="Send", exact=True).click()

    bubble = page.locator(_USER_BUBBLE).last
    code = bubble.locator('code[data-streamdown="inline-code"]')
    expect(code).to_be_visible(timeout=30_000)
    expect(code).to_have_text(_COMMAND)

    letter_spacing = code.evaluate("el => getComputedStyle(el).letterSpacing")
    assert letter_spacing.endswith("px")
    assert float(letter_spacing.removesuffix("px")) > 0
