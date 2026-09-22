"""Fork dialog text selection must not activate a sidebar session drag."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn


@pytest.mark.parametrize("entrypoint", ["sidebar", "response"])
def test_fork_dialog_text_selection(
    page: Page,
    seeded_session: tuple[str, str],
    entrypoint: str,
) -> None:
    """Select the advanced label with the mouse, then toggle it by click and Enter."""
    base_url, session_id = seeded_session
    marker = f"fork-selection-{entrypoint}"
    seed_committed_turn(session_id, prompt="Check fork settings", reply=marker)
    page.goto(f"{base_url}/c/{session_id}")
    response = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=marker
    )
    expect(response).to_be_visible(timeout=60_000)

    if entrypoint == "sidebar":
        row = page.locator(f'[data-sidebar-session-id="{session_id}"]')
        row.hover()
        row.get_by_test_id("conversation-actions").click()
        page.get_by_test_id("fork-conversation").click()
    else:
        response.hover()
        response.get_by_test_id("fork-from-response").click()

    toggle = page.get_by_test_id("fork-session-advanced-toggle")
    expect(toggle).to_be_visible()
    toggle.scroll_into_view_if_needed()
    bounds = toggle.evaluate(
        """button => {
          const text = [...button.childNodes].find(
            node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()
          );
          const range = document.createRange();
          range.selectNodeContents(text);
          return range.getBoundingClientRect().toJSON();
        }"""
    )
    y = bounds["y"] + bounds["height"] / 2
    page.mouse.move(bounds["x"] + 1, y)
    page.mouse.down()
    page.mouse.move(bounds["x"] + bounds["width"] - 1, y, steps=20)
    page.mouse.up()
    assert page.evaluate("window.getSelection().toString()") == "Advanced settings"

    expanded = toggle.get_attribute("aria-expanded")
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false" if expanded == "true" else "true")
    toggle.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", expanded)
