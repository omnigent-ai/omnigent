"""E2E: a drill-in page of the mobile model picker opens at the top of a scrolled list.

Once the menu is capped at the iOS safe area, the new-chat screen's harness
list no longer fits a notched iPhone and scrolls. The advanced-settings page
(Edit on the selected harness) and the "Other..." page replace that list inside
the same scroll container, so a list scrolled to its end used to open them with
their Back row above the menu's visible edge. The iOS shell, insets and catalogs
are emulated as in ``test_model_advanced_settings_safe_area``.
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.mobile.test_model_advanced_settings_safe_area import (
    _CODEX_AGENT_ID,
    _IOS_SHELL_INIT_SCRIPT,
    _IPHONE_SAFE_AREA,
    _IPHONE_VIEWPORT,
    _settled_box,
    _stub_landing_catalog,
)


def _open_list(page: Page) -> tuple[Locator, Locator]:
    """Open the harness picker on the new-chat landing.

    :param page: Playwright page on the new-chat landing.
    :returns: The picker trigger and the open dropdown content showing the list.
    """
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    expect(picker).to_be_visible(timeout=30_000)
    expect(picker).to_be_enabled()
    picker.click()
    expect(picker).to_have_attribute("aria-expanded", "true")
    menu = page.locator('[data-slot="dropdown-menu-content"][data-state="open"]')
    expect(menu).to_be_visible()
    return picker, menu


def _scroll_list_to_end(page: Page, menu: Locator) -> int:
    """Wheel-scroll the harness list as far as it goes.

    :param page: Playwright page.
    :param menu: The open menu holding the harness list.
    :returns: The resulting scroll offset, which must be positive.
    """
    menu.hover()
    state = {"top": 0, "max": 0}
    for _ in range(40):
        page.mouse.wheel(0, 400)
        state = menu.evaluate(
            "el => ({top: el.scrollTop, max: el.scrollHeight - el.clientHeight})"
        )
        if state["max"] > 0 and state["top"] >= state["max"]:
            return state["top"]
        page.wait_for_timeout(50)
    if state["max"] <= 0:
        raise AssertionError(f"the harness list never overflowed the capped menu: {state}")
    raise AssertionError(f"the harness list did not scroll to its end: {state}")


@pytest.mark.parametrize("page_name", ["advanced settings", "Other..."])
def test_drill_in_page_opens_at_top_of_scrolled_list(
    request: pytest.FixtureRequest, live_server: str, page_name: str
) -> None:
    """A page opened from a fully scrolled harness list shows its Back row at the top.

    :param request: Pytest request, used to open the recorded page after setup.
    :param live_server: Base URL of the e2e server.
    :param page_name: Which drill-in page to open.
    :returns: None.
    """
    page: Page = request.getfixturevalue("page")
    if page.viewport_size != _IPHONE_VIEWPORT:
        page.set_viewport_size(_IPHONE_VIEWPORT)
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": _IPHONE_SAFE_AREA})
    _stub_landing_catalog(page)

    try:
        page.goto(f"{live_server}/")
        expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
        expect(page.locator(".app-shell").first).to_have_attribute("data-ios-native", "true")

        picker, menu = _open_list(page)
        if page_name == "advanced settings":
            # Edit on a harness other than the selected one also selects it and
            # rebuilds the menu, so select Codex first and reopen the list.
            page.get_by_test_id(f"new-chat-landing-agent-{_CODEX_AGENT_ID}").click()
            expect(picker).to_have_attribute("aria-expanded", "false")
            picker, menu = _open_list(page)
            trigger = page.get_by_test_id(f"new-chat-landing-agent-config-{_CODEX_AGENT_ID}")
        else:
            trigger = page.get_by_test_id("new-chat-landing-harness-more")
        scrolled = _scroll_list_to_end(page, menu)
        assert scrolled > 0

        expect(trigger).to_be_visible()
        # Capture the list's offset at the moment of the tap: the page must open
        # from the scrolled list, not from one the click driver scrolled back.
        menu.evaluate(
            "el => el.addEventListener('pointerdown', "
            "() => { window.__scrollAtTap = el.scrollTop; }, {capture: true, once: true})"
        )
        trigger.click()
        back = page.get_by_test_id("new-chat-landing-page-back")
        expect(back).to_be_visible()
        scroll_at_tap = page.evaluate("() => window.__scrollAtTap")
        assert scroll_at_tap is not None, "no pointerdown reached the menu before the page opened"
        assert scroll_at_tap == scrolled, (
            "the harness list was no longer at its end when the control was tapped"
        )
        menu_box = _settled_box(page, menu)
        back_box = _settled_box(page, back)
        # Hold the open page so the outcome is readable in a recording.
        if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
            page.wait_for_timeout(1_000)

        assert back_box["y"] >= menu_box["y"], (
            f"the {page_name} page opened scrolled: its Back row starts at "
            f"y={back_box['y']:.0f}px, above the menu's top edge at y={menu_box['y']:.0f}px"
        )
        assert menu.evaluate("el => el.scrollTop") == 0, (
            f"the {page_name} page inherited the list's scroll offset"
        )
    finally:
        page.unroute_all(behavior="ignoreErrors")
