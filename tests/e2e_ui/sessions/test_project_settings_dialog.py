"""Browser e2e for the Project settings dialog (project ``config`` defaults).

A project carries owner-private session defaults in its ``config`` JSON
(``{ host_id?, workspace?, agent_id?, use_worktree? }``). The "Project settings"
dialog (``web/src/shell/ProjectSettingsDialog.tsx``, reached from the
project-folder kebab) reads and writes that config via ``GET /v1/projects/{id}``
and ``PATCH /v1/projects/{id}``.

This drives the real persistence round-trip the ``ProjectSettingsDialog`` unit
tests mock out: open the dialog → flip the "Random worktree" toggle ON → Save
(``PATCH``) → reopen and confirm the fetched config seeds the toggle back ON.
Worktrees are opt-in, so a toggle-ON stores ``use_worktree: true``.

The working-directory / host prefill can't be exercised here — the e2e_ui
runner registers no host to browse — so this test stays on the host-free path
(the worktree toggle is a plain Switch, no host needed).
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _move_to_new_project(page: Page, row: Locator, name: str) -> None:
    """Drive the row kebab → "Add to project" → "Create new project" flow so a
    first-class project row exists (its ``config`` can then be edited)."""
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("move-to-project").click()
    page.get_by_role("menuitem", name="Create new project").click()
    new_input = page.get_by_placeholder("Project name…")
    new_input.fill(name)
    new_input.press("Enter")


def _open_project_settings(page: Page, project: str) -> None:
    """Open the folder kebab → "Project settings" for *project*."""
    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible()
    header.hover()
    page.get_by_role("button", name=f"Project actions for {project}").click()
    page.get_by_test_id("project-settings").click()
    # The dialog's Save button confirms the editor mounted + the config fetch
    # settled (Save is disabled while loading).
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


def test_project_settings_worktree_toggle_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Turning "Random worktree" ON persists and re-seeds on reopen.

    Creates a first-class project, opens its settings, flips the opt-in worktree
    toggle ON, and Saves (``PATCH /v1/projects/{id}`` with ``use_worktree:true``).
    Reopening the dialog fetches the stored config and seeds the toggle back ON —
    proving the write reached the server and the read seeds from it.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")
    _move_to_new_project(page, _row(page, session_id), project)
    expect(page.get_by_role("button", name=project, exact=True)).to_be_visible()

    # Open settings; the worktree toggle defaults OFF (opt-in).
    _open_project_settings(page, project)
    toggle = page.get_by_test_id("project-settings-worktree")
    expect(toggle).to_have_attribute("data-state", "unchecked")

    # Flip it ON and save.
    toggle.click()
    expect(toggle).to_have_attribute("data-state", "checked")
    page.get_by_test_id("project-settings-save").click()

    # The dialog closes on a successful save.
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — the stored config seeds the toggle back ON.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "checked"
    )


def test_project_settings_clears_worktree_toggle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Turning the toggle back OFF clears the stored default.

    Starts from a project whose ``config`` has ``use_worktree: true`` (set via
    the API), opens settings (toggle seeds ON), flips it OFF, and Saves. An
    all-default dialog clears to ``{}``, so reopening seeds the toggle OFF.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"

    page.goto(f"{base_url}/c/{session_id}")
    _move_to_new_project(page, _row(page, session_id), project)
    expect(page.get_by_role("button", name=project, exact=True)).to_be_visible()

    # Resolve the freshly-created project's id and pre-seed use_worktree:true so
    # the dialog opens with the toggle already ON.
    projects = httpx.get(f"{base_url}/v1/projects", timeout=10.0).json()
    project_id = next(p["id"] for p in projects["data"] if p["name"] == project)
    httpx.patch(
        f"{base_url}/v1/projects/{project_id}",
        json={"config": {"use_worktree": True}},
        timeout=10.0,
    ).raise_for_status()

    _open_project_settings(page, project)
    toggle = page.get_by_test_id("project-settings-worktree")
    expect(toggle).to_have_attribute("data-state", "checked")

    # Flip OFF and save → config clears to {}.
    toggle.click()
    expect(toggle).to_have_attribute("data-state", "unchecked")
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — no stored default, toggle seeds OFF.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "unchecked"
    )
