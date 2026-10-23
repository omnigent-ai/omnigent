"""Real drag/drop, routing and concurrent transcripts against explicit browser contracts."""

from __future__ import annotations

import re
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

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


def _move_cell(
    page: Page, session_id: str, target_id: str, *, cancel: bool = False, edge: str | None = None
) -> None:
    title = page.locator(f'[data-grid-session="{session_id}"]').get_by_role(
        "button", name=re.compile("^Move ")
    )
    box = title.bounding_box()
    assert box
    page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 50, box["y"] + box["height"] / 2)
    target = page.locator(f'[data-grid-session="{target_id}"]')
    if page.locator("[data-grid-session]").count() > 2:
        expect(page.get_by_test_id("grid-drop-targets")).to_have_count(0)
    else:
        expect(page.locator("[data-grid-drop-edge]")).to_have_count(2)
    if edge:
        grid_box = page.get_by_test_id("session-grid").bounding_box()
        source_box = page.locator(f'[data-grid-session="{session_id}"]').bounding_box()
        assert grid_box and source_box
        # Reach the grid boundary on the dragged cell's side, not the other cell.
        point = {
            "x": source_box["x"] + source_box["width"] / 2,
            "y": source_box["y"] + source_box["height"] / 2,
        }
        dimension, coordinate = ("height", "y") if edge in ("top", "bottom") else ("width", "x")
        point[coordinate] = grid_box[coordinate] + (
            grid_box[dimension] - 4 if edge in ("right", "bottom") else 4
        )
        page.mouse.move(**point)
    else:
        target.hover()
    ghost = page.get_by_test_id("grid-drop-ghost" if edge else "grid-move-ghost")
    expect(ghost).to_be_visible()
    if edge:
        grid_box = page.get_by_test_id("session-grid").bounding_box()
        ghost_box = ghost.bounding_box()
        assert grid_box and ghost_box
        expected = dict(grid_box)
        dimension, coordinate = ("height", "y") if edge in ("top", "bottom") else ("width", "x")
        expected[dimension] /= 2
        if edge in ("right", "bottom"):
            expected[coordinate] += expected[dimension]
        for key in expected:
            assert ghost_box[key] == pytest.approx(expected[key], abs=1)
    if cancel:
        page.keyboard.press("Escape")
    page.mouse.up()
    expect(ghost).to_have_count(0)


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
            session.update_session(labels={"omnigent.ui": "terminal"})
            browser_contract.json(
                f"/v1/sessions/{session.session_id}/resources/terminals",
                list_payload(
                    [
                        {
                            "id": "terminal_tui_main",
                            "object": "terminal",
                            "name": "tui",
                            "metadata": {
                                "terminal_name": "tui",
                                "session_key": "main",
                                "running": True,
                            },
                        }
                    ]
                ),
            )
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
        terminal_inputs: dict[str, list[bytes]] = {}
        terminal_connections: dict[str, int] = {}

        def attach_terminal(socket: WebSocketRoute) -> None:
            session_id = socket.url.split("/sessions/")[1].split("/")[0]
            terminal_connections[session_id] = terminal_connections.get(session_id, 0) + 1
            terminal_inputs.setdefault(session_id, [])
            socket.send(f"Terminal for {session_id}\r\n$ ".encode())

            def record_input(message: str | bytes) -> None:
                if isinstance(message, bytes):
                    terminal_inputs[session_id].append(message)
                    socket.send(message)

            socket.on_message(record_input)

        browser_contract.websocket(
            "**/resources/terminals/terminal_tui_main/attach*", attach_terminal
        )
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
        active_style = re.compile(r"bg-\[var\(--sidebar-active\)\]")
        for index in range(5):
            link = page.locator(f'aside [data-sidebar-session-id="grid-{index}"] a')
            if index < 2:
                expect(link).to_have_class(active_style)
            else:
                expect(link).not_to_have_class(active_style)
        expect(page.get_by_role("region", name="Grids")).to_be_visible()
        grid_nav = page.get_by_role("region", name="Grids")
        # dnd-kit suppresses click propagation for 50 ms after a drop.
        grid_nav.get_by_role("link").dblclick(delay=75)
        rename = page.get_by_role("textbox", name="Rename grid")
        page.get_by_role("heading", name="Grid · 2 sessions", exact=True).click()
        _move_cell(page, "grid-0", "grid-1")
        expect(grid_nav.get_by_role("link")).to_have_text("Grid session 1 + Grid session 0")
        _move_cell(page, "grid-0", "grid-1")
        expect(grid_nav.get_by_role("link")).to_have_text("Grid session 0 + Grid session 1")
        grid_nav.get_by_role("link").dblclick(delay=75)
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
        page.get_by_role("heading", name="Research grid · 2 sessions", exact=True).click()
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
        header = (
            page.get_by_role("region", name="Session grid", exact=True).locator("header").first
        )
        header_box = header.bounding_box()
        panel_box = workspace.bounding_box()
        assert header_box and panel_box
        assert panel_box["y"] == 0
        assert panel_box["height"] == page.viewport_size["height"]
        assert abs(header_box["x"] + header_box["width"] - panel_box["x"]) < 2
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
        expect(first_cell.locator('header [data-size="icon-xs"]')).to_have_count(4)
        # Both cells use real xterm attaches, with independent input and view selection.
        first_cell.get_by_test_id("side-chat-input").fill("Keep this chat draft")
        for cell in (first_cell, other_cell):
            cell.get_by_role("button", name="Terminal view", exact=True).click()
            expect(cell.get_by_test_id("terminal-view")).to_have_attribute(
                "data-state", "connected"
            )
        connections = dict(terminal_connections)
        _move_cell(page, "grid-0", "grid-1", edge="top", cancel=True)
        assert first_cell.evaluate("node => node.style.gridArea") == original_area
        for edge, axis, order in [
            ("top", "rows", ["grid-0", "grid-1"]),
            ("right", "columns", ["grid-1", "grid-0"]),
            ("bottom", "rows", ["grid-1", "grid-0"]),
            ("left", "columns", ["grid-0", "grid-1"]),
        ]:
            _move_cell(page, "grid-0", "grid-1", edge=edge)
            first_box, other_box = first_cell.bounding_box(), other_cell.bounding_box()
            assert first_box and other_box
            if axis == "rows":
                assert first_box["x"] == other_box["x"]
                assert first_box["y"] != other_box["y"]
            else:
                assert first_box["y"] == other_box["y"]
                assert first_box["x"] != other_box["x"]
            saved = page.evaluate("""() => JSON.parse(localStorage.getItem(
                `omnigent:session-grids:${location.origin}:local`))[0].layout""")
            assert saved == {"axis": axis, "children": order}
            expect(page).to_have_url(grid_url)
            expect(first_cell).to_have_attribute("data-instance", "preserved")
            assert terminal_connections == connections
            page.screenshot(path=str(artifacts / f"two-sessions-{edge}.png"))
        first_cell.locator(".xterm-helper-textarea").click()
        page.keyboard.type("first")
        expect(first_cell).to_have_attribute("data-active", "true")
        other_cell.locator(".xterm-helper-textarea").click()
        page.keyboard.type("second")
        expect(other_cell).to_have_attribute("data-active", "true")
        page.wait_for_function(
            "() => document.activeElement?.classList.contains('xterm-helper-textarea')"
        )
        assert b"first" in b"".join(terminal_inputs["grid-0"])
        assert b"second" in b"".join(terminal_inputs["grid-1"])
        assert b"second" not in b"".join(terminal_inputs["grid-0"])
        page.screenshot(path=str(artifacts / "grid-terminals.png"))
        page.keyboard.press("ControlOrMeta+Alt+Backslash")
        expect(other_cell.get_by_test_id("side-chat-input")).to_be_visible()
        expect(first_cell.get_by_test_id("main-terminal-view")).to_have_attribute(
            "data-visible", "true"
        )
        first_cell.get_by_role("button", name="Chat view", exact=True).click()
        expect(first_cell.get_by_test_id("side-chat-input")).to_have_value("Keep this chat draft")
        first_cell.get_by_role("button", name="Terminal view", exact=True).click()
        assert terminal_connections == connections
        first_cell.get_by_role("button", name="Chat view", exact=True).click()
        first_cell.get_by_test_id("side-chat-input").fill("")
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
        assert panel_box["y"] == 0
        assert header.evaluate("node => !!node.closest('[inert]')")
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
        assert header.bounding_box()["width"] > header_box["width"]
        _drag_session(page, "grid-2", "grid-0", "bottom")
        for index in range(3):
            expect(
                page.locator(f'aside [data-sidebar-session-id="grid-{index}"] a')
            ).to_have_class(active_style)
        page.screenshot(path=str(artifacts / "grid-members-highlighted.png"))
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
        sessions[1].reject_uploads = False
        second.locator('input[type="file"]').set_input_files(
            {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"notes"}
        )
        page.route(
            "**/v1/sessions/grid-1/events",
            lambda route: route.fulfill(status=503, json={"detail": "Temporary send failure"}),
            times=1,
        )
        with page.expect_response(
            lambda response: response.url.endswith("/grid-1/events") and response.status == 503
        ):
            second.get_by_role("button", name="Send message", exact=True).click()
        expect(second.get_by_role("textbox")).to_have_value("Send to session one")
        expect(second.get_by_text("notes.txt", exact=True)).to_be_visible()
        expect(first.get_by_role("textbox")).to_have_value("Keep this draft")
        page.screenshot(path=str(artifacts / "grid-failed-send-restored.png"))
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
            expect(
                page.locator(f'aside [data-sidebar-session-id="grid-{index}"] a')
            ).not_to_have_class(active_style)
            if index != 1:
                expect(page.locator('[data-grid-session][data-active="true"]')).to_have_count(1)
                expect(workspace).to_have_count(1)
        expect(page).to_have_url(sessions[0].url)
        expect(page.get_by_role("region", name="Grids")).to_have_count(0)
        # The row context menu is the keyboard alternative to dragging.
        page.locator('aside [data-sidebar-session-id="grid-1"]').click(button="right")
        page.get_by_role("menuitem", name="Open in grid", exact=True).focus()
        page.keyboard.press("ArrowRight")
        page.get_by_role("menuitem", name="Session · split bottom", exact=True).click()
        expect(page.locator("[data-grid-session]")).to_have_count(2)
        reopened_url = page.url
        page.get_by_role("link", name="Open Grid session 0 full screen").click()
        expect(page).to_have_url(sessions[0].url)
        expect(page.locator('aside [data-sidebar-session-id="grid-0"] a')).to_have_class(
            active_style
        )
        expect(page.locator('aside [data-sidebar-session-id="grid-1"] a')).not_to_have_class(
            active_style
        )
        page.get_by_role("region", name="Grids").get_by_role("link").click()
        expect(page).to_have_url(reopened_url)
        expect(page.locator("[data-grid-session]")).to_have_count(2)
        saved_grids = page.evaluate(
            "localStorage.getItem(`omnigent:session-grids:${location.origin}:local`)"
        )
        page.set_viewport_size({"width": 700, "height": 900})
        expect(
            page.get_by_text("Grids are available on desktop only.", exact=False)
        ).to_be_visible()
        expect(page.locator("[data-grid-session]")).to_have_count(0)
        expect(page.get_by_role("region", name="Grids")).to_have_count(0)
        expect(page.get_by_role("button", name="Expand grid workspace")).to_have_count(0)
        page.screenshot(path=str(artifacts / "mobile-grid-blocked.png"))
        page.reload()
        expect(
            page.get_by_text("Grids are available on desktop only.", exact=False)
        ).to_be_visible()
        assert (
            page.evaluate(
                "localStorage.getItem(`omnigent:session-grids:${location.origin}:local`)"
            )
            == saved_grids
        )
        page.set_viewport_size({"width": 2000, "height": 1000})
        expect(page.locator("[data-grid-session]")).to_have_count(2)
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

        if not features:
            browser_contract.json("/v1/me", {"user_id": None, "is_admin": False})
            page.reload()
            expect(
                page.get_by_text("Your account identity is unavailable. Reload to retry.")
            ).to_be_visible()
            browser_contract.json("/v1/me", {"user_id": "local", "is_admin": True})
            page.get_by_role("button", name="Reload", exact=True).click()
            expect(
                page.get_by_text("This grid isn’t saved on this device.", exact=False)
            ).to_be_visible()


@pytest.mark.parametrize("width,touch", [(390, False), (1024, True), (1280, False)])
def test_grid_mobile_entry_points(
    page: Page, browser_contract: BrowserContract, width: int, touch: bool
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    if touch:
        page.context.new_cdp_session(page).send(
            "Emulation.setTouchEmulationEnabled", {"enabled": True}
        )
    assert page.evaluate("matchMedia('(pointer: coarse)').matches") == touch
    desktop = width >= 768 and not touch
    with ExitStack() as stack:
        sessions = [
            stack.enter_context(
                contextmanager(chat_session_handle)(
                    page, browser_contract, session_id=f"entry-{index}"
                )
            )
            for index in range(2)
        ]
        browser_contract.json(
            "/v1/info", {"single_user": True, "features": {"grid_sessions_display_ui": True}}
        )
        browser_contract.json(
            "/v1/sessions", lambda _: list_payload([s.session_snapshot() for s in sessions])
        )
        page.goto(sessions[0].url)
        page.evaluate(
            """ids => localStorage.setItem(
            `omnigent:session-grids:${location.origin}:local`,
            JSON.stringify([{id: 'saved', layout: {axis: 'columns', children: ids}}]))""",
            [s.session_id for s in sessions],
        )
        page.reload()
        if width < 768:
            page.get_by_role("button", name="Open sidebar", exact=True).click()
        row = page.locator(f'aside [data-sidebar-session-id="{sessions[1].session_id}"]')
        expect(row).to_be_visible()
        expect(page.get_by_role("region", name="Grids")).to_have_count(int(desktop))
        expect(page.locator("[data-grid-session]")).to_have_count(int(desktop))
        if width < 768:
            row.click(button="right")
        else:
            row.get_by_test_id("conversation-actions").click()
        expect(page.get_by_role("menu")).to_be_visible()
        expect(page.get_by_role("menuitem", name="Open in grid", exact=True)).to_have_count(
            int(desktop)
        )
        page.keyboard.press("Escape")
        box = row.bounding_box()
        assert box
        page.mouse.move(box["x"] + 30, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + 45, box["y"] + box["height"] / 2, steps=3)
        expect(row).to_have_class(re.compile("opacity-40"))
        expect(page.get_by_test_id("grid-drop-targets")).to_have_count(int(desktop))
        page.keyboard.press("Escape")
        page.mouse.up()
        page.goto(f"{sessions[0].base_url}/grid/saved")
        if desktop:
            expect(page.get_by_test_id("session-grid")).to_be_visible()
            expect(page.locator("[data-grid-session]")).to_have_count(2)
        else:
            expect(
                page.get_by_text("Grids are available on desktop only.", exact=False)
            ).to_be_visible()
            expect(page.locator("[data-grid-session]")).to_have_count(0)
            expect(page.get_by_role("link", name="Back to sessions")).to_be_visible()
