"""Unsaved-editor navigation through a collapsed desktop workspace."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
    _open_viewer,
    _post_message,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture


def test_hidden_workspace_guards_unsaved_markdown_navigation(
    page: Page, seeded_session: BrowserSession
) -> None:
    """Hiding the rail keeps its dirty guard until navigation is confirmed."""
    path, destination = "src/draft.md", "src/destination.md"
    files = {path: "Original document.", destination: "Destination document."}
    writes = _mock_markdown_files(page, seeded_session, files)
    _post_message(
        seeded_session,
        f"[Original document]({path}) and [Destination document]({destination})",
    )
    page.clock.install()
    _open_viewer(
        page, seeded_session, path, {"diffActive": False, "previewableViewMode": "editor"}
    )
    viewer = page.get_by_test_id("file-viewer")
    editor = viewer.locator('[contenteditable="true"]')
    expect(editor).to_have_text(files[path])
    # Cache both files before going offline so autosave cannot clear the dirty state.
    for label, target in [("Destination document", destination), ("Original document", path)]:
        page.get_by_role("button", name=label, exact=True).click()
        expect(editor).to_have_text(files[target])
    seeded_session.runner_online = False
    seeded_session.host_online = False
    page.clock.fast_forward(10_000)
    expect(
        viewer.get_by_role(
            "button", name="Runner offline — your changes will save when it reconnects", exact=True
        )
    ).to_be_visible()
    draft = "Keep this unsaved Markdown draft."
    editor.fill(draft)
    expect(viewer.get_by_text("Runner offline — changes save", exact=False)).to_be_visible()

    rail = page.locator('aside[aria-label="Workspace"]')
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    for decision in ("Keep editing", "Discard changes"):
        page.get_by_role("button", name="Collapse right panel", exact=True).click()
        expect(rail).to_have_attribute("data-state", "closed")
        page.wait_for_function(
            "el => el.getBoundingClientRect().width < 2", arg=rail.element_handle()
        )
        collapsed_url = page.url
        page.get_by_role("button", name="Destination document", exact=True).click()
        expect(dialog).to_be_visible()
        expect(page).to_have_url(collapsed_url)
        expect(editor).to_have_text(draft)
        assert not writes, "The guard must protect an unsaved draft"
        dialog.get_by_role("button", name=decision, exact=True).click()
        expect(dialog).not_to_be_visible()
        if decision == "Keep editing":
            expect(rail).to_have_attribute("data-state", "closed")
            page.get_by_role("button", name="Expand right panel", exact=True).click()
            expect(editor).to_be_visible()
            expect(editor).to_have_text(draft)
        else:
            expect(rail).to_have_attribute("data-state", "open")
            expect(editor).to_be_visible()
            expect(editor).to_have_text(files[destination])
            page.wait_for_function(
                "path => new URL(location.href).searchParams.get('file') === path",
                arg=destination,
            )
            expect(dialog).not_to_be_visible()
    assert not writes, "Discarding must not save the offline draft"
