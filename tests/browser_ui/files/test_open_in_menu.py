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
    ("width", "host_id", "host_name", "sandbox_provider", "ssh_host", "desktop", "available"),
    [
        (360, "local", "laptop", None, None, True, True),
        (950, "local", "laptop", None, None, True, True),
        (950, "arca", "my-arca", "databricks-arca", "arca.ssh", True, True),
        (950, "personal-arca", "ip-10-92-23-179", None, "arca.ssh", False, True),
        (950, "personal-arclet", "custom name", None, "arclet_conversant-crane.ssh", False, True),
        (
            360,
            "arclet",
            "conversant-crane",
            "arclet",
            "arclet_conversant-crane.ssh",
            False,
            True,
        ),
        (950, "unknown", "custom-alias", None, None, False, False),
    ],
)
def test_open_folder_and_file_in_app(
    page: Page,
    seeded_session: BrowserSession,
    width: int,
    host_id: str,
    host_name: str,
    sandbox_provider: str | None,
    ssh_host: str | None,
    desktop: bool,
    available: bool,
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
    session.contract.json(
        f"/v1/sessions/{sid}/resources/environments/default/filesystem/etc/metric_proxy/labels/default?base=host",
        {"detail": "File not found"},
        status=404,
    )
    if sandbox_provider is None and ssh_host is not None:
        machine_type = "arca" if ssh_host == "arca.ssh" else "arclet"
        metadata_files = {"etc/metric_proxy/labels/default": f"arca_type {machine_type}\n"}
        if machine_type == "arclet":
            metadata_files["opt/arclet/arclet-name"] = "conversant-crane\n"
        for metadata_path, content in metadata_files.items():
            session.contract.json(
                f"/v1/sessions/{sid}/resources/environments/default/filesystem/{metadata_path}?base=host",
                {
                    "object": "session.environment.filesystem.file_content",
                    "path": f"/{metadata_path}",
                    "content_type": "text/plain",
                    "encoding": "utf-8",
                    "content": content,
                    "bytes": len(content),
                },
            )
    session.contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": host_id,
                    "name": host_name,
                    "owner": "local",
                    "status": "online",
                    "sandbox_provider": sandbox_provider,
                }
            ]
        },
    )
    state = [{"id": sid, "state": {"open": True, "widthPx": width, "rightRailTab": "files"}}]
    encoded_state = json.dumps(json.dumps(state))
    page.add_init_script(
        f"localStorage.setItem('omnigent:session-workspace-state', {encoded_state});"
    )
    page.add_init_script("""
      window.__openInCalls = [];
      window.__openInCspViolations = [];
      window.open = (url) => { window.__openInCalls.push(url); return null; };
      document.addEventListener('securitypolicyviolation', (event) => {
        if (event.effectiveDirective === 'frame-src') {
          window.__openInCspViolations.push(event.blockedURI);
        }
      });
      document.addEventListener('click', (event) => {
        const link = event.target.closest?.('a');
        if (link && /^(cursor|vscode):/.test(link.href)) {
          window.__openInCalls.push(link.href);
          event.preventDefault();
        }
      }, true);
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
    page.evaluate("""() => {
      const policy = document.createElement('meta');
      policy.httpEquiv = 'Content-Security-Policy';
      policy.content = 'frame-src *';
      document.head.appendChild(policy);
    }""")
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    folder_open = rail.get_by_role("button", name="Choose app to open in", exact=True)
    if not available:
        expect(folder_open).to_have_count(0)
        rail.get_by_text("main.md", exact=True).click()
        viewer = rail.get_by_test_id("file-viewer")
        expect(viewer).to_be_visible()
        expect(
            viewer.get_by_role("button", name="Choose app to open in", exact=True)
        ).to_have_count(0)
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
    page.wait_for_function(
        "window.__openInCalls.length === 1 || window.__openInCspViolations.length > 0"
    )
    assert page.evaluate("window.__openInCspViolations") == []
    assert page.evaluate("window.__openInCalls") == [expected_folder]
    expect(page.get_by_role("menu")).to_have_count(0)
    folder_open.click()
    page.screenshot(path=str(tmp_path / "folder-menu.png"), animations="disabled")
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
    assert page.evaluate("window.__openInCspViolations") == []
