"""File references use Enter/arrows without trapping browser focus navigation."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_file_mentions"
_ROOT = "/work/repo"


def test_file_mention_keyboard(page: Page, live_server: str) -> None:
    """Browse, attach files/folders, and Tab away in the real landing composer."""
    page.route(
        "**/v1/agents",
        lambda route: route.fulfill(
            json={
                "data": [
                    {
                        "id": "ag_claude_mentions",
                        "name": "claude-native-ui",
                        "display_name": "Claude Code",
                        "harness": "claude-native",
                        "skills": [],
                    }
                ]
            }
        ),
    )
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            json={
                "hosts": [
                    {"host_id": _HOST_ID, "name": "demo-host", "status": "online", "owner": "e2e"}
                ]
            }
        ),
    )
    page.route(
        "**/v1/hosts/*/harnesses/*/model-options",
        lambda route: route.fulfill(json={"models": []}),
    )
    page.route(
        re.compile(r"/v1/hosts/[^/]+/worktrees"),
        lambda route: route.fulfill(json={"data": []}),
    )
    creates: list[str] = []

    def sessions(route: Route) -> None:
        if route.request.method == "POST":
            creates.append(route.request.url)
        route.fulfill(json={"data": [], "has_more": False})

    page.route(re.compile(r"/v1/sessions(\?.*)?$"), sessions)

    def filesystem(route: Route) -> None:
        directory = unquote(urlparse(route.request.url).path.split("/filesystem", 1)[1])
        listing = {
            _ROOT: [("src", "directory"), ("README.md", "file")],
            f"{_ROOT}/src": [("nested", "directory"), ("app.ts", "file")],
            f"{_ROOT}/src/nested": [],
        }.get(directory, [])
        route.fulfill(
            json={
                "object": "list",
                "data": [
                    {
                        "name": name,
                        "path": f"{directory}/{name}",
                        "type": kind,
                        "bytes": None,
                        "modified_at": 0,
                    }
                    for name, kind in listing
                ],
                "has_more": False,
            }
        )

    page.route(re.compile(r"/v1/hosts/[^/]+/filesystem"), filesystem)
    page.add_init_script(
        'localStorage.setItem("omnigent:recent-workspaces", '
        f'JSON.stringify({{"{_HOST_ID}": ["{_ROOT}"]}}));'
    )
    page.goto(f"{live_server}/")
    composer = page.get_by_test_id("new-chat-landing-input")
    expect(composer).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text("repo")

    composer.fill("@")
    expect(page.get_by_title("Open src", exact=True)).to_be_visible()
    composer.press("ArrowRight")
    expect(composer).to_have_value("@src/")
    expect(page.get_by_title("Open nested", exact=True)).to_be_visible()
    composer.press("ArrowRight")
    expect(composer).to_have_value("@src/nested/")
    composer.press("ArrowLeft")
    expect(composer).to_have_value("@src/")
    composer.press("Backspace")
    expect(composer).to_have_value("@")
    expect(page.get_by_title("Open src", exact=True)).to_be_visible()
    composer.press("Enter")
    expect(page.get_by_text("@src/", exact=True)).to_be_visible()
    expect(composer).to_have_value("")

    composer.fill("@src/ap")
    expect(page.get_by_title("Attach app.ts", exact=True)).to_be_visible()
    composer.press("Backspace")
    expect(composer).to_have_value("@src/a")
    composer.press("Enter")
    expect(page.get_by_text("@src/app.ts", exact=True)).to_be_visible()

    for key in ("Tab", "Shift+Tab"):
        composer.fill("")
        composer.fill("@README")
        expect(page.get_by_title("Attach README.md", exact=True)).to_be_visible()
        composer.press(key)
        expect(composer).not_to_be_focused()
        expect(composer).to_have_value("@README")
        expect(page.get_by_role("listbox")).to_have_count(0)
        expect(page.get_by_text("@README.md", exact=True)).to_have_count(0)
        assert page.evaluate("document.activeElement !== document.body")

    assert creates == [], "Reference-picker keys must not launch a session"
