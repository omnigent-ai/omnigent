"""Browser e2e: the Rename-project dialog must focus and preselect the name.

Drives the real sidebar journey (right-click a project folder -> Rename
project) and asserts that the dialog opens with the name input focused and
its whole value selected, so typing immediately replaces the current name.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Locator, Page, expect


def _create_project(page: Page, name: str) -> None:
    """Create an empty project from the Projects header action."""
    # Fine-pointer layouts reveal this control only while its header is hovered.
    page.get_by_role("button", name="Projects", exact=True).hover()
    page.get_by_test_id("new-project").click()
    page.get_by_placeholder("Project name…").fill(name)
    page.get_by_test_id("new-project-confirm").click()
    expect(page.get_by_test_id("new-project-confirm")).to_have_count(0)


def _folder_header(page: Page, project: str) -> Locator:
    """Locate a project folder's collapse-header button by its visible name."""
    return page.locator('button[data-slot="context-menu-trigger"]').filter(has_text=project)


def _open_rename_dialog(
    page: Page,
    seeded_session: tuple[str, str],
    menu_kind: str = "context",
    *,
    pause_menu_close: bool = False,
) -> Locator:
    """Open the rename dialog for a fresh project and return its name input."""
    base_url, session_id = seeded_session
    project = f"Repro-{uuid.uuid4().hex[:6]}"
    page.goto(f"{base_url}/c/{session_id}")
    _create_project(page, project)
    header = _folder_header(page, project)
    expect(header).to_be_visible()
    if menu_kind == "context":
        header.click(button="right")
    else:
        page.get_by_role("button", name=f"Project actions for {project}", exact=True).click()
    if pause_menu_close:
        # Hold the exit animation so typing straddles the menu's focus restoration.
        page.add_style_tag(
            content='[role="menu"][data-state="closed"] '
            "{ animation-play-state: paused !important; }"
        )
    page.get_by_test_id("rename-project").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    name_input = dialog.locator("input")
    expect(name_input).to_have_value(project)
    return name_input


def test_rename_dialog_focuses_name_input(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The name input receives initial focus when the dialog opens."""
    name_input = _open_rename_dialog(page, seeded_session)
    expect(name_input).to_be_focused()


@pytest.mark.parametrize("menu_kind", ["context", "dropdown"])
def test_rename_dialog_preselects_name_so_typing_replaces(
    page: Page,
    seeded_session: tuple[str, str],
    menu_kind: str,
) -> None:
    """The current name opens fully selected, so typing replaces it."""
    name_input = _open_rename_dialog(page, seeded_session, menu_kind, pause_menu_close=True)
    old_name = name_input.input_value()
    expect(name_input).to_be_focused()
    expect(name_input).to_have_js_property("selectionStart", 0)
    expect(name_input).to_have_js_property("selectionEnd", len(old_name))
    new_name = f"Renamed-{uuid.uuid4().hex[:6]}"
    page.keyboard.type(new_name[:4])
    menu = page.locator('[role="menu"][data-state="closed"]')
    # Wait for Radix's deferred close-focus event, including its default action.
    menu.evaluate("""async menu => {
        const closed = new Promise(resolve => {
            menu.addEventListener('focusScope.autoFocusOnUnmount', () => {
                queueMicrotask(resolve);
            }, { once: true });
        });
        menu.getAnimations().forEach(animation => animation.finish());
        await closed;
    }""")
    page.keyboard.type(new_name[4:])
    expect(name_input).to_have_value(new_name)
