"""E2E coverage for the chat-header session actions menu.

The owner path exercises the real REST-backed rename flow from the header.
The child path proves sub-agent breadcrumbs stay navigation-only.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def test_header_session_menu_always_shows_horizontal_ellipsis(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The desktop trigger stays visible and uses a horizontal ellipsis."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    trigger = page.get_by_test_id("header-conversation-actions")
    expect(trigger).to_be_visible(timeout=30_000)

    expect(trigger).to_have_css("opacity", "1")
    expect(trigger.locator("svg.lucide-ellipsis")).to_have_count(1)
    expect(trigger.locator("svg.lucide-ellipsis-vertical")).to_have_count(0)

    trigger.click()
    viewport = page.viewport_size
    assert viewport is not None
    page.mouse.move(viewport["width"] - 4, viewport["height"] - 4)

    expect(page.get_by_role("menu")).to_be_visible()
    expect(trigger).to_have_css("opacity", "1")


def test_header_session_menu_renames_owner_and_hides_for_subagent(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Owner menu opens in order, renames the session, and stays off children."""
    base_url, session_id = seeded_session
    child_id: str | None = None

    try:
        page.goto(f"{base_url}/c/{session_id}")

        trigger = page.get_by_test_id("header-conversation-actions")
        expect(trigger).to_be_visible(timeout=30_000)
        trigger.click()

        menu_items = page.get_by_role("menuitem")
        expect(menu_items).to_have_count(6)
        assert menu_items.all_inner_texts() == [
            "Pin",
            "Rename",
            "Mark as unread",
            "Add to project",
            "Archive",
            "Delete",
        ]

        page.get_by_role("menuitem", name="Rename").click()
        rename_input = page.get_by_role("textbox", name="Session name")
        expect(rename_input).to_be_visible()
        renamed_title = "Header menu renamed session"
        rename_input.fill(renamed_title)
        page.get_by_role("button", name="Rename").click()

        breadcrumb = page.get_by_role("navigation", name="Conversation")
        expect(breadcrumb.get_by_text(renamed_title, exact=True)).to_be_visible(timeout=15_000)

        agent_resp = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/agent",
            timeout=10.0,
        )
        agent_resp.raise_for_status()
        child_resp = httpx.post(
            f"{base_url}/v1/sessions",
            json={
                "agent_id": agent_resp.json()["id"],
                "parent_session_id": session_id,
                "title": "header-menu-child",
            },
            timeout=10.0,
        )
        child_resp.raise_for_status()
        child_id = str(child_resp.json()["id"])

        page.goto(f"{base_url}/c/{child_id}")

        expect(page.get_by_role("link", name="Back to parent session")).to_be_visible(
            timeout=30_000
        )
        expect(page.get_by_test_id("header-conversation-actions")).to_have_count(0)
    finally:
        if child_id is not None:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
