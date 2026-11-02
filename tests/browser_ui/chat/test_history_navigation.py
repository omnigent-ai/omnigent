"""Browser-contract proof for the sidebar's Back and Forward buttons.

They step through the Navigation API, which jsdom lacks, so only a real browser
exercises the path that ships.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


def test_back_and_forward_step_between_screens_without_leaving_the_app(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Session → Home, then Back and Forward retrace it; Back stops at the first screen."""
    chat = chat_session_contract
    # Home's composer lists the host's folders.
    chat.contract.json(f"/v1/hosts/{chat.host_id}/filesystem", {"path": "/", "entries": []})
    page.goto(chat.url)
    header = page.locator('.conversations-sidebar [data-testid="sidebar-header-actions"]')
    back = header.get_by_role("button", name="Back", exact=True)
    forward = header.get_by_role("button", name="Forward", exact=True)
    expect(back).to_be_visible(timeout=30_000)
    expect(back).to_have_attribute("aria-disabled", "true")
    expect(forward).to_have_attribute("aria-disabled", "true")

    page.get_by_test_id("sidebar-brand").click()
    expect(page).to_have_url(re.compile(r"/$"))
    expect(back).not_to_have_attribute("aria-disabled", "true")

    back.click()
    expect(page).to_have_url(chat.url)
    expect(back).to_have_attribute("aria-disabled", "true")
    expect(forward).not_to_have_attribute("aria-disabled", "true")

    forward.click()
    expect(page).to_have_url(re.compile(r"/$"))
    expect(forward).to_have_attribute("aria-disabled", "true")
