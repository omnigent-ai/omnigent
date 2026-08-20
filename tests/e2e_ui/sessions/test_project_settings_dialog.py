"""Browser e2e for the Project settings dialog (project ``config`` defaults).

A project carries owner-private session defaults in its ``config`` JSON
(``{ host_id?, workspace?, agent_id?, use_worktree?, base_branch? }``). The "Project settings"
dialog (``web/src/shell/ProjectSettingsDialog.tsx``, reached from the
project-folder kebab) reads and writes that config via ``GET /v1/projects/{id}``
and ``PATCH /v1/projects/{id}``.

This drives the real persistence round-trip the ``ProjectSettingsDialog`` unit
tests mock out: open the dialog → flip the "Random worktree" toggle ON → Save
(``PATCH``) → reopen and confirm the fetched config seeds the toggle back ON.
Worktrees are opt-in, so a toggle-ON stores ``use_worktree: true``.

The project is created directly via ``POST /v1/projects`` (an empty first-class
project needs no member session, and this sidesteps the flaky row-kebab
move-to-project dance). The working-directory / host prefill can't be exercised
here — the e2e_ui runner registers no host to browse — so this test stays on the
host-free path (the worktree toggle is a plain Switch, no host needed).
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect


def _create_project(base_url: str, name: str) -> str:
    """Create an empty first-class project via the API; return its id."""
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["id"]


def _set_project_config(base_url: str, project_id: str, config: dict) -> None:
    """Write a project's stored config via ``PATCH /v1/projects/{id}``."""
    resp = httpx.patch(
        f"{base_url}/v1/projects/{project_id}", json={"config": config}, timeout=10.0
    )
    resp.raise_for_status()


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

    Opens an empty project's settings, flips the opt-in worktree toggle ON, and
    Saves (``PATCH /v1/projects/{id}`` with ``use_worktree:true``). Reopening the
    dialog fetches the stored config and seeds the toggle back ON — proving the
    write reached the server and the read seeds from it.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

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

    Starts from a project whose ``config`` has ``use_worktree: true``, opens
    settings (toggle seeds ON), flips it OFF, and Saves. An all-default dialog
    clears to ``{}``, so reopening seeds the toggle OFF.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    _set_project_config(base_url, project_id, {"use_worktree": True})

    page.goto(f"{base_url}/c/{session_id}")

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


def test_project_settings_base_branch_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The base-branch field shows only with the worktree default on, and persists.

    The base branch only forks a worktree, so its field is hidden until the
    "Random worktree" toggle is ON. Flipping the toggle reveals it; typing a
    branch + Save stores ``base_branch`` alongside ``use_worktree:true``.
    Reopening fetches the stored config and seeds both back.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

    # Worktree toggle defaults OFF, so the base-branch field is hidden.
    _open_project_settings(page, project)
    toggle = page.get_by_test_id("project-settings-worktree")
    expect(toggle).to_have_attribute("data-state", "unchecked")
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_count(0)

    # Turning the worktree default ON reveals the base-branch field; fill + save.
    toggle.click()
    base_input = page.get_by_test_id("project-settings-base-branch")
    expect(base_input).to_be_visible()
    base_input.fill("develop")
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — the stored config seeds the toggle ON and the branch back.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "checked"
    )
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_value("develop")


def test_project_settings_drops_base_branch_when_worktree_off(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Turning the worktree default OFF drops a previously stored base branch.

    Starts from a project whose ``config`` has ``use_worktree:true`` and a
    ``base_branch``. The base-branch field is only meaningful with worktrees on,
    so flipping the toggle OFF clears the whole config to ``{}`` — reopening
    seeds the toggle OFF and (once toggled back on) shows a blank base branch.
    """
    base_url, session_id = seeded_session
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)
    _set_project_config(base_url, project_id, {"use_worktree": True, "base_branch": "develop"})

    page.goto(f"{base_url}/c/{session_id}")

    # Seeded ON → the base-branch field shows its stored value.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_value("develop")

    # Flip the worktree default OFF and save → config clears to {}.
    page.get_by_test_id("project-settings-worktree").click()
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # Reopen — toggle seeds OFF; toggling back on shows a blank base branch.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-worktree")).to_have_attribute(
        "data-state", "unchecked"
    )
    page.get_by_test_id("project-settings-worktree").click()
    expect(page.get_by_test_id("project-settings-base-branch")).to_have_value("")
