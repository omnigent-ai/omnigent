"""E2E: monospace punctuation renders without Geist contextual alternates."""

from __future__ import annotations

from playwright.sync_api import Page, expect

_COMPOSER = "Message the agent"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_COMMAND = "omnigent host --server ..."


def test_inline_code_disables_geist_punctuation_ligatures(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Inline code keeps adjacent hyphens and periods in normal glyph positions."""
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

    styles = code.evaluate(
        """el => {
          const style = getComputedStyle(el);
          return {
            featureSettings: style.fontFeatureSettings,
            variantLigatures: style.fontVariantLigatures,
          };
        }"""
    )
    assert styles["variantLigatures"] == "none"
    assert '"liga" 0' in styles["featureSettings"]
    assert '"calt" 0' in styles["featureSettings"]
