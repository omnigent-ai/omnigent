"""Browser e2e: the Rename-project dialog must focus and preselect the name.

Drives the real sidebar journey (right-click a project folder -> Rename
project) and asserts that the dialog opens with the name input focused and
its whole value selected, so typing immediately replaces the current name.
"""

from __future__ import annotations

import uuid

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


def _open_rename_dialog(page: Page, seeded_session: tuple[str, str]) -> Locator:
    """Open the rename dialog for a fresh project and return its name input."""
    base_url, session_id = seeded_session
    project = f"Repro-{uuid.uuid4().hex[:6]}"
    page.goto(f"{base_url}/c/{session_id}")
    _create_project(page, project)
    header = _folder_header(page, project)
    expect(header).to_be_visible()
    header.click(button="right")
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


def test_rename_dialog_preselects_name_so_typing_replaces(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The current name opens fully selected, so typing replaces it."""
    name_input = _open_rename_dialog(page, seeded_session)
    new_name = f"Renamed-{uuid.uuid4().hex[:6]}"
    page.keyboard.type(new_name)
    expect(name_input).to_have_value(new_name)
