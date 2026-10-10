"""Shared Playwright navigation helpers for the new-session composer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from playwright.async_api import Page, expect


async def wait_until(predicate: Callable[[], object], *, timeout_s: float = 15.0) -> None:
    """Poll ``predicate`` on the event loop until true or timeout.

    :param predicate: Zero-arg callable returning truthy when satisfied.
    :param timeout_s: Max seconds to wait before failing the test.
    :raises AssertionError: If the predicate never becomes truthy.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


async def stub_empty_host_picker_data(page: Page, host_id: str) -> None:
    """Answer auxiliary requests for a fake host with no catalog or worktrees."""
    await page.route(
        f"**/v1/hosts/{host_id}/harnesses/*/model-options",
        lambda route: route.fulfill(json={"models": []}),
    )
    await page.route(
        f"**/v1/hosts/{host_id}/worktrees?*",
        lambda route: route.fulfill(json={"data": []}),
    )


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
    # The closed menu's dismissal layer outlives aria-expanded and swallows the
    # next pointerdown; wait for it to unmount so a follow-up click lands.
    await expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


async def open_entry_models(page: Page, agent_id: str) -> None:
    """Select a harness and open its primary model and effort picker."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await picker.click()
    await expect(picker).to_have_attribute("aria-expanded", "true")
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        await page.get_by_test_id("new-chat-landing-harness-more").click()
    await (
        page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
        .get_by_text("Edit", exact=True)
        .click()
    )


async def close_entry_models(page: Page) -> None:
    """Dismiss the primary picker after immediate selection."""
    await page.keyboard.press("Escape")
    if (
        await page.get_by_test_id("new-chat-landing-agent-select").get_attribute("aria-expanded")
        == "true"
    ):
        await page.keyboard.press("Escape")
