"""Folder and file editor links through the built SPA in browser and desktop modes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.files.test_file_line_navigation import BrowserSession, _mock_markdown_files
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture


@pytest.mark.parametrize(
    ("width", "host_id", "ssh_host", "desktop"),
    [
        (360, "local", None, True),
        (950, "local", None, True),
        (950, "arca", "arca.ssh", True),
        (360, "arclet", "arclet_dev.ssh", False),
        (360, "remote", None, False),
    ],
)
def test_open_folder_and_file_in_app(
    page: Page,
    seeded_session: BrowserSession,
    width: int,
    host_id: str,
    ssh_host: str | None,
    desktop: bool,
    tmp_path: Path,
) -> None:
    session = seeded_session
    sid = session.session_id
    path = (
        "src/components/file-browser/toolbars/main.md"
        if width == 360 and host_id == "local"
        else "src/main.md"
    )
    _mock_markdown_files(page, session, {path: "# Open in an editor\n\nWorkspace document."})
    session.contract.json(
        f"/v1/sessions/{sid}",
        {
            "id": sid,
            "object": "conversation",
            "title": "Open files in your apps",
            "agent_id": "file-line-agent",
            "agent_name": "hello_world",
            "host_id": host_id,
            "status": "idle",
            "created_at": 1,
            "updated_at": 1,
            "labels": {},
            "permission_level": None,
        },
    )
    session.contract.json(
        f"/v1/sessions/{sid}/resources/environments/default/filesystem",
        {
            "object": "list",
            "data": [{"path": path, "name": "main.md", "type": "file", "bytes": 50}],
        },
    )
    session.contract.json(f"/v1/hosts/{host_id}/worktrees", {"data": []})
    session.contract.json(f"/v1/hosts/{host_id}", {"ssh_target": ssh_host})
    state = [{"id": sid, "state": {"open": True, "widthPx": width, "rightRailTab": "files"}}]
    encoded_state = json.dumps(json.dumps(state))
    page.add_init_script(
        f"localStorage.setItem('omnigent:session-workspace-state', {encoded_state});"
    )
    page.add_init_script("""
      window.__openInCalls = [];
      window.open = (url) => { window.__openInCalls.push(url); return null; };
      const originalAppend = Node.prototype.appendChild;
      Node.prototype.appendChild = function(child) {
        if (child instanceof HTMLIFrameElement && /^(cursor|vscode):/.test(child.src)) {
          window.__openInCalls.push(child.src);
          child.removeAttribute('src');
        }
        return originalAppend.call(this, child);
      };
    """)
    if desktop:
        page.add_init_script("""
          window.omnigentDesktop = {
            kind: "electron",
            getHostIdentity: async () => ({ cliInstalled: true, hostId: "local" }),
            revealFile: async () => true,
          };
        """)
    page.set_viewport_size({"width": 1800, "height": 1000})
    page.goto(f"{session.contract.base_url}/c/{sid}")
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    folder_open = rail.get_by_role("button", name="Choose app to open in", exact=True)
    if host_id == "remote":
        expect(folder_open).to_have_count(0)
        rail.get_by_text("main.md", exact=True).click()
        viewer = rail.get_by_test_id("file-viewer")
        expect(viewer).to_be_visible()
        expect(
            viewer.get_by_role("button", name="Choose app to open in", exact=True)
        ).to_have_count(0)
        # With fewer actions the toolbar fits inline, even in a narrow panel.
        expect(viewer.get_by_role("button", name="Copy link to file")).to_be_visible()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert page.evaluate("window.__openInCalls") == []
        page.screenshot(path=str(tmp_path / "unknown-remote-menu.png"), animations="disabled")
        return
    expect(folder_open).to_be_visible()
    copy_folder = rail.get_by_role("button", name="Copy folder path: workspace")
    default_folder = rail.get_by_role("button", name="Open in VS Code", exact=True)
    assert (
        copy_folder.bounding_box()["x"]
        < default_folder.bounding_box()["x"]
        < folder_open.bounding_box()["x"]
    )
    page.screenshot(path=str(tmp_path / "folder-split-button.png"), animations="disabled")
    default_folder.click()
    location = "file" if host_id == "local" else f"vscode-remote/ssh-remote+{ssh_host}"
    expected_folder = f"vscode://{location}/workspace"
    page.wait_for_function("window.__openInCalls.length === 1")
    assert page.evaluate("window.__openInCalls") == [expected_folder]
    expect(page.get_by_role("menu")).to_have_count(0)
    folder_open.click()
    page.screenshot(path=str(tmp_path / "folder-menu.png"), animations="disabled")
    if host_id != "local":
        expect(page.get_by_role("menuitem", name="Terminal", exact=True)).to_have_count(0)
        expect(page.get_by_role("menuitem", name="Default app", exact=True)).to_have_count(0)
    page.get_by_role("menuitem", name="Cursor", exact=True).click()
    page.wait_for_function("window.__openInCalls.length === 2")
    expect(rail.get_by_role("button", name="Open in Cursor", exact=True)).to_be_visible()

    rail.get_by_text("main.md", exact=True).click()
    viewer = rail.get_by_test_id("file-viewer")
    expect(viewer).to_be_visible()
    if width == 360:
        expect(viewer.get_by_role("button", name="More actions", exact=True)).to_be_visible()
    # One-click opening stays visible when the other toolbar actions collapse.
    default_file = viewer.get_by_role("button", name="Open in Cursor", exact=True)
    expect(default_file).to_be_visible()
    expect(viewer.get_by_role("button", name="Choose app to open in", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / "file-split-button.png"), animations="disabled")
    default_file.click()
    page.wait_for_function("window.__openInCalls.length === 3")
    selection = "" if host_id == "local" else ":1:1"
    expected_file = f"cursor://{location}/workspace/{quote(path, safe='/')}{selection}"
    assert page.evaluate("window.__openInCalls")[2] == expected_file
    viewer.get_by_role("button", name="Choose app to open in", exact=True).click()
    expect(page.get_by_role("menuitem", name="VS Code", exact=True)).to_be_visible()
    page.screenshot(path=str(tmp_path / "file-menu.png"), animations="disabled")
    page.get_by_role("menuitem", name="VS Code", exact=True).click()
    page.wait_for_function("window.__openInCalls.length === 4")
    expect(page.get_by_role("dialog")).to_have_count(0)
    expected_file = f"vscode://{location}/workspace/{quote(path, safe='/')}{selection}"
    assert page.evaluate("window.__openInCalls")[3] == expected_file
