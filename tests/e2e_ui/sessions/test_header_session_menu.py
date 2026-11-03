"""E2E coverage for the chat-header session actions menu.

The owner path exercises the real REST-backed rename flow from the header.
The child path proves the owner menu is replaced by a Fork-only menu.
"""

from __future__ import annotations

import sys

import httpx
from playwright.sync_api import Page, expect


def test_rename_hotkey_from_composer_persists_and_cancels(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The shortcut edits the current title without losing the chat draft."""
    base_url, session_id = seeded_session
    original_title = "Session before rename"
    response = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": original_title},
        timeout=10.0,
    )
    response.raise_for_status()
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Keep this draft while renaming")
    modifier = "Meta" if sys.platform == "darwin" else "Control"
    page.keyboard.press(f"{modifier}+Alt+r")

    rename_input = page.get_by_role("textbox", name="Session name")
    expect(rename_input).to_be_focused()
    expect(rename_input).to_have_value(original_title)
    assert rename_input.evaluate("el => [el.selectionStart, el.selectionEnd]") == [
        0,
        len(original_title),
    ]
    renamed_title = "Session renamed with the keyboard"
    page.keyboard.type(renamed_title)
    expect(rename_input).to_have_value(renamed_title)
    rename_input.press("Enter")

    expect(page.get_by_test_id("header-title")).to_have_text(renamed_title)
    expect(composer).to_have_value("Keep this draft while renaming")
    page.reload()
    expect(page.get_by_test_id("header-title")).to_have_text(renamed_title)

    composer.click()
    page.keyboard.press(f"{modifier}+Alt+r")
    expect(rename_input).to_be_focused()
    rename_input.fill("Cancel this rename")
    rename_input.press("Escape")
    expect(rename_input).to_have_count(0)
    expect(page.get_by_test_id("header-title")).to_have_text(renamed_title)
    expect(composer).to_have_value("Keep this draft while renaming")

    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    snapshot.raise_for_status()
    assert snapshot.json()["title"] == renamed_title


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
    """Owner menu renames the session; children get only Fork."""
    base_url, session_id = seeded_session
    child_id: str | None = None

    try:
        page.goto(f"{base_url}/c/{session_id}")

        trigger = page.get_by_test_id("header-conversation-actions")
        expect(trigger).to_be_visible(timeout=30_000)
        trigger.click()

        # Desktop keeps "Rename" and "Add to project" in the kebab, alongside the
        # breadcrumb title (HeaderTitle) and folder tag (HeaderProjectTag)
        # shortcuts. "Add to project" is a submenu trigger, so its label carries
        # the flyout's own items; match only the leading action label.
        menu_items = page.get_by_role("menuitem")
        expect(menu_items).to_have_count(8)
        labels = [text.split("\n")[0] for text in menu_items.all_inner_texts()]
        assert labels == [
            "Pin",
            "Fork",
            "Export",
            "Rename",
            "Mark as unread",
            "Add to project",
            "Archive",
            "Delete",
        ]
        # Dismiss the menu and wait for it to fully detach — Radix briefly puts
        # `pointer-events: none` on the body while closing, which would swallow
        # the title click that follows.
        page.keyboard.press("Escape")
        expect(page.get_by_role("menu")).to_have_count(0)

        # Rename by clicking the breadcrumb title → inline input, Enter commits.
        breadcrumb = page.get_by_role("navigation", name="Conversation")
        page.get_by_test_id("header-title").click()
        rename_input = page.get_by_role("textbox", name="Session name")
        expect(rename_input).to_be_visible()
        renamed_title = "Header title renamed session"
        rename_input.fill(renamed_title)
        rename_input.press("Enter")

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
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        page.keyboard.press(f"{modifier}+Alt+r")
        expect(page.get_by_role("textbox", name="Session name")).to_have_count(0)
        child_trigger = page.get_by_test_id("desktop-fork-actions-menu")
        expect(child_trigger).to_be_visible()
        child_trigger.click()
        expect(page.get_by_role("menuitem")).to_have_count(1)
        expect(page.get_by_role("menuitem", name="Fork", exact=True)).to_be_visible()
    finally:
        if child_id is not None:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
