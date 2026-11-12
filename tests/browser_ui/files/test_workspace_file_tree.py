"""Workspace Files tree beside an open file: Chromium geometry, gestures and editor lifetime."""

from __future__ import annotations

import json
import re
from itertools import combinations

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
    "src/handbook.md": "\n\n".join(f"Handbook line {index}." for index in range(1, 151)),
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
    # Seed only a fresh profile, so a reload keeps what the page persisted.
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


def test_tree_beside_file_routes_gestures_and_keeps_tree_state(
    page: Page, seeded_session: BrowserSession
) -> None:
    _open_tree(page, seeded_session)
    tree = page.get_by_test_id("workspace-file-tree-column")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    files_tab = toolbar.locator('[data-workspace-tab="files"]')
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    divider = page.get_by_role("separator", name="Resize file tree", exact=True)
    header = page.locator("[data-files-header]")
    # Before any file is chosen the tree has the whole panel: no empty file pane.
    expect(divider).to_have_count(0)
    expect(viewer).to_have_count(0)
    assert abs(tree.bounding_box()["width"] - header.bounding_box()["width"]) <= 1
    tree.get_by_text("src/", exact=True).click()
    original_tree = tree.element_handle()
    for name in ("app.md", "guide.md"):
        tree.get_by_text(name, exact=True).click()
        expect(viewer).to_contain_text(_FILES[f"src/{name}"])
        expect(divider).to_be_visible()
        expect(tree.locator('button[aria-current="true"]')).to_have_text(name)
        expect(toolbar.locator('[role="button"][title^="src/"]')).to_have_count(0)
    expect(page).to_have_url(re.compile(r"[?&]file=src%2Fguide\.md(?:&|$)"))
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
        files_tab.click()
        expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
        expect(viewer).to_contain_text(_FILES["src/guide.md"])
        assert tree.evaluate("(node, original) => node === original", original_tree)
        expect(page.get_by_role("searchbox", name="Search all files")).to_have_count(1)

    scroll = tree.locator("section")
    scroll.evaluate("node => { node.scrollTop = 100; }")
    offset = scroll.evaluate("node => node.scrollTop")
    assert offset > 0
    toolbar.locator('[role="button"][title="src/app.md"]').click()
    files_tab.click()
    assert scroll.evaluate("node => node.scrollTop") == offset
    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    expect(divider).to_be_visible()
    toolbar.get_by_role("button", name="Exit full screen", exact=True).click()
    expect(divider).to_be_visible()

    # Narrowed below the split width (measured live), the tree fills the panel again and
    # a plain click opens a tab; widening brings the file back beside the same tree.
    rail_edge = page.get_by_role("separator", name="Resize panel", exact=True)
    rail_edge.focus()
    for _ in range(5):
        page.keyboard.press("ArrowRight")
    expect(divider).to_have_count(0)
    assert abs(tree.bounding_box()["width"] - header.bounding_box()["width"]) <= 1
    tree.get_by_text("reference-00.md", exact=True).click()
    expect(toolbar.locator('[role="button"][title="src/reference-00.md"]')).to_have_attribute(
        "aria-current", "true"
    )
    files_tab.click()
    # At the docked minimum the header still fits, with Copy and Refresh in Folder actions.
    rail_edge.focus()
    for _ in range(15):
        page.keyboard.press("ArrowRight")
    expect(header.get_by_role("button", name="Folder actions", exact=True)).to_be_visible()
    expect(header.get_by_role("button", name="Refresh files", exact=True)).not_to_be_visible()
    label = header.get_by_text("workspace", exact=True)
    assert label.bounding_box()["width"] >= 72
    _assert_controls_fit(header, header.locator("button").or_(label))
    rail_edge.focus()
    for _ in range(20):
        page.keyboard.press("ArrowLeft")
    expect(divider).to_be_visible()
    expect(viewer).to_contain_text(_FILES["src/guide.md"])
    assert tree.evaluate("(node, original) => node === original", original_tree)

    # The file beside the tree keeps its scroll position while another tab covers it.
    tree.get_by_text("handbook.md", exact=True).click()
    expect(viewer).to_contain_text("Handbook line 1.")
    content = viewer.locator('[contenteditable="true"]').evaluate_handle(
        """node => {
            let element = node.parentElement;
            while (element && element.scrollHeight <= element.clientHeight) {
                element = element.parentElement;
            }
            return element;
        }"""
    )
    content.evaluate("node => { node.scrollTop = 600; }")
    # Polled per frame, so the scroll event has fired before the tab changes.
    page.wait_for_function("node => node.scrollTop === 600", arg=content)
    toolbar.locator('[data-workspace-tab="subagents"]').click()
    expect(tree).not_to_be_visible()
    files_tab.click()
    expect(viewer).to_contain_text("Handbook line 1.")
    assert content.evaluate("node => node.scrollTop") == 600


def test_tree_minimum_width_keeps_controls_separate_and_persists(
    page: Page, seeded_session: BrowserSession
) -> None:
    # The host serves files while the runner sleeps: the header shows its Asleep badge.
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
    # A local desktop host offers the folder's Open-in action.
    page.add_init_script("""window.omnigentDesktop = {
        kind: "electron",
        getHostIdentity: async () => ({ cliInstalled: true, hostId: "local" }),
        revealFile: async () => true,
    };""")
    _open_tree(page, seeded_session)
    tree = page.get_by_test_id("workspace-file-tree-column")
    header = page.locator("[data-files-header]")
    folder_open_in = header.get_by_role("button", name="Choose app to open in", exact=True)
    expect(folder_open_in).to_be_visible()
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
    # The header spans the panel above both the tree column and the file.
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
    row = tree.get_by_text("app.md", exact=True).locator("xpath=ancestor::button/..")
    row.hover()
    _assert_controls_fit(row, row.locator("button"))
    expect(header.get_by_test_id("files-host-served-badge")).to_be_visible()
    # One Open-in per file: the folder's gives way to Close file.
    expect(header.get_by_role("button", name="Close file", exact=True)).to_be_visible()
    expect(folder_open_in).to_have_count(0)
    expect(viewer.get_by_role("button", name="Choose app to open in", exact=True)).to_have_count(1)

    divider.focus()
    page.keyboard.press("ArrowRight")
    expect(divider).to_have_attribute("aria-valuenow", "216")
    page.reload()
    # The ?file= link reopens as a tab, by design; the preview waits on the Files tab.
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    expect(toolbar.locator('[role="button"][title="src/app.md"]')).to_have_attribute(
        "aria-current", "true"
    )
    toolbar.locator('[data-workspace-tab="files"]').click()
    expect(divider).to_have_attribute("aria-valuenow", "216")
    expect(viewer).to_contain_text(_FILES["src/app.md"])
    header.get_by_role("button", name="Close file", exact=True).click()
    expect(divider).to_have_count(0)
    expect(viewer).to_have_count(0)
    expect(page).not_to_have_url(re.compile(r"[?&]file="))
    expect(tree.locator('button[aria-current="true"]')).to_have_count(0)
    assert abs(tree.bounding_box()["width"] - header.bounding_box()["width"]) <= 1
    folder_open_in.click()
    expect(page.get_by_role("menuitem", name="VS Code", exact=True)).to_be_visible()


def test_closing_a_previewed_files_tab_discards_its_real_draft_once(
    page: Page, seeded_session: BrowserSession
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
    # Offline, autosave can't run, so the draft is genuinely unsaved.
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
    # On a phone the preview hides but keeps its draft and ignores viewer shortcuts.
    page.set_viewport_size({"width": 700, "height": 1000})
    expect(viewer).to_have_count(0)
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 1800, "height": 1000})
    expect(editor).to_have_text(draft)
    # The same file in its own tab keeps the same editor and draft.
    tree.get_by_text("app.md", exact=True).click(modifiers=["ControlOrMeta"])
    expect(editor).to_have_text(draft)

    dialog = page.get_by_role("dialog", name="Unsaved changes")
    current_url = page.url
    page.get_by_role("button", name="Close app.md", exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("app.md")
    expect(page).to_have_url(current_url)
    dialog.get_by_role("button", name="Keep editing", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(editor).to_have_text(draft)

    page.get_by_role("toolbar", name="Workspace tabs").locator('[title="src/app.md"]').focus()
    page.keyboard.press("Escape")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Discard changes", exact=True).click()
    expect(dialog).not_to_be_visible()
    # Back beside the tree, the discarded draft is gone for good, and a new one is guarded.
    expect(tree).to_be_visible()
    expect(editor).to_have_text(_FILES["src/app.md"])
    editor.fill("A fresh unsaved draft")
    tree.get_by_text("guide.md", exact=True).click()
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Keep editing", exact=True).click()
    expect(editor).to_have_text("A fresh unsaved draft")
    assert not writes
