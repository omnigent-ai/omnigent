"""Shared Playwright navigation helpers for the new-session composer."""

from __future__ import annotations

from playwright.async_api import Page, expect


async def open_landing_workspace_picker(page: Page) -> None:
    """Open the second-stage filesystem picker from the workspace recents menu."""
    await page.get_by_test_id("new-chat-landing-workspace-chip").click()
    open_folder = page.get_by_test_id("new-chat-landing-workspace-open-folder")
    await expect(open_folder).to_be_visible()
    await open_folder.click()
    await expect(page.get_by_test_id("workspace-picker")).to_be_visible()


async def commit_landing_workspace_picker(page: Page) -> None:
    """Commit the currently browsed directory back to the landing composer."""
    await page.get_by_test_id("workspace-picker-select").click()
    await expect(page.get_by_test_id("workspace-picker")).to_be_hidden()


async def select_landing_agent(page: Page, agent_id: str) -> None:
    """Select an agent and dismiss its integrated configuration menu layer."""
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    await trigger.click()
    option = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    await expect(option).to_be_visible(timeout=60_000)
    await option.click()

    # The selected row becomes the integrated model/effort/config submenu.
    # Radix can preserve the root layer across that rerender, so close it before
    # interacting with composer controls behind the modal overlay.
    if await trigger.get_attribute("aria-expanded") == "true":
        await page.keyboard.press("Escape")
    await expect(trigger).to_have_attribute("aria-expanded", "false")
