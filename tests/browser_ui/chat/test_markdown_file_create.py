"""Browser contract for creating a Markdown file from the Workspace rail."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import Page, Route, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


def test_markdown_file_action_uses_atomic_create_and_avoids_collisions(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
) -> None:
    """Create the next root note without treating a nested name as occupied."""
    chat = chat_session_contract
    chat.update_session(permission_level=4)
    environment_path = f"/v1/sessions/{chat.session_id}/resources/environments/default"
    environment = chat.base_url + environment_path
    existing_content = {
        "untitled.md": "# Existing root note\n",
        "src/untitled-2.md": "# Nested note\n",
    }
    root_entries: list[dict[str, object]] = [
        {
            "id": "root-untitled",
            "name": "untitled.md",
            "path": "untitled.md",
            "type": "file",
            "bytes": len(existing_content["untitled.md"]),
            "modified_at": 1,
        },
        {
            "id": "src-directory",
            "name": "src",
            "path": "src",
            "type": "directory",
            "bytes": None,
            "modified_at": 1,
        },
        {
            "id": "nested-untitled",
            "name": "untitled-2.md",
            "path": "src/untitled-2.md",
            "type": "file",
            "bytes": len(existing_content["src/untitled-2.md"]),
            "modified_at": 1,
        },
    ]
    create_paths: list[str] = []

    chat.contract.json(environment_path, {"metadata": {"root": "/browser-workspace"}})

    def list_root(route: Route) -> None:
        if route.request.method != "GET":
            route.fallback()
            return
        route.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "list",
                    "data": root_entries,
                    "has_more": False,
                }
            ),
        )

    root_matcher = re.compile(rf"^{re.escape(environment)}/filesystem(?:\?.*)?$")
    chat.contract.route(root_matcher, list_root)

    def create_file(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        path = unquote(urlparse(route.request.url).path.split("/filesystem/", 1)[1])
        body = json.loads(route.request.post_data or "{}")
        assert body == {"content": "", "encoding": "utf-8"}
        create_paths.append(path)
        if not any(entry["path"] == path for entry in root_entries):
            root_entries.append(
                {
                    "id": "created-untitled-2",
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "type": "file",
                    "bytes": 0,
                    "modified_at": 2,
                }
            )
        existing_content[path] = ""
        route.fulfill(
            status=201,
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "session.environment.filesystem.file_content",
                    "path": path,
                    "content": "",
                    "encoding": "utf-8",
                    "content_type": "text/markdown",
                    "bytes": 0,
                }
            ),
        )

    create_matcher = re.compile(rf"^{re.escape(environment)}/filesystem/.+$")
    chat.contract.route(create_matcher, create_file)

    def serve_file(route: Route) -> None:
        if route.request.method != "GET":
            route.fallback()
            return
        path = unquote(urlparse(route.request.url).path.split("/filesystem/", 1)[1])
        if path not in existing_content:
            route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"detail": "file not found"}),
            )
            return
        content = existing_content[path]
        route.fulfill(
            content_type="application/json",
            body=json.dumps(
                {
                    "object": "session.environment.filesystem.file_content",
                    "path": path,
                    "content": content,
                    "encoding": "utf-8",
                    "content_type": "text/markdown",
                    "bytes": len(content),
                }
            ),
        )

    chat.contract.route(create_matcher, serve_file)
    chat.contract.json(
        f"{environment_path}/changes",
        lambda _request: {
            "object": "list",
            "has_more": False,
            "data": [
                {
                    "path": path,
                    "name": path.rsplit("/", 1)[-1],
                    "status": "created",
                    "bytes": 0,
                    "modified_at": 2,
                    "lines_added": 0,
                    "lines_removed": 0,
                }
                for path in create_paths
            ],
        },
    )
    chat.contract.json(f"/v1/sessions/{chat.session_id}/comments", [])
    chat.contract.json(
        f"{environment_path}/diff/untitled-2.md",
        {
            "object": "session.environment.filesystem.file_diff",
            "path": "untitled-2.md",
            "before": None,
            "after": "",
        },
    )

    page.goto(chat.url)
    expect(page.get_by_role("button", name="Expand right panel")).to_be_visible(timeout=30_000)
    page.get_by_role("button", name="Expand right panel").click()
    workspace = page.get_by_role("complementary", name="Workspace")
    expect(workspace).to_be_visible(timeout=30_000)

    open_new = workspace.get_by_role("button", name="Open new")
    expect(open_new).to_be_visible()
    open_new.click()
    markdown_item = page.get_by_role("menuitem", name="Markdown file", exact=True)
    expect(markdown_item).to_be_visible()
    expect(markdown_item).to_be_enabled()

    evidence_dir = Path(output_path)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(evidence_dir / "markdown-create-menu.png"),
        full_page=False,
        animations="disabled",
    )

    with page.expect_request(
        lambda request: request.method == "POST" and "/filesystem/" in request.url,
        timeout=30_000,
    ) as create_request:
        markdown_item.click()

    request_path = unquote(urlparse(create_request.value.url).path.split("/filesystem/", 1)[1])
    assert request_path == "untitled-2.md"
    assert create_paths == ["untitled-2.md"]
    assert existing_content["untitled.md"] == "# Existing root note\n"
    paths = [str(entry["path"]) for entry in root_entries]
    assert "untitled.md" in paths
    assert "src/untitled-2.md" in paths
    assert request_path != "src/untitled-2.md"

    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(viewer).to_be_visible(timeout=30_000)
    toolbar = viewer.get_by_test_id("FILESTOOLBAR")
    expect(toolbar.get_by_text("untitled-2.md", exact=True)).to_be_visible(timeout=30_000)
    expect(viewer.locator('[contenteditable="true"]')).to_be_visible(timeout=30_000)
    page.screenshot(
        path=str(evidence_dir / "markdown-created-editor.png"),
        full_page=False,
        animations="disabled",
    )
