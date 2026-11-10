"""Workspace tree previews, tab gestures, and split sizing in Chromium."""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

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
}


def _open_tree(
    page: Page,
    session: BrowserSession,
    *,
    width: int = 600,
    enabled: bool = True,
    always_soft_tab: bool = False,
    file: str | None = None,
    unpinned_files: bool = False,
) -> list[str]:
    writes = _mock_markdown_files(page, session, _FILES)
    session.features["workspace_file_tree_split_ui"] = enabled
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
    state = [
        {
            "id": session.session_id,
            "state": {
                "open": True,
                "widthPx": width,
                "rightRailTab": "files",
                "openPanels": ["files"] if unpinned_files else [],
            },
        }
    ]
    initial_storage = {
        "omnigent:session-workspace-state": json.dumps(state),
        "omnigent:always-open-files-in-tab": str(always_soft_tab).lower(),
        "omnigent:file-view-preferences": json.dumps(
            {"diffActive": False, "previewableViewMode": "editor"}
        ),
    }
    if unpinned_files:
        initial_storage["omnigent:pinned-workspace-tabs"] = json.dumps(["github", "subagents"])
    page.add_init_script(
        f"""for (const [key, value] of Object.entries({json.dumps(initial_storage)})) {{
          if (localStorage.getItem(key) === null) localStorage.setItem(key, value);
        }}"""
    )
    page.set_viewport_size({"width": 1800, "height": 1000})
    url = f"{session.contract.base_url}/c/{session.session_id}"
    page.goto(f"{url}?file={file}" if file else url)
    expect(page.get_by_role("toolbar", name="Workspace tabs")).to_be_visible(timeout=30_000)
    return writes


def test_docked_tree_preview_and_explicit_soft_tabs(
    page: Page, seeded_session: BrowserSession
) -> None:
    seeded_session.contract.route(
        re.compile(r".*/environments/default/search\?.*"),
        lambda route: route.fulfill(
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {"path": path, "name": path.split("/")[-1], "type": "file"} for path in _FILES
                ],
            }
        ),
    )
    _open_tree(page, seeded_session)
    tree = page.get_by_test_id("workspace-file-tree-column")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    tabs = toolbar.locator('[role="button"][title^="src/"]')
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(page.get_by_test_id("workspace-files-split")).to_have_count(0)
    expect(page.get_by_role("separator", name="Resize file tree", exact=True)).to_have_count(0)
    expect(page.get_by_text("Select a file to view it here", exact=True)).to_have_count(0)
    expect(viewer).to_have_count(0)
    expect(tree.get_by_text("src/", exact=True)).to_be_visible()
    header = page.locator("[data-files-header]")
    tree_box, header_box = tree.bounding_box(), header.bounding_box()
    assert tree_box is not None and header_box is not None
    assert abs(tree_box["width"] - header_box["width"]) <= 1
    tree.get_by_text("src/", exact=True).click()
    for name in ("app.md", "guide.md"):
        tree.get_by_text(name, exact=True).click()
        expect(viewer).to_contain_text(_FILES[f"src/{name}"])
        expect(page.get_by_test_id("workspace-files-split")).to_be_visible()
        expect(page.get_by_role("separator", name="Resize file tree", exact=True)).to_be_visible()
        expect(tree.locator('button[aria-current="true"]')).to_have_count(1)
        expect(tree.locator('button[aria-current="true"]')).to_have_text(name)
        toolbar.hover()
        assert tree.locator('button[aria-current="true"]').evaluate("""button => {
            const selected = getComputedStyle(button.parentElement).backgroundColor;
            const other = [...button.closest('section').querySelectorAll('button')]
                .find(row => row.textContent === 'notes.md');
            return selected !== 'rgba(0, 0, 0, 0)' &&
                selected !== getComputedStyle(other.parentElement).backgroundColor;
        }""")
        expect(tabs).to_have_count(0)
        expect(tree).to_have_count(1)
        expect(page.locator('input[aria-label="Search all files"]')).to_have_count(1)
        expect(tree).to_be_visible()

    search = tree.get_by_role("searchbox", name="Search all files")
    search.fill(".md")
    expect(tree.get_by_role("button", name="src/guide.md", exact=True)).to_have_attribute(
        "aria-current", "true"
    )
    tree.get_by_role("button", name="src/app.md", exact=True).click()
    expect(tree.locator('button[aria-current="true"]')).to_have_text("src/app.md")
    toolbar.hover()
    assert tree.locator('button[aria-current="true"]').evaluate(
        "button => getComputedStyle(button.parentElement).backgroundColor !== 'rgba(0, 0, 0, 0)'"
    )
    expect(viewer).to_contain_text(_FILES["src/app.md"])
    expect(tabs).to_have_count(0)
    tree.get_by_role("button", name="src/guide.md", exact=True).click()
    expect(tree.locator('button[aria-current="true"]')).to_have_text("src/guide.md")
    search.fill("")
    expect(tree.get_by_role("button", name="guide.md", exact=True)).to_have_attribute(
        "aria-current", "true"
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
        expect(tree).to_have_count(1)
        expect(page.locator('input[aria-label="Search all files"]')).to_have_count(1)
        expect(tree).not_to_be_visible()
        toolbar.locator('[data-workspace-tab="files"]').click()
        expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
        expect(viewer).to_contain_text(_FILES["src/guide.md"])
        expect(tree.locator('button[aria-current="true"]')).to_have_text("guide.md")
    expect(tabs).to_have_count(3)
    expect(page).to_have_url(re.compile(r"[?&]file=src%2Fguide.md(?:&|$)"))
    toolbar.locator('[data-workspace-tab="changes"]').click()
    page.keyboard.press("Escape")
    toolbar.locator('[data-workspace-tab="files"]').click()
    expect(viewer).to_contain_text(_FILES["src/guide.md"])
    expect(page).to_have_url(re.compile(r"[?&]file=src%2Fguide.md(?:&|$)"))

    toolbar.get_by_role("button", name="Full screen", exact=True).click()
    expect(page.get_by_test_id("workspace-files-split")).to_be_visible()
    expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
    expect(tree).to_have_count(1)
    toolbar.get_by_role("button", name="Exit full screen", exact=True).click()
    expect(tree.get_by_text("app.md", exact=True)).to_be_visible()


@pytest.mark.parametrize(
    ("width", "enabled", "always_soft_tab"),
    [(300, True, False), (600, False, False), (600, True, True)],
    ids=["narrow", "flag-off", "setting-on"],
)
def test_single_column_tree_opens_soft_tabs(
    page: Page,
    seeded_session: BrowserSession,
    width: int,
    enabled: bool,
    always_soft_tab: bool,
) -> None:
    _open_tree(page, seeded_session, width=width, enabled=enabled, always_soft_tab=always_soft_tab)
    rail = page.get_by_role("complementary", name="Workspace")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    expect(page.get_by_test_id("workspace-files-split")).to_have_count(0)
    rail.get_by_text("src/", exact=True).click()
    rail.get_by_text("app.md", exact=True).click()
    expect(toolbar.locator('[role="button"][title="src/app.md"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text(
        _FILES["src/app.md"]
    )
    page.reload()
    toolbar.get_by_role("button", name="Select panel", exact=True).click()
    page.get_by_role("menuitem", name="Files", exact=True).click()
    expect(page.get_by_test_id("workspace-files-split")).to_have_count(0)
    expect(rail.get_by_text("src/", exact=True)).to_be_visible()
    assert (
        page.evaluate("localStorage.getItem('omnigent:always-open-files-in-tab')")
        == str(always_soft_tab).lower()
    )


def test_tree_minimum_width_keeps_controls_separate_and_persists(
    page: Page, seeded_session: BrowserSession
) -> None:
    # A local desktop host exposes the IDE menu alongside the asleep badge.
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
    handle = page.get_by_role("separator", name="Resize file tree", exact=True)
    box = handle.bounding_box()
    tree_box = tree.bounding_box()
    assert box is not None and tree_box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(tree_box["x"] + 100, box["y"] + box["height"] / 2, steps=10)
    page.mouse.up()
    expect(handle).to_have_attribute("aria-valuenow", "200")
    header = page.locator("[data-files-header]")
    label = header.get_by_text("workspace", exact=True)
    label_box = label.bounding_box()
    assert label_box is not None and label_box["width"] >= 72
    assert header.evaluate("""header => {
        const panel = header.parentElement.getBoundingClientRect();
        const headerBox = header.getBoundingClientRect();
        const tree = header.parentElement.querySelector(
          '[data-testid="workspace-file-tree-column"]').getBoundingClientRect();
        const viewer = header.parentElement.querySelector(
          '[data-testid="file-viewer"]').getBoundingClientRect();
        const controls = [...header.querySelectorAll('button')]
          .filter(el => el.checkVisibility()).map(el => el.getBoundingClientRect());
        const label = [...header.querySelectorAll('span')]
          .find(el => el.textContent === 'workspace' && el.children.length === 0);
        controls.push(label.getBoundingClientRect());
        return Math.abs(headerBox.width - panel.width) <= 1 &&
          headerBox.left <= tree.left && headerBox.right >= viewer.right &&
          headerBox.bottom <= tree.top && headerBox.bottom <= viewer.top &&
          controls.every((box, index) =>
          box.left >= panel.left && box.right <= panel.right &&
          controls.slice(index + 1).every(other =>
            box.right <= other.left || other.right <= box.left ||
            box.bottom <= other.top || other.bottom <= box.top));
    }""")
    search = tree.get_by_role("searchbox", name="Search all files")
    search_box = search.bounding_box()
    assert search_box is not None and search_box["width"] >= 100
    assert search.evaluate("""input => {
        const column = input.closest('[data-testid="workspace-file-tree-column"]')
          .getBoundingClientRect();
        const controls = [input, ...input.parentElement.parentElement.querySelectorAll('button')]
          .filter(el => el.checkVisibility()).map(el => el.getBoundingClientRect());
        return controls.every((box, index) =>
          box.left >= column.left && box.right <= column.right &&
          controls.slice(index + 1).every(other =>
            box.right <= other.left || other.right <= box.left ||
            box.bottom <= other.top || other.bottom <= box.top));
    }""")
    expect(header.get_by_test_id("files-host-served-badge")).to_be_visible()
    expect(header.get_by_role("button", name="Close file", exact=True)).to_be_visible()
    expect(header.get_by_role("button", name="Choose app to open in", exact=True)).to_have_count(0)
    expect(header.get_by_role("button", name="Open in IDE", exact=True)).to_have_count(0)
    expect(
        header.get_by_role("button", name="Copy folder path: workspace", exact=True)
    ).to_be_visible()
    expect(header.get_by_role("button", name="Refresh files", exact=True)).to_be_visible()
    expect(header.get_by_role("button", name="More file actions", exact=True)).not_to_be_visible()

    handle.focus()
    page.keyboard.press("ArrowRight")
    expect(handle).to_have_attribute("aria-valuenow", "216")
    assert page.evaluate("localStorage.getItem('omnigent:workspace-file-tree-width')") == "216"
    page.reload()
    page.get_by_role("toolbar", name="Workspace tabs").locator(
        '[data-workspace-tab="files"]'
    ).click()
    expect(handle).to_have_attribute("aria-valuenow", "216")
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text(
        _FILES["src/app.md"]
    )
    tree.get_by_text("src/", exact=True).click()
    expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
    header.get_by_role("button", name="Close file", exact=True).click()
    expect(page.get_by_test_id("workspace-files-split")).to_have_count(0)
    expect(handle).to_have_count(0)
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_have_count(0)
    expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
    expect(tree.locator('button[aria-current="true"]')).to_have_count(0)
    tree_box, header_box = tree.bounding_box(), header.bounding_box()
    assert tree_box is not None and header_box is not None
    assert abs(tree_box["width"] - header_box["width"]) <= 1
    expect(header.get_by_role("button", name="Close file", exact=True)).to_have_count(0)
    header.get_by_role("button", name="Choose app to open in", exact=True).click()
    expect(page.get_by_role("menuitem", name="VS Code", exact=True)).to_be_visible()
    page.keyboard.press("Escape")


@pytest.mark.parametrize("destination", ["file", "close-panel", "close-preview"])
def test_inline_tree_navigation_guards_unsaved_markdown(
    page: Page, seeded_session: BrowserSession, destination: str
) -> None:
    page.clock.install()
    writes = _open_tree(page, seeded_session, unpinned_files=destination == "close-panel")
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
    expect(viewer.get_by_text("Runner offline — changes save", exact=False)).to_be_visible()
    # Both responsive transitions retain the mounted draft without letting hidden Escape close it.
    page.set_viewport_size({"width": 700, "height": 1000})
    expect(viewer).to_have_count(0)
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 1800, "height": 1000})
    expect(editor).to_have_text(draft)
    dialog = page.get_by_role("dialog", name="Unsaved changes")
    current_url = page.url
    for decision in ("Keep editing", "Discard changes"):
        if destination == "file":
            tree.get_by_text("guide.md", exact=True).click()
        elif destination == "close-preview":
            page.locator("[data-files-header]").get_by_role(
                "button", name="Close file", exact=True
            ).click()
        else:
            page.get_by_role("button", name="Close Files", exact=True).click()
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
        elif destination == "close-preview":
            expect(viewer).to_have_count(0)
            expect(page.get_by_test_id("workspace-files-split")).to_have_count(0)
            expect(tree.get_by_text("app.md", exact=True)).to_be_visible()
            expect(
                page.locator("[data-files-header]").get_by_role(
                    "button", name="Close file", exact=True
                )
            ).to_have_count(0)
        else:
            expect(viewer).to_have_count(0)
            expect(page.get_by_role("button", name="Close Files", exact=True)).to_have_count(0)
    assert not writes
    expect(
        page.get_by_role("toolbar", name="Workspace tabs").locator(
            '[role="button"][title^="src/"]'
        )
    ).to_have_count(0)


def test_file_deep_link_still_opens_a_soft_tab(page: Page, seeded_session: BrowserSession) -> None:
    _open_tree(page, seeded_session, file="src/app.md")
    toolbar = page.get_by_role("toolbar", name="Workspace tabs")
    expect(toolbar.locator('[role="button"][title="src/app.md"]')).to_have_attribute(
        "aria-current", "true"
    )
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_contain_text(
        _FILES["src/app.md"]
    )
    expect(page.get_by_test_id("workspace-file-tree-column")).not_to_be_visible()
