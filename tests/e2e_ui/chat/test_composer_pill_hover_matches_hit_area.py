"""E2E: the shared composer's highlighted model label opens configuration."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native


def test_composer_pill_highlighted_label_is_clickable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The model label shares the trigger's hover paint and click target."""
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        trigger = page.get_by_test_id("composer-config-gear")
        label = page.get_by_test_id("composer-agent-config-value")
        expect(trigger).to_be_visible(timeout=15_000)
        expect(label).to_be_visible()
        page.mouse.move(5, 5)
        background = trigger.evaluate("element => getComputedStyle(element).backgroundColor")
        label.hover()
        page.wait_for_function(
            "([element, background]) => getComputedStyle(element).backgroundColor !== background",
            arg=[trigger.element_handle(), background],
        )
        label.click()
        expect(page.get_by_test_id("composer-agent-menu")).to_be_visible()
        expect(page.get_by_test_id("composer-advanced-settings")).to_have_count(0)
        page.get_by_test_id("composer-agent-edit").click()
        expect(page.get_by_test_id("composer-agent-config-menu")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_test_id("composer-agent-config-menu")).not_to_be_visible()
    finally:
        page.unroute_all(behavior="ignoreErrors")
