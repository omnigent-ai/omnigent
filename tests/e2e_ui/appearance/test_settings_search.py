"""UI e2e: Settings search, from its field in the Settings sidebar and from ⌘K.

Typing finds a catalogued row; picking a result opens its section, scrolls the row into view,
highlights it briefly and focuses its control. ⌘K's "Search settings…" opens Settings with the
field focused.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_HIGHLIGHT = "data-settings-highlight"


def _search_field(page: Page):
    return page.get_by_role("combobox", name="Search settings")


def _expect_revealed(page: Page, row_id: str) -> None:
    """The row is on screen and highlighted, and the highlight then fades."""
    row = page.locator(f'[data-settings-item="{row_id}"]')
    expect(row).to_be_in_viewport()
    expect(row).to_have_attribute(_HIGHLIGHT, "")
    expect(row).not_to_have_attribute(_HIGHLIGHT, "", timeout=5_000)


def test_settings_search_reveals_the_picked_row(page: Page, live_server: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 640})
    page.goto(f"{live_server}/settings/general")
    field = _search_field(page)
    expect(field).to_be_visible(timeout=30_000)
    nav_link = page.get_by_test_id("settings-nav-appearance")
    expect(nav_link).to_be_visible()

    # A query's results take the section nav's place.
    field.fill("heavier code")
    # The row itself ranks first, ahead of its parts.
    result = page.get_by_role("option").first
    expect(result).to_have_accessible_name("Heavier code font Appearance")
    expect(result).to_have_attribute("aria-selected", "true")
    expect(nav_link).to_have_count(0)

    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"/settings/appearance#code-font-weight$"))
    _expect_revealed(page, "code-font-weight")
    row = page.locator('[data-settings-item="code-font-weight"]')
    expect(row.get_by_role("switch", name="Use heavier code text")).to_be_focused()
    # The query stays for the next pick; Escape clears it and the nav comes back.
    expect(field).to_have_value("heavier code")
    field.focus()
    page.keyboard.press("Escape")
    expect(field).to_have_value("")
    expect(nav_link).to_be_visible()


# Whether the results scroll: the listbox, something in it, or the sidebar holding it.
_SCROLLS = """(list) => {
  const around = [];
  for (let el = list; el; el = el.parentElement) around.push(el);
  return [...around, ...list.querySelectorAll("*")].some(
    (el) =>
      el.scrollHeight > el.clientHeight + 1 &&
      ["auto", "scroll"].includes(getComputedStyle(el).overflowY),
  );
}"""


def test_settings_search_lists_its_best_matches_without_scrolling(
    page: Page, live_server: str
) -> None:
    """A broad query lists ten results: a full-height window fits them; a short one scrolls,
    keeping the selected result in view."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{live_server}/settings/general")
    field = _search_field(page)
    expect(field).to_be_visible(timeout=30_000)
    field.fill("theme")
    options = page.get_by_role("option")
    expect(options).to_have_count(10)
    listbox = page.get_by_role("listbox")
    assert not listbox.evaluate(_SCROLLS), "a full-height window shouldn't scroll the results"
    expect(options.last).to_be_in_viewport()

    page.set_viewport_size({"width": 1280, "height": 480})
    expect(options.last).not_to_be_in_viewport()
    assert listbox.evaluate(_SCROLLS), "a short window should scroll the results"
    # Moving the selection to the last result scrolls it into view.
    field.focus()
    page.keyboard.press("End")
    expect(options.last).to_have_attribute("aria-selected", "true")
    expect(options.last).to_be_in_viewport()


# A prompt long enough that the Omni Advisor row is taller than a 720px window.
_LONG_ADVISOR_PROMPT = "\n".join(f"Instruction {n}: be useful." for n in range(80))


def _offer_omni_advisor(page: Page) -> None:
    """Report Omni Advisor as offered, with its settings and a long prompt loaded."""

    def info(route: Route) -> None:
        response = fetch_with_retry(route)
        body = response.json()
        body["proactive_agent_available"] = True
        route.fulfill(response=response, body=json.dumps(body))

    page.route("**/v1/info", info)
    page.route(
        "**/v1/proactive-agent/settings",
        lambda route: route.fulfill(
            json={
                "workspace_enabled": True,
                "user_enabled": True,
                "enabled": True,
                "can_manage_workspace": True,
            }
        ),
    )
    page.route(
        "**/v1/proactive-agent/prompt",
        lambda route: route.fulfill(
            json={
                "prompt": _LONG_ADVISOR_PROMPT,
                "default_prompt": _LONG_ADVISOR_PROMPT,
                "overridden": False,
            }
        ),
    )


def test_a_revealed_control_in_a_row_taller_than_the_window_is_on_screen(
    page: Page, live_server: str
) -> None:
    """Picking a part low in a tall row brings that control into view, not just the row."""
    page.set_viewport_size({"width": 1280, "height": 720})
    _offer_omni_advisor(page)
    page.goto(f"{live_server}/settings/admin#proactive-agent-workspace:users")
    users = page.locator('[data-settings-item="proactive-agent-workspace"]').get_by_role(
        "textbox", name="Users to run now"
    )
    expect(users).to_be_focused(timeout=30_000)
    expect(users).to_be_in_viewport()


def _open_command_palette(page: Page) -> Locator:
    """Open ⌘K and return its dialog."""
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_role("dialog", name="Command palette", exact=True)
    expect(palette).to_be_visible(timeout=10_000)
    return palette


def _run_command(page: Page, label: str) -> None:
    """Run the ⌘K command whose label starts with ``label``, from the keyboard."""
    palette = _open_command_palette(page)
    palette.get_by_role("combobox").fill(label)
    command = palette.get_by_role("option", name=re.compile(rf"^{re.escape(label)}"))
    expect(command).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    expect(palette).to_have_count(0)


def test_search_settings_from_the_command_palette(page: Page, live_server: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(live_server)
    sidebar = page.locator('aside[aria-label="Conversations"]')
    expect(sidebar).not_to_have_attribute("data-collapsed", "true", timeout=30_000)
    # A collapsed sidebar opens for the search, and is collapsed again after Back.
    _run_command(page, "Toggle conversations sidebar")
    expect(sidebar).to_have_attribute("data-collapsed", "true")

    _run_command(page, "Search settings…")

    expect(page).to_have_url(re.compile(r"/settings/general$"))
    field = _search_field(page)
    expect(field).to_be_focused()
    page.keyboard.type("terminal theme")
    page.get_by_role("option", name=re.compile(r"^Terminal theme")).click()
    expect(page).to_have_url(re.compile(r"/settings/appearance#terminal-theme$"))
    _expect_revealed(page, "terminal-theme")

    page.get_by_role("link", name="Back").click()
    expect(page).not_to_have_url(re.compile(r"/settings"))
    expect(sidebar).to_have_attribute("data-collapsed", "true")
