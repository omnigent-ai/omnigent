"""Real drag/drop, routing and concurrent transcripts against explicit browser contracts."""

from __future__ import annotations

import re
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import chat_session_handle, list_payload, message_item
from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.files.test_file_line_navigation import BrowserSession, _mock_markdown_files


def _drag_session(page: Page, session_id: str, target_id: str, edge: str) -> None:
    source = page.locator(f'aside [data-sidebar-session-id="{session_id}"]')
    source.scroll_into_view_if_needed()
    box = source.bounding_box()
    assert box
    page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 50, box["y"] + box["height"] / 2)
    target = page.locator(f'[data-grid-session="{target_id}"] [data-grid-drop-edge="{edge}"]')
    expect(target).to_be_visible()
    target.hover()
    expect(page.get_by_test_id("grid-drop-ghost")).to_be_visible()
    page.mouse.up()
    expect(page.get_by_test_id("grid-drop-ghost")).to_have_count(0)


def _move_cell(page: Page, session_id: str, target_id: str, *, cancel: bool = False) -> None:
    title = page.locator(f'[data-grid-session="{session_id}"]').get_by_role(
        "button", name=re.compile("^Move ")
    )
    box = title.bounding_box()
    assert box
    page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 50, box["y"] + box["height"] / 2)
    page.locator(f'[data-grid-session="{target_id}"]').hover()
    expect(page.get_by_test_id("grid-move-ghost")).to_be_visible()
    if cancel:
        page.keyboard.press("Escape")
    page.mouse.up()
    expect(page.get_by_test_id("grid-move-ghost")).to_have_count(0)


def test_session_grid_drag_persist_live_updates_and_close(
    page: Page, browser_contract: BrowserContract, output_path: str
) -> None:
    artifacts = Path(output_path)
    with ExitStack() as stack:
        sessions = [
            stack.enter_context(
                contextmanager(chat_session_handle)(
                    page, browser_contract, session_id=f"grid-{index}"
                )
            )
            for index in range(5)
        ]
        # Deleting the active grid opens New Chat, which lists the host's home directory.
        host_filesystem_api = f"/v1/hosts/{sessions[0].host_id}/filesystem"
        browser_contract.json(host_filesystem_api, list_payload([]))
        for index, session in enumerate(sessions):
            session.update_session(title=f"Grid session {index}")
            session.set_items(
                [
                    message_item(
                        f"item-{index}",
                        "assistant",
                        f"Transcript {index}",
                        response_id=f"r-{index}",
                    )
                ]
            )
        for index, session in enumerate(sessions[:2]):
            path = f"notes-{index}.md"
            _mock_markdown_files(
                page,
                BrowserSession(browser_contract, session_id=session.session_id),
                {path: f"# Workspace {index}"},
            )
            environment_api = f"/v1/sessions/{session.session_id}/resources/environments/default"
            browser_contract.json(f"{environment_api}/changes", list_payload([]))
            browser_contract.json(
                f"{environment_api}/filesystem",
                list_payload([{"path": path, "name": path, "type": "file", "bytes": 20}]),
            )
            browser_contract.json(f"/v1/sessions/{session.session_id}/comments", [])
        sessions[3].update_session(permission_level=1)
        browser_contract.json(
            "/v1/info", {"single_user": True, "features": {"grid_sessions_display_ui": True}}
        )
        browser_contract.json(
            "/v1/sessions", lambda _: list_payload([s.session_snapshot() for s in sessions])
        )
        browser_contract.json(
            "/health",
            {
                "sessions": {
                    s.session_id: {"host_online": True, "runner_online": True} for s in sessions
                }
            },
        )
        page.set_viewport_size({"width": 2000, "height": 1000})
        page.goto(sessions[0].url)
        expect(page.get_by_text("Transcript 0", exact=True)).to_be_visible()
        page.get_by_role("button", name="Filter sessions", exact=True).click()
        page.get_by_role("menuitemradio", name="All sessions", exact=True).click()
        _drag_session(page, "grid-1", "grid-0", "right")
        expect(page).to_have_url(re.compile(r"/grid/[^/]+$"))
        expect(page.get_by_test_id("new-chat-button")).not_to_have_class(
            re.compile(r"bg-\[var\(--sidebar-active\)\]")
        )
        grid_url = page.url
        expect(page.get_by_role("region", name="Grids")).to_be_visible()
        grid_nav = page.get_by_role("region", name="Grids")
        grid_nav.get_by_role("link").dblclick()
        rename = page.get_by_role("textbox", name="Rename grid")
        page.get_by_role("heading", name="Grid · 2 sessions", exact=True).click()
        _move_cell(page, "grid-0", "grid-1")
        expect(grid_nav.get_by_role("link")).to_have_text("Grid session 1 + Grid session 0")
        _move_cell(page, "grid-0", "grid-1")
        expect(grid_nav.get_by_role("link")).to_have_text("Grid session 0 + Grid session 1")
        grid_nav.get_by_role("link").dblclick()
        rename.fill("Cancelled title")
        rename.press("Escape")
        expect(grid_nav.get_by_role("link")).not_to_contain_text("Cancelled title")
        grid_nav.get_by_role("button", name=re.compile("Actions for")).click()
        page.get_by_role("menuitem", name="Rename", exact=True).click()
        rename.fill("Research grid")
        rename.press("Enter")
        expect(grid_nav.get_by_role("link", name="Research grid")).to_be_visible()
        grid_nav.get_by_role("link").click(button="right")
        page.get_by_role("menuitem", name="Rename", exact=True).click()
        rename.fill("Research monitor")
        page.get_by_role("heading", name="Research grid", exact=True).click()
        expect(grid_nav.get_by_role("link", name="Research monitor")).to_be_visible()
        # One grid-wide workspace follows clicks in any cell, keeping its width and tabs.
        page.get_by_role("button", name="Expand grid workspace").click()
        workspace = page.get_by_role("complementary", name="Workspace", exact=True)
        for index in range(2):
            cell = page.locator(f'[data-grid-session="grid-{index}"]')
            cell.get_by_text(f"Transcript {index}", exact=True).click()
            expect(cell).to_have_attribute("data-active", "true")
            expect(workspace).to_have_count(1)
            expect(cell.get_by_role("complementary")).to_have_count(0)
            workspace.get_by_text(f"notes-{index}.md", exact=True).click()
            expect(workspace.get_by_test_id("file-viewer")).to_contain_text(f"Workspace {index}")
            expect(page).to_have_url(grid_url)
        page.screenshot(path=str(artifacts / "shared-workspace.png"))
        first_cell = page.locator('[data-grid-session="grid-0"]')
        first_cell.get_by_text("Transcript 0", exact=True).click()
        first_workspace = workspace
        expect(workspace.get_by_test_id("file-viewer")).to_contain_text("Workspace 0")
        original_area = first_cell.evaluate("node => node.style.gridArea")
        other_cell = page.locator('[data-grid-session="grid-1"]')
        editor = workspace.get_by_test_id("file-viewer").locator('[contenteditable="true"]')
        editor.fill("Unsaved grid file edit")
        other_cell.get_by_text("Transcript 1", exact=True).click()
        expect(workspace.get_by_test_id("file-viewer")).to_contain_text("Workspace 1")
        first_cell.get_by_text("Transcript 0", exact=True).click()
        expect(editor).to_have_text("Unsaved grid file edit")
        workspace.get_by_role("tab", name="Files", exact=True).click()
        page.get_by_role("dialog", name="Unsaved changes").get_by_role(
            "button", name="Discard changes", exact=True
        ).click()
        workspace.get_by_text("notes-0.md", exact=True).first.click()
        expect(workspace.get_by_test_id("file-viewer")).to_contain_text("Workspace 0")
        other_area = other_cell.evaluate("node => node.style.gridArea")
        first_cell.evaluate("node => node.dataset.instance = 'preserved'")
        assert first_cell.locator("header").bounding_box()["height"] == 32
        expect(first_cell.locator('header [data-size="icon-xs"]')).to_have_count(2)
        _move_cell(page, "grid-0", "grid-1")
        assert first_cell.evaluate("node => node.style.gridArea") == other_area
        assert other_cell.evaluate("node => node.style.gridArea") == original_area
        expect(first_cell).to_have_attribute("data-instance", "preserved")
        expect(workspace.get_by_test_id("file-viewer")).to_contain_text("Workspace 0")
        page.screenshot(path=str(artifacts / "workspaces-rearranged.png"), animations="disabled")
        _move_cell(page, "grid-0", "grid-1", cancel=True)
        assert first_cell.evaluate("node => node.style.gridArea") == other_area
        move_title = first_cell.get_by_role("button", name="Move Grid session 0", exact=True)
        move_title.focus()
        page.keyboard.press("Space")
        page.keyboard.press("ArrowLeft")
        expect(page.get_by_test_id("grid-move-ghost")).to_be_visible()
        page.keyboard.press("Space")
        expect(page.get_by_test_id("grid-move-ghost")).to_have_count(0)
        assert first_cell.evaluate("node => node.style.gridArea") == original_area
        first_workspace.get_by_role("button", name="Full screen", exact=True).click()
        expect(first_workspace).to_have_attribute("data-maximized", "true")
        grid_box = page.get_by_test_id("grid-workspace").bounding_box()
        panel_box = workspace.bounding_box()
        assert grid_box and panel_box
        assert abs(panel_box["width"] - grid_box["width"]) < 2
        assert abs(panel_box["height"] - grid_box["height"]) < 2
        page.screenshot(path=str(artifacts / "workspace-maximized-in-grid.png"))
        first_workspace.get_by_role("button", name="Exit full screen", exact=True).click()
        workspace.get_by_role("separator").focus()
        before_width = workspace.bounding_box()["width"]
        page.keyboard.press("ArrowLeft")
        saved_width = workspace.bounding_box()["width"]
        assert saved_width > before_width
        # Pointer resizing and a click in the composer also retain the grid's width.
        handle_box = workspace.get_by_role("separator").bounding_box()
        assert handle_box
        page.mouse.move(handle_box["x"] + 2, handle_box["y"] + 100)
        page.mouse.down()
        page.mouse.move(handle_box["x"] - 30, handle_box["y"] + 100, steps=5)
        page.mouse.up()
        saved_width = workspace.bounding_box()["width"]
        assert saved_width > before_width + 24
        other_cell.get_by_role("textbox").click()
        expect(other_cell).to_have_attribute("data-active", "true")
        expect(workspace.get_by_test_id("file-viewer")).to_contain_text("Workspace 1")
        assert workspace.bounding_box()["width"] == saved_width
        first_cell.get_by_text("Transcript 0", exact=True).click()
        expect(workspace.get_by_test_id("file-viewer")).to_contain_text("Workspace 0")
        assert workspace.bounding_box()["width"] == saved_width
        page.get_by_role("button", name="Collapse grid workspace").click()
        _drag_session(page, "grid-2", "grid-0", "bottom")
        _drag_session(page, "grid-3", "grid-1", "bottom")
        expect(page.locator("[data-grid-session]")).to_have_count(4)
        expect(page.locator('[data-grid-session="grid-3"]').get_by_role("textbox")).to_have_count(
            0
        )
        page.get_by_text("Transcript 3", exact=True).click()
        page.get_by_role("button", name="Expand grid workspace").click()
        read_only_workspace = workspace
        expect(read_only_workspace).to_be_visible()
        expect(read_only_workspace.get_by_role("tab", name="Files", exact=True)).to_have_count(0)
        page.get_by_role("button", name="Collapse grid workspace").click()
        for index in range(4):
            expect(page.get_by_text(f"Transcript {index}", exact=True)).to_be_visible()
            sessions[index].wait_for_stream()
        # Each pane writes to its own session, even while another pane has a draft.
        first = page.locator('[data-grid-session="grid-0"]')
        second = page.locator('[data-grid-session="grid-1"]')
        first.get_by_role("textbox").fill("Keep this draft")
        second.get_by_role("textbox").fill("Send to session one")
        with page.expect_response(
            lambda response: (
                response.url.endswith(f"/{sessions[1].session_id}/events")
                and response.request.method == "POST"
            )
        ):
            second.get_by_role("button", name="Send message", exact=True).click()
        expect(first.get_by_role("textbox")).to_have_value("Keep this draft")
        expect(second.get_by_role("textbox")).to_have_value("")
        assert len(sessions[1].event_posts) == 1
        assert not sessions[0].event_posts
        for session in sessions[:4]:
            session.emit(
                {
                    "event": "session.status",
                    "data": {"conversation_id": session.session_id, "status": "running"},
                }
            )
        expect(page.get_by_test_id("working-indicator")).to_have_count(4)
        previous_first_area = first.evaluate("node => node.style.gridArea")
        last_cell = page.locator('[data-grid-session="grid-3"]')
        last_area = last_cell.evaluate("node => node.style.gridArea")
        _move_cell(page, "grid-0", "grid-3")
        assert first.evaluate("node => node.style.gridArea") == last_area
        assert last_cell.evaluate("node => node.style.gridArea") == previous_first_area
        expect(first.get_by_role("textbox")).to_have_value("Keep this draft")
        # A fifth session and existing members offer no valid grid drop zones.
        page.locator("html").evaluate("node => node.dataset.gridDocument = 'original'")
        for session_id in ("grid-4", "grid-1"):
            row = page.locator(f'aside [data-sidebar-session-id="{session_id}"]')
            row.hover()
            page.mouse.down()
            page.mouse.move(500, 200, steps=10)
            expect(page.get_by_test_id("grid-drop-targets")).to_have_count(0)
            page.mouse.up()
            expect(page).to_have_url(grid_url)
            expect(page.locator("html")).to_have_attribute("data-grid-document", "original")
        # Native link dragging is disabled; even an external URL drop cannot navigate.
        assert page.locator('aside [data-sidebar-session-id="grid-1"] a').evaluate(
            """node => !node.draggable && !node.dispatchEvent(new DragEvent('dragstart', {
                bubbles: true, cancelable: true
            }))"""
        )
        assert page.get_by_role("region", name="Session grid", exact=True).evaluate(
            """node => {
                const dataTransfer = new DataTransfer();
                dataTransfer.setData('text/uri-list', location.origin + '/c/grid-1');
                return !node.dispatchEvent(new DragEvent('drop', {
                    bubbles: true, cancelable: true, dataTransfer
                }));
            }"""
        )
        page.reload()
        expect(page).to_have_url(grid_url)
        expect(grid_nav.get_by_role("link", name="Research monitor")).to_be_visible()
        expect(page.locator("[data-grid-session]")).to_have_count(4)
        assert first.evaluate("node => node.style.gridArea") == last_area
        page.get_by_role("button", name="Expand grid workspace").click()
        assert workspace.bounding_box()["width"] == saved_width
        page.get_by_text("Transcript 3", exact=True).click()
        expect(page.locator('[data-grid-session="grid-3"]')).to_have_attribute(
            "data-active", "true"
        )
        for index in range(4):
            expect(page.get_by_text(f"Transcript {index}", exact=True)).to_be_visible()
        for index in (3, 2, 1):
            page.get_by_role("button", name=f"Remove Grid session {index} from grid").click()
            if index != 1:
                expect(page.locator('[data-grid-session][data-active="true"]')).to_have_count(1)
                expect(workspace).to_have_count(1)
        expect(page).to_have_url(sessions[0].url)
        expect(page.get_by_role("region", name="Grids")).to_have_count(0)
        # The row context menu is the keyboard/touch alternative to dragging.
        page.locator('aside [data-sidebar-session-id="grid-1"]').click(button="right")
        page.get_by_role("menuitem", name="Open in grid", exact=True).focus()
        page.keyboard.press("ArrowRight")
        page.get_by_role("menuitem", name="Session · split bottom", exact=True).click()
        expect(page.locator("[data-grid-session]")).to_have_count(2)
        reopened_url = page.url
        page.get_by_role("link", name="Open Grid session 0 full screen").click()
        expect(page).to_have_url(sessions[0].url)
        page.get_by_role("region", name="Grids").get_by_role("link").click()
        expect(page).to_have_url(reopened_url)
        expect(page.locator("[data-grid-session]")).to_have_count(2)
        page.set_viewport_size({"width": 700, "height": 900})
        page.get_by_role("region", name="Grids").get_by_role("link").click()
        expect(page.get_by_role("button", name="Open sidebar", exact=True)).to_have_attribute(
            "data-size", "icon-sm"
        )
        page.get_by_role("button", name="Expand grid workspace").click()
        narrow_workspace = workspace
        expect(narrow_workspace).to_be_visible()
        narrow_workspace.get_by_role("button", name="Full screen", exact=True).click()
        assert narrow_workspace.bounding_box()["width"] <= 700
        page.screenshot(path=str(artifacts / "narrow-grid-workspace.png"))
        page.set_viewport_size({"width": 2000, "height": 1000})
        page.get_by_role("button", name="Open sidebar", exact=True).click()
        page.get_by_role("region", name="Grids").get_by_role("link").click(button="right")
        with page.expect_response(f"**{host_filesystem_api}?*") as home_listing:
            page.get_by_role("menuitem", name="Delete grid", exact=True).click()
        assert home_listing.value.ok
        expect(page).to_have_url(f"{sessions[0].base_url}/")
        expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible()
        expect(page.get_by_role("region", name="Grids")).to_have_count(0)
        expect(page.locator('aside [data-sidebar-session-id="grid-1"]')).to_be_visible()


@pytest.mark.parametrize("features", [{}, {"grid_sessions_display_ui": False}])
def test_grid_flag_off_and_unknown_local_grid(
    page: Page, browser_contract: BrowserContract, features: dict[str, bool]
) -> None:
    with contextmanager(chat_session_handle)(page, browser_contract) as session:
        browser_contract.json("/v1/info", {"single_user": True, "features": features})
        page.goto(session.url)
        row = page.locator(f'aside [data-sidebar-session-id="{session.session_id}"]')
        expect(row).to_be_visible()
        # A grid saved by an earlier flag-on visit must stay hidden and untouched.
        page.evaluate("""() => localStorage.setItem(
            `omnigent:session-grids:${location.origin}:local`,
            JSON.stringify([{id: 'saved', layout: {
                axis: 'columns', children: ['first', 'second']}}]))""")
        page.reload()
        expect(row).to_be_visible()
        expect(page.get_by_role("region", name="Grids")).to_have_count(0)
        expect(page.locator("[data-grid-session]")).to_have_count(0)
        expect(page.get_by_role("button", name="Expand grid workspace")).to_have_count(0)
        row.click(button="right")
        expect(page.get_by_role("menuitem", name="Open in grid", exact=True)).to_have_count(0)
        page.keyboard.press("Escape")
        assert row.locator("a").evaluate("""node => node.draggable &&
            node.dispatchEvent(new DragEvent('dragstart', {bubbles: true, cancelable: true}))""")
        row.hover()
        page.mouse.down()
        page.mouse.move(800, 300, steps=10)
        expect(page.get_by_test_id("grid-drop-targets")).to_have_count(0)
        page.mouse.up()
        expect(page).to_have_url(session.url)
        assert page.evaluate(
            "localStorage.getItem(`omnigent:session-grids:${location.origin}:local`)"
        )
        page.goto(f"{session.base_url}/grid/saved")
        expect(page.get_by_text("Page not found", exact=True)).to_be_visible()
        page.goto(f"{session.base_url}/grid/missing")
        expect(page.get_by_text("Page not found", exact=True)).to_be_visible()
        browser_contract.json(
            "/v1/info", {"single_user": True, "features": {"grid_sessions_display_ui": True}}
        )
        page.reload()
        expect(page.get_by_text("Grid unavailable", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="Back to sessions")).to_be_visible()
