"""Workspace tree/preview geometry and editor lifecycle in Chromium."""

from __future__ import annotations

import json
import re
from itertools import combinations

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture

_FILES = {
    "src/app.md": "App document.",
    "src/guide.md": "Guide document.",
    "src/notes.md": "Notes document.",
    **{f"src/reference-{index:02}.md": f"Reference {index}." for index in range(35)},
}


def _open_tree(page: Page, session: BrowserSession, *, width: int = 600) -> list[str]:
    writes = _mock_markdown_files(page, session, _FILES)
    session.features["workspace_file_tree_split_ui"] = True
    session.contract.route(
        re.compile(r".*/environments/default/filesystem\?.*"),
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [{"path": "src", "name": "src", "type": "directory"}],
            }
        ),
    )
    storage = {
        "omnigent:session-workspace-state": json.dumps(
            [
                {
                    "id": session.session_id,
                    "state": {"open": True, "widthPx": width, "rightRailTab": "files"},
                }
            ]
        ),
        "omnigent:file-view-preferences": json.dumps(
            {"diffActive": False, "previewableViewMode": "editor"}
        ),
    }
    page.add_init_script(
        f"""for (const [key, value] of Object.entries({json.dumps(storage)})) {{
            if (localStorage.getItem(key) === null) localStorage.setItem(key, value);
        }}"""
    )
    page.set_viewport_size({"width": 1800, "height": 1000})
    page.goto(f"{session.contract.base_url}/c/{session.session_id}")
    expect(page.get_by_role("toolbar", name="Workspace tabs")).to_be_visible(timeout=30_000)
    return writes


def _assert_controls_fit(container: Locator, controls: Locator) -> None:
    bounds = container.bounding_box()
    assert bounds is not None
    boxes = [control.bounding_box() for control in controls.all() if control.is_visible()]
    assert boxes and all(box is not None for box in boxes)
    for box in boxes:
        assert box["x"] >= bounds["x"] - 1
        assert box["x"] + box["width"] <= bounds["x"] + bounds["width"] + 1
    for first, second in combinations(boxes, 2):
        assert (
            first["x"] + first["width"] <= second["x"] + 1
            or second["x"] + second["width"] <= first["x"] + 1
            or first["y"] + first["height"] <= second["y"] + 1
            or second["y"] + second["height"] <= first["y"] + 1
        ), (first, second)


def test_docked_tree_preview_and_explicit_soft_tabs(
    page: Page, seeded_session: BrowserSession
) -> None:
    _open_tree(page, seeded_session)
    tree = page.get_by_test_id("workspace-file-tree-column")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    divider = page.get_by_role("separator", name="Resize file tree", exact=True)
    expect(divider).to_have_count(0)
    expect(viewer).to_have_count(0)
    expect(page.get_by_text("Select a file to view it here", exact=True)).to_have_count(0)
    header = page.locator("[data-files-header]")
    assert abs(tree.bounding_box()["width"] - header.bounding_box()["width"]) <= 1
    tree.get_by_text("src/", exact=True).click()
    original_tree = tree.element_handle()
    for name in ("app.md", "guide.md"):
        tree.get_by_text(name, exact=True).click()
        expect(viewer).to_contain_text(_FILES[f"src/{name}"])
        expect(divider).to_be_visible()
        expect(tree.locator('button[aria-current="true"]')).to_have_text(name)
        expect(toolbar.locator('[role="button"][title^="src/"]')).to_have_count(0)
    toolbar.hover()
    assert tree.locator('button[aria-current="true"]').evaluate(
        "button => getComputedStyle(button.parentElement).backgroundColor !== 'rgba(0, 0, 0, 0)'"
    )

    for name, gesture in (("app.md", "modified"), ("notes.md", "middle"), ("guide.md", "menu")):
        row = tree.get_by_text(name, exact=True)
        if gesture == "modified":
            row.click(modifiers=["ControlOrMeta"])
        elif gesture == "middle":
            row.click(button="middle")
        else:
            row.click(button="right")
            page.get_by_role("menuitem", name="Open in new tab", exact=True).click()
        expect(toolbar.locator(f'[role="button"][title="src/{name}"]')).to_have_attribute(
            "aria-current", "true"
        )
        expect(viewer).to_have_count(1)
        expect(viewer).to_contain_text(_FILES[f"src/{name}"])
        expect(tree).not_to_be_visible()
        toolbar.locator('[data-workspace-tab="files"]').click()
        expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
        expect(viewer).to_contain_text(_FILES["src/guide.md"])
        assert tree.evaluate("(node, original) => node === original", original_tree)
        expect(page.get_by_role("searchbox", name="Search all files")).to_have_count(1)

    scroll = tree.locator("section")
    scroll.evaluate("node => { node.scrollTop = 100; }")
    offset = scroll.evaluate("node => node.scrollTop")
    assert offset > 0
    toolbar.locator('[role="button"][title="src/app.md"]').click()
    toolbar.locator('[data-workspace-tab="files"]').click()
    assert scroll.evaluate("node => node.scrollTop") == offset
    expect(page).to_have_url(re.compile(r"[?&]file=src%2Fguide.md(?:&|$)"))
    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    expect(divider).to_be_visible()
    toolbar.get_by_role("button", name="Exit full screen", exact=True).click()
    expect(divider).to_be_visible()
    assert tree.evaluate("(node, original) => node === original", original_tree)


def test_narrow_tree_opens_a_standalone_tab(page: Page, seeded_session: BrowserSession) -> None:
    _open_tree(page, seeded_session, width=300)
    tree = page.get_by_test_id("workspace-file-tree-column")
    tree.get_by_text("src/", exact=True).click()
    tree.get_by_text("app.md", exact=True).click()
    expect(page.get_by_role("separator", name="Resize file tree", exact=True)).to_have_count(0)
    expect(
        page.get_by_role("toolbar", name="Workspace tabs").locator(
            '[role="button"][title="src/app.md"]'
        )
    ).to_have_attribute("aria-current", "true")
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text(
        _FILES["src/app.md"]
    )


def test_tree_minimum_width_keeps_controls_separate_and_persists(
    page: Page, seeded_session: BrowserSession
) -> None:
    seeded_session.runner_online = False
    seeded_session.contract.json(
        f"/v1/sessions/{seeded_session.session_id}",
        {
            "id": seeded_session.session_id,
            "object": "conversation",
            "title": "Workspace files",
            "agent_id": "file-line-agent",
            "agent_name": "hello_world",
            "host_id": "local",
            "status": "idle",
            "created_at": 1,
            "updated_at": 1,
            "labels": {},
            "permission_level": None,
        },
    )
    seeded_session.contract.json("/v1/hosts/local/worktrees", {"data": []})
    page.add_init_script("""window.omnigentDesktop = {
        kind: "electron",
        getHostIdentity: async () => ({ cliInstalled: true, hostId: "local" }),
        revealFile: async () => true,
    };""")
    _open_tree(page, seeded_session)
    tree = page.get_by_test_id("workspace-file-tree-column")
    tree.get_by_text("src/", exact=True).click()
    tree.get_by_text("app.md", exact=True).click()
    divider = page.get_by_role("separator", name="Resize file tree", exact=True)
    box, tree_box = divider.bounding_box(), tree.bounding_box()
    assert box is not None and tree_box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 50)
    page.mouse.down()
    page.mouse.move(tree_box["x"] + 100, box["y"] + 50, steps=10)
    page.mouse.up()
    expect(divider).to_have_attribute("aria-valuenow", "200")
    header = page.locator("[data-files-header]")
    label = header.get_by_text("workspace", exact=True)
    assert label.bounding_box()["width"] >= 72
    _assert_controls_fit(header, header.locator("button").or_(label))
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    header_box, tree_box, viewer_box = (
        header.bounding_box(),
        tree.bounding_box(),
        viewer.bounding_box(),
    )
    assert abs(tree_box["width"] - 200) <= 1
    assert viewer_box["width"] >= 320
    assert header_box["y"] + header_box["height"] <= tree_box["y"]
    assert header_box["y"] + header_box["height"] <= viewer_box["y"]
    assert header_box["x"] <= tree_box["x"]
    assert header_box["x"] + header_box["width"] >= viewer_box["x"] + viewer_box["width"] - 1
    search = tree.get_by_role("searchbox", name="Search all files")
    search_row = search.locator("../..")
    _assert_controls_fit(tree, search_row.locator("input, button"))
    assert search.bounding_box()["width"] >= 100
    search_y = search.bounding_box()["y"] + search.bounding_box()["height"] / 2
    for control in search_row.locator("button").all():
        control_box = control.bounding_box()
        assert abs(control_box["y"] + control_box["height"] / 2 - search_y) <= 2
    row = tree.get_by_role("button", name="app.md", exact=True).locator("..")
    row.hover()
    _assert_controls_fit(row, row.locator("button"))
    expect(header.get_by_test_id("files-host-served-badge")).to_be_visible()
    expect(header.get_by_role("button", name="Close file", exact=True)).to_be_visible()
    expect(header.get_by_role("button", name="Choose app to open in", exact=True)).to_have_count(0)
    divider.focus()
    page.keyboard.press("ArrowRight")
    expect(divider).to_have_attribute("aria-valuenow", "216")
    assert page.evaluate("localStorage.getItem('omnigent:workspace-file-tree-width')") == "216"
    page.reload()
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    expect(toolbar.locator('[role="button"][title="src/app.md"]')).to_have_attribute(
        "aria-current", "true"
    )
    toolbar.locator('[data-workspace-tab="files"]').click()
    expect(divider).to_have_attribute("aria-valuenow", "216")
    expect(viewer).to_contain_text(_FILES["src/app.md"])
    tree.get_by_text("src/", exact=True).click()
    header.get_by_role("button", name="Close file", exact=True).click()
    expect(divider).to_have_count(0)
    expect(viewer).to_have_count(0)
    expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
    expect(tree.locator('button[aria-current="true"]')).to_have_count(0)
    assert abs(tree.bounding_box()["width"] - header.bounding_box()["width"]) <= 1
    header.get_by_role("button", name="Choose app to open in", exact=True).click()
    expect(page.get_by_role("menuitem", name="VS Code", exact=True)).to_be_visible()


@pytest.mark.parametrize("destination", ["file", "close-preview", "close-tab"])
def test_inline_tree_navigation_guards_unsaved_markdown(
    page: Page, seeded_session: BrowserSession, destination: str
) -> None:
    page.clock.install()
    writes = _open_tree(page, seeded_session)
    tree = page.get_by_test_id("workspace-file-tree-column")
    tree.get_by_text("src/", exact=True).click()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = viewer.locator('[contenteditable="true"]')
    for name in ("guide.md", "app.md"):
        tree.get_by_text(name, exact=True).click()
        expect(editor).to_have_text(_FILES[f"src/{name}"])
    seeded_session.runner_online = False
    seeded_session.host_online = False
    page.clock.fast_forward(10_000)
    expect(
        viewer.get_by_role(
            "button", name="Runner offline — your changes will save when it reconnects", exact=True
        )
    ).to_be_visible()
    draft = "Keep this inline draft."
    editor.fill(draft)
    # A hidden responsive preview retains the draft and ignores viewer shortcuts.
    page.set_viewport_size({"width": 700, "height": 1000})
    expect(viewer).to_have_count(0)
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 1800, "height": 1000})
    expect(editor).to_have_text(draft)
    if destination == "close-tab":
        tree.get_by_text("app.md", exact=True).click(modifiers=["ControlOrMeta"])
        expect(editor).to_have_text(draft)
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    current_url = page.url
    for decision in ("Keep editing", "Discard changes"):
        if destination == "file":
            tree.get_by_text("guide.md", exact=True).click()
        elif destination == "close-tab":
            if decision == "Keep editing":
                page.get_by_role("button", name="Close app.md", exact=True).click()
            else:
                page.get_by_role("toolbar", name="Workspace tabs").locator(
                    '[title="src/app.md"]'
                ).focus()
                page.keyboard.press("Escape")
        else:
            page.locator("[data-files-header]").get_by_role(
                "button", name="Close file", exact=True
            ).click()
        expect(dialog).to_be_visible()
        expect(page).to_have_url(current_url)
        expect(editor).to_have_text(draft)
        assert not writes
        dialog.get_by_role("button", name=decision, exact=True).click()
        expect(dialog).not_to_be_visible()
        if decision == "Keep editing":
            expect(editor).to_have_text(draft)
        elif destination == "file":
            expect(editor).to_have_text(_FILES["src/guide.md"])
        elif destination == "close-tab":
            expect(tree).to_be_visible()
            expect(editor).to_have_text(_FILES["src/app.md"])
            editor.fill("A fresh unsaved draft")
            tree.get_by_text("guide.md", exact=True).click()
            expect(dialog).to_be_visible()
            dialog.get_by_role("button", name="Keep editing", exact=True).click()
            expect(editor).to_have_text("A fresh unsaved draft")
        else:
            expect(viewer).to_have_count(0)
            expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
    assert not writes
